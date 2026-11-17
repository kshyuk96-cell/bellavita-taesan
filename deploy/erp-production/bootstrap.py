"""Generic bootstrap. Proprietary runtime is retrieved only from the private ERP."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
ALLOWED = {'client.py','render_child.py','company_renderer.py','asset_bundle.py','company/__init__.py','company/render.py','company/sender.py','company/event_scroll.py','legacy_adapter.py','legacy_vision.py','legacy/server.py','legacy/automation.py','legacy/invitation.py','legacy/messages.json','legacy/face_detection_yunet_2023mar.onnx','legacy/YUNET-LICENSE.txt','manifest.json'}


def download_runtime(origin, token, expected_digest, worker_key=None):
    u=urllib.parse.urlsplit(origin)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.path not in ('','/') or u.query or u.fragment:
        raise ValueError('HTTPS ERP origin required')
    if not re.fullmatch(r'[0-9a-f]{64}',expected_digest):
        raise ValueError('Pinned runtime digest required')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args):return None
    headers={'OAI-Sites-Authorization':'Bearer '+token}
    if worker_key:headers['X-Production-Worker-Key']=worker_key
    request=urllib.request.Request(origin.rstrip('/')+'/production-runtime.zip',headers=headers)
    with urllib.request.build_opener(NoRedirect).open(request,timeout=45) as response:
        data=response.read(2*1024*1024+1)
    if len(data)>2*1024*1024 or hashlib.sha256(data).hexdigest()!=expected_digest:
        raise ValueError('Runtime integrity mismatch')
    return data


def extract_runtime(data,destination):
    root=Path(destination).resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries=archive.infolist();names=[entry.filename for entry in entries]
        if len(names)!=len(set(names)) or set(names)!=ALLOWED or sum(i.file_size for i in entries)>2*1024*1024:
            raise ValueError('Unexpected runtime contents')
        if any(stat.S_ISLNK(i.external_attr>>16) or i.is_dir() for i in entries):
            raise ValueError('Unsafe runtime archive')
        for entry in entries:
            content=archive.read(entry)
            if len(content)!=entry.file_size or len(content)>2*1024*1024:
                raise ValueError('Runtime size mismatch')
            path=root/entry.filename
            if not path.resolve().is_relative_to(root) or path.is_symlink():
                raise ValueError('Unsafe runtime path')
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
    manifest=json.loads((root/'manifest.json').read_text())
    if set(manifest.get('files',{}))!=ALLOWED-{'manifest.json'}:
        raise ValueError('Invalid runtime manifest')
    for name,digest in manifest['files'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:
            raise ValueError('Runtime member integrity mismatch')
    return root


def pair_worker(origin, token, key, runtime_digest):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args):return None
    body=json.dumps({'key':key,'runtimeSha':runtime_digest}).encode()
    request=urllib.request.Request(origin.rstrip('/')+'/api/production/worker/pair',data=body,headers={
        'OAI-Sites-Authorization':'Bearer '+token,'X-Production-Worker-Key':key,'Origin':origin.rstrip('/'),'Content-Type':'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request,timeout=30) as response:
        result=json.loads(response.read(4096))
    if result.get('paired') is not True:raise ValueError('Worker pairing failed')


def main():
    for name in ('ERP_BASE_URL','ERP_SITE_TOKEN','ERP_WORKER_KEY','ERP_RUNTIME_SHA256'):
        if not os.environ.get(name):raise SystemExit('Missing required server secret/config: '+name)
    try:
        data=download_runtime(os.environ['ERP_BASE_URL'],os.environ['ERP_SITE_TOKEN'],os.environ['ERP_RUNTIME_SHA256'],os.environ['ERP_WORKER_KEY'])
        root=extract_runtime(data,tempfile.mkdtemp(prefix='erp-production-runtime-'))
        pair_worker(os.environ['ERP_BASE_URL'],os.environ['ERP_SITE_TOKEN'],os.environ['ERP_WORKER_KEY'],os.environ['ERP_RUNTIME_SHA256'])
    except Exception:
        raise SystemExit('Private runtime download/verification failed; credentials are not logged')
    os.environ.update(ENGINE_ID='company-python-v1',ENGINE_MODULE='company_renderer',ENGINE_VERSION='company-renderer-20261116-v1')
    os.chdir(root)
    # Exec replaces this process, so the queue worker receives SIGTERM directly.
    os.execve(sys.executable,[sys.executable,str(root/'client.py')],os.environ)

if __name__=='__main__':main()
