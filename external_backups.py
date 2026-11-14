"""Encrypted external backup envelopes and opt-in Google Drive transfer."""
import hashlib
import datetime as dt
import io
import json
import os
from pathlib import Path,PurePosixPath
import re
import tarfile
import tempfile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import manage_data

MAGIC=b'BVBACKUP1\n'
MAX_BYTES=64*1024*1024


def setup(c):
    c.execute("CREATE TABLE IF NOT EXISTS shop_external_backups(id TEXT PRIMARY KEY,provider TEXT NOT NULL,sha256 TEXT NOT NULL,verified TEXT NOT NULL,mode TEXT NOT NULL)")


def record(c,fields):
    identity=fields.get('file_id','');digest=fields.get('sha256','')
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',identity) or not re.fullmatch(r'[a-f0-9]{64}',digest):raise ValueError('외부 백업 식별자와 체크섬을 확인하세요.')
    mode=fields.get('mode','manual_verified')
    if mode not in ('manual_verified','automatic_verified'):raise ValueError('백업 검증 방식을 확인하세요.')
    prior=c.execute('SELECT sha256 FROM shop_external_backups WHERE id=?',(identity,)).fetchone()
    if prior and prior['sha256']!=digest:raise ValueError('기록된 백업 체크섬이 다릅니다.')
    c.execute('INSERT OR IGNORE INTO shop_external_backups VALUES (?,?,?,?,?)',(identity,'google_drive',digest,dt.datetime.now(dt.timezone.utc).isoformat(),mode))


def key_bytes(value=None):
    value=os.getenv('SHOP_BACKUP_ENCRYPTION_KEY','') if value is None else value
    if not re.fullmatch(r'[a-fA-F0-9]{64}',value):raise ValueError('백업 암호화 키 설정을 확인하세요.')
    return bytes.fromhex(value)


def configuration():
    fields={'encryption_key':bool(re.fullmatch(r'[a-fA-F0-9]{64}',os.getenv('SHOP_BACKUP_ENCRYPTION_KEY',''))),
        'drive_folder':bool(os.getenv('SHOP_BACKUP_DRIVE_FOLDER','')),
        'server_credentials':Path(os.getenv('SHOP_BACKUP_DRIVE_CREDENTIALS','/nonexistent')).is_file()}
    return {'enabled':os.getenv('SHOP_EXTERNAL_BACKUP_ENABLED')=='1','configured':all(fields.values()),'fields':fields}


def pack(snapshot,key=None):
    root=Path(snapshot).resolve();manifest=json.loads((root/'backup-manifest.json').read_text())
    if manifest!={'version':1,'sha256':manage_data.inventory(root)}:raise ValueError('백업 체크섬이 일치하지 않습니다.')
    if sum(p.stat().st_size for p in root.rglob('*') if p.is_file())>MAX_BYTES:raise ValueError('백업은 64MB 이내로 분할 설정이 필요합니다.')
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as archive:
        for p in sorted(root.rglob('*')):
            if p.is_symlink():raise ValueError('백업에 링크를 포함할 수 없습니다.')
            archive.add(p,arcname=p.relative_to(root).as_posix(),recursive=False)
    nonce=os.urandom(12)
    content=MAGIC+nonce+AESGCM(key_bytes(key)).encrypt(nonce,stream.getvalue(),MAGIC)
    if len(content)>MAX_BYTES:raise ValueError('암호화 백업 크기 한도를 초과했습니다.')
    return content


def restore(content,destination,key=None):
    if not content.startswith(MAGIC) or len(content)>MAX_BYTES:raise ValueError('암호화 백업 형식·크기를 확인하세요.')
    try:raw=AESGCM(key_bytes(key)).decrypt(content[len(MAGIC):len(MAGIC)+12],content[len(MAGIC)+12:],MAGIC)
    except Exception:raise ValueError('백업 인증·암호화 키를 확인하세요.') from None
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('복구 대상은 새 디렉터리여야 합니다.')
    with tempfile.TemporaryDirectory(prefix='bv-external-restore-') as temporary:
        root=Path(temporary)/'snapshot';root.mkdir()
        with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
            members=archive.getmembers();names=set();total=0
            for member in members:
                path=PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts or member.name in names or not (member.isfile() or member.isdir()):raise ValueError('안전하지 않은 백업 항목입니다.')
                names.add(member.name);total+=member.size
                if total>MAX_BYTES or len(names)>10000:raise ValueError('복구 크기·항목 한도를 초과했습니다.')
            archive.extractall(root,filter='data')
        manage_data.transfer(root,destination,restore=True)
    return manage_data.inspect(destination)


def upload(snapshot):
    config=configuration()
    if not config['enabled'] or not config['configured']:return {'state':'not_configured'}
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    content=pack(snapshot);folder=os.environ['SHOP_BACKUP_DRIVE_FOLDER']
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',folder):raise ValueError('Drive 백업 폴더를 확인하세요.')
    credentials=Credentials.from_service_account_file(os.environ['SHOP_BACKUP_DRIVE_CREDENTIALS'],scopes=['https://www.googleapis.com/auth/drive.file'])
    session=AuthorizedSession(credentials)
    name=Path(snapshot).name+'.bvbackup';boundary='bellavita-'+os.urandom(16).hex()
    metadata=json.dumps({'name':name,'parents':[folder]},ensure_ascii=False).encode()
    body=(('--'+boundary+'\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n').encode()+metadata+('\r\n--'+boundary+'\r\nContent-Type: application/octet-stream\r\n\r\n').encode()+content+('\r\n--'+boundary+'--\r\n').encode())
    response=session.post('https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,size',headers={'Content-Type':'multipart/related; boundary='+boundary},data=body,timeout=30)
    response.raise_for_status();identity=response.json()['id']
    returned=session.get('https://www.googleapis.com/drive/v3/files/'+identity+'?alt=media',timeout=30);returned.raise_for_status()
    if len(returned.content)>MAX_BYTES or hashlib.sha256(returned.content).digest()!=hashlib.sha256(content).digest():raise ValueError('외부 백업 재다운로드 체크섬이 다릅니다.')
    with tempfile.TemporaryDirectory(prefix='bv-drive-drill-') as tmp:restore(returned.content,Path(tmp)/'restored')
    return {'state':'verified','provider':'google_drive','file_id':identity,'sha256':hashlib.sha256(content).hexdigest(),'encrypted':True,'restore_drill':'passed'}


def main():
    import argparse
    import getpass
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['restore'])
    parser.add_argument('--file',required=True)
    parser.add_argument('--destination',required=True)
    args=parser.parse_args()
    key=os.getenv('SHOP_BACKUP_ENCRYPTION_KEY') or getpass.getpass('백업 복구 키: ')
    content=Path(args.file).read_bytes()
    report=restore(content,args.destination,key)
    print(json.dumps({'integrity':report['integrity_check'],'counts':report['counts']},ensure_ascii=False))


if __name__=='__main__':main()
