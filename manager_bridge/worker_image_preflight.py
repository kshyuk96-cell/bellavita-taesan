"""Trusted local image contexts and observations; never a push/deploy mechanism."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from .continuous_host import _git
from .store import digest

BASE_IMAGE='python:3.13.5-slim-bookworm@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419'
ROLES={'control':10001,'developer':10002,'reviewer':10003}
MAX_CONTEXT_BYTES=512*1024*1024
MAX_CONTEXT_FILES=50000


def _physical(path):
    value=Path(path).absolute()
    if '..' in value.parts or any(p.is_symlink() for p in (value,*value.parents)) or not value.is_dir():raise ValueError('physical source directory required')
    return value


def prepare_build_context(*,source,expected_commit,role,destination,codex_binary=None):
    source=_physical(source)
    if role not in ROLES or type(expected_commit) is not str or not re.fullmatch('[a-f0-9]{40}',expected_commit):raise ValueError('fixed role and exact application commit required')
    if _git(source,'rev-parse','HEAD').decode().strip()!=expected_commit:raise ValueError('application pin differs')
    target=Path(destination).absolute()
    if target.exists() or target.is_symlink() or '..' in target.parts or any(p.is_symlink() for p in target.parents):raise ValueError('new physical build context required')
    target.mkdir(mode=0o700)
    # Local shallow Git fetch copies only committed bytes and preserves the real
    # commit identity. It never forwards owner Git config/auth into an image.
    _git(source,'clone','--no-local','--depth','1','file://'+str(source),str(target/'app'))
    app=target/'app';_git(app,'checkout','--detach',expected_commit);_git(app,'remote','remove','origin')
    if _git(app,'status','--porcelain'):raise ValueError('committed application context required')
    tracked=_git(app,'ls-files','-z').decode().split('\0')
    for name in filter(None,tracked):
        parts=Path(name).parts
        if any(part in ('auth.json','.env','.aws','.codex') or part.endswith(('.token','.sqlite','.sqlite3')) for part in parts):raise ValueError('credential/state input forbidden')
    recipes=source/'ops'/'worker-images'
    shutil.copyfile(recipes/(role+'.Dockerfile'),target/'Dockerfile');shutil.copyfile(recipes/'context.dockerignore',target/'.dockerignore')
    codex_sha='not-included'
    if role!='control':
        binary=Path(codex_binary) if codex_binary is not None else None
        if binary is None or binary.is_symlink() or not binary.is_file() or binary.stat().st_size>400*1024*1024:raise ValueError('bounded trusted Codex artifact required')
        shutil.copyfile(binary,target/'codex');(target/'codex').chmod(0o555)
        with (target/'codex').open('rb') as f:codex_sha=hashlib.file_digest(f,'sha256').hexdigest()
    total=0;records=[]
    for item in sorted(target.rglob('*')):
        if item.is_symlink():raise ValueError('build context links forbidden')
        if item.is_file():
            total+=item.stat().st_size
            if total>MAX_CONTEXT_BYTES or len(records)>=MAX_CONTEXT_FILES:raise ValueError('bounded build context required')
            with item.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
            records.append([item.relative_to(target).as_posix(),sha,item.stat().st_size])
        elif not item.is_dir():raise ValueError('build context special files forbidden')
    value={'role':role,'application_commit':expected_commit,'base_image':BASE_IMAGE,'codex_sha256':codex_sha,'context_bytes':total,'context_file_count':len(records),'context_digest':digest(records),'credentials_included':False,'images_built':False,'images_pushed':False}
    (target/'context-manifest.json').write_text(json.dumps(value,sort_keys=True)+'\n');(target/'context-manifest.json').chmod(0o600)
    return value


def inspect_local_image(image_id,*,expected_role,expected_commit,docker='docker'):
    if not re.fullmatch('sha256:[a-f0-9]{64}',image_id) or expected_role not in ROLES or not re.fullmatch('[a-f0-9]{40}',expected_commit):raise ValueError('exact local image digest and registered role/commit required')
    raw=subprocess.run([docker,'image','inspect',image_id],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15)
    if raw.returncode or len(raw.stdout)>262144:raise ValueError('bounded local image observation unavailable')
    rows=json.loads(raw.stdout)
    if type(rows) is not list or len(rows)!=1:raise ValueError('single local image required')
    value=rows[0];config=value.get('Config',{});labels=config.get('Labels') or {}
    matches=(value.get('Id')==image_id and labels.get('org.opencontainers.image.revision')==expected_commit and labels.get('io.taesan.worker.role')==expected_role and config.get('User')==str(ROLES[expected_role])+':'+str(ROLES[expected_role]))
    return {'scope':'observed_local_image_metadata','image_config_digest':value.get('Id'),'registry_manifest_digest':None,'expected_bindings_match':matches,'role':expected_role,'application_commit':expected_commit,'nonroot_user':config.get('User'),'runtime_probe_performed':False,'isolation_verified':False,'credentials_mounted':False,'images_pushed':False,'deployment_performed':False}


def probe_local_image(image_id,*,expected_role,expected_commit,docker='docker'):
    """Fixed version/namespace checks with no network, auth, source task or model."""
    result=inspect_local_image(image_id,expected_role=expected_role,expected_commit=expected_commit,docker=docker)
    if not result['expected_bindings_match']:raise ValueError('local image bindings differ')
    program="""import json,os,platform,shutil,subprocess
result={'uid':os.getuid(),'python':platform.python_version()}
for name in ('git','bwrap','codex'):
 path=shutil.which(name)
 if path:
  value=subprocess.run([path,'--version'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
  result[name]={'available':True,'exit_code':value.returncode,'version':value.stdout[:256].decode('utf-8','replace').strip()}
 else:result[name]={'available':False}
namespace=['/usr/bin/bwrap','--unshare-all','--die-with-parent','--new-session','--ro-bind','/usr','/usr','--symlink','usr/bin','/bin','--symlink','usr/lib','/lib']
if os.path.isdir('/usr/lib64'):namespace+=['--symlink','usr/lib64','/lib64']
namespace+=['--proc','/proc','--dev','/dev','--tmpfs','/tmp','--clearenv','/usr/bin/true']
p=subprocess.run(namespace,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=5)
result['namespace_isolation_verified']=p.returncode==0
result['namespace_probe_exit_code']=p.returncode
result['namespace_probe_failure_category']='permission_denied' if b'Operation not permitted' in p.stderr else ('none' if p.returncode==0 else 'unavailable')
print(json.dumps(result))
"""
    command=[docker,'run','--rm','--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=64','--memory=512m','--cpus=1','--tmpfs=/tmp:rw,noexec,nosuid,size=16m','--entrypoint=/usr/local/bin/python',image_id,'-I','-B','-c',program]
    observed=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30)
    if observed.returncode or len(observed.stdout)>16384:
        return dict(result,runtime_probe_performed=True,runtime_probe_state='unavailable',isolation_verified=False)
    facts=json.loads(observed.stdout)
    result.update(runtime_probe_performed=True,runtime_probe_state='observed',runtime=facts,isolation_verified=facts.get('namespace_isolation_verified') is True)
    return result
