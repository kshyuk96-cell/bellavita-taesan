"""AEAD recovery bundles behind a trusted storage interface; no remote defaults."""
from abc import ABC,abstractmethod
import base64,hashlib,json,os,struct,tempfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from .governance_durability import GovernanceDurability,MAX_DATABASE_BYTES,MAX_METADATA_BYTES,_directory,_read,_new,_path
from .maintenance_registry import name
from .store import Conflict,canonical_json

MAX_ENVELOPE_BYTES=MAX_DATABASE_BYTES+MAX_METADATA_BYTES+4096
MAGIC=b'TAESAN-AEAD-1\n'

def _guard_cleartext_labels(encoded,key):
    # Only cleartext labels/header are checked, never randomized ciphertext.
    variants={key,key.hex().encode(),key.hex().upper().encode()}
    for encoder in (base64.b64encode,base64.urlsafe_b64encode):
        value=encoder(key);variants.update((value,value.rstrip(b'=')))
    if any(value and value in encoded for value in variants):
        raise Conflict('backup labels contain confidential key material')


class BackupBackend(ABC):
    """Host-installed transport. No URLs, credentials or callbacks from clients."""
    @abstractmethod
    def put_if_absent(self,backup_id,payload):pass
    @abstractmethod
    def get(self,backup_id,max_bytes):pass

class PrivateLocalBackupBackend(BackupBackend):
    """Local proof/storage only; this is not external/offsite durability."""
    def __init__(self,directory):self.directory=_directory(directory)
    def put_if_absent(self,backup_id,payload):
        name(backup_id);_directory(self.directory)
        if type(payload) is not bytes or len(payload)>MAX_ENVELOPE_BYTES:raise ValueError('bounded encrypted payload required')
        with os.fdopen(_new(self.directory/(backup_id+'.aead')),'wb') as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
        fd=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return True
    def get(self,backup_id,max_bytes):
        name(backup_id)
        if type(max_bytes) is not int or not 1<=max_bytes<=MAX_ENVELOPE_BYTES:raise ValueError('bounded encrypted read required')
        return _read(self.directory/(backup_id+'.aead'),max_bytes)

class EncryptedBackup:
    def __init__(self,durability,backend,secret_provider,staging_root):
        if type(durability) is not GovernanceDurability or not isinstance(backend,BackupBackend) or not callable(secret_provider):raise ValueError('trusted recovery components required')
        self.durability,self.store,self.backend,self.secret_provider=durability,durability.store,backend,secret_provider
        self.staging_root=_directory(staging_root)
        with self.store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS encrypted_backups (backup_id TEXT PRIMARY KEY,key_id TEXT,actor_hash TEXT,state TEXT,ciphertext_digest TEXT,bytes INTEGER,updated REAL)')
    def _owner(self,token):return self.durability._global_owner(token)
    def _names(self,token,backup_id,key_id):
        name(backup_id);name(key_id)
        from .agent_directory import _reject_known_tokens
        with self.store._transaction() as c:
            self.store._authenticate(c,token);_reject_known_tokens(c,{'backup_id':backup_id,'key_id':key_id})
    def _key(self,key_id):
        try:value=self.secret_provider(key_id)
        except Exception:raise Conflict('backup encryption key unavailable') from None
        if type(value) is not bytes or len(value)!=32:raise Conflict('AES-256 key unavailable')
        return value
    def backup(self,token,backup_id,key_id):
        actor=self._owner(token);self._names(token,backup_id,key_id)
        key=self._key(key_id)
        _guard_cleartext_labels(canonical_json({'backup_id':backup_id,'key_id':key_id}).encode(),key)
        self._owner(token)
        with self.store._transaction() as c:
            self.store._authenticate(c,token)
            if c.execute('SELECT 1 FROM encrypted_backups WHERE backup_id=?',(backup_id,)).fetchone():raise Conflict('backup ID already used; explicit reconciliation required')
            c.execute("INSERT INTO encrypted_backups VALUES (?,?,?,'preparing',NULL,NULL,?)",(backup_id,key_id,actor['token_hash'],self.store._now()))
        try:
            _directory(self.staging_root)
            with tempfile.TemporaryDirectory(dir=self.staging_root,prefix='backup-') as stage:
                bundle=Path(stage)/'bundle';self.durability.backup(token,bundle)
                metadata=_read(bundle/'metadata.json',MAX_METADATA_BYTES);database=_read(bundle/'manager.sqlite3',MAX_DATABASE_BYTES)
                plaintext=struct.pack('>I',len(metadata))+metadata+database
                nonce=os.urandom(12)
                header=canonical_json({'version':1,'backup_id':backup_id,'key_id':key_id,'nonce':base64.b64encode(nonce).decode('ascii')}).encode()
                _guard_cleartext_labels(header,key)
                encrypted=AESGCM(key).encrypt(nonce,plaintext,MAGIC+header)
                payload=MAGIC+struct.pack('>I',len(header))+header+encrypted
                del key,plaintext,database
            if len(payload)>MAX_ENVELOPE_BYTES:raise Conflict('encrypted backup exceeds bound')
            checksum=hashlib.sha256(payload).hexdigest();self._owner(token)
            with self.store._transaction() as c:
                self.store._authenticate(c,token)
                c.execute("UPDATE encrypted_backups SET state='uploading',ciphertext_digest=?,bytes=?,updated=? WHERE backup_id=?",(checksum,len(payload),self.store._now(),backup_id))
            try:receipt=self.backend.put_if_absent(backup_id,payload)
            except Exception:raise Conflict('backup transport outcome uncertain') from None
            self._owner(token)
            if receipt is not True:raise Conflict('backup transport outcome uncertain')
            with self.store._transaction() as c:
                self.store._authenticate(c,token)
                c.execute("UPDATE encrypted_backups SET state='stored',updated=? WHERE backup_id=? AND state='uploading'",(self.store._now(),backup_id))
            return {'backup_id':backup_id,'state':'encrypted_stored','ciphertext_digest':checksum,'bytes':len(payload),'offsite_verified':False,'automatic_replay':False}
        except BaseException:
            with self.store._transaction() as c:c.execute("UPDATE encrypted_backups SET state='uncertain',updated=? WHERE backup_id=?",(self.store._now(),backup_id))
            raise
    def restore_rehearsal(self,token,backup_id,destination):
        self._owner(token);name(backup_id)
        with self.store._transaction() as c:
            row=c.execute("SELECT * FROM encrypted_backups WHERE backup_id=? AND state='stored'",(backup_id,)).fetchone()
            if row is None:raise Conflict('stored backup receipt required')
            row=dict(row)
        try:payload=self.backend.get(backup_id,MAX_ENVELOPE_BYTES)
        except Exception:raise Conflict('backup transport read unavailable') from None
        self._owner(token)
        if type(payload) is not bytes or len(payload)!=row['bytes'] or len(payload)>MAX_ENVELOPE_BYTES or hashlib.sha256(payload).hexdigest()!=row['ciphertext_digest']:raise Conflict('encrypted backup integrity mismatch')
        try:
            if not payload.startswith(MAGIC):raise ValueError()
            size=struct.unpack('>I',payload[len(MAGIC):len(MAGIC)+4])[0]
            if not 1<=size<=2048:raise ValueError()
            start=len(MAGIC)+4;header=payload[start:start+size]
            value=json.loads(header)
            if type(value) is not dict or set(value)!={'version','backup_id','key_id','nonce'} or type(value['version']) is not int or value['version']!=1 or value['backup_id']!=backup_id or value['key_id']!=row['key_id'] or canonical_json(value).encode()!=header:raise ValueError()
            nonce=base64.b64decode(value['nonce'],validate=True)
            if len(nonce)!=12:raise ValueError()
            plaintext=AESGCM(self._key(row['key_id'])).decrypt(nonce,payload[start+size:],MAGIC+header)
            size=struct.unpack('>I',plaintext[:4])[0]
            if not 1<=size<=MAX_METADATA_BYTES or not 1<=len(plaintext)-4-size<=MAX_DATABASE_BYTES:raise ValueError()
        except (ValueError,TypeError,KeyError,struct.error,InvalidTag):raise Conflict('encrypted backup authentication failed') from None
        self._owner(token);_directory(self.staging_root)
        with tempfile.TemporaryDirectory(dir=self.staging_root,prefix='restore-') as stage:
            bundle=Path(stage)/'bundle';bundle.mkdir(mode=0o700)
            for filename,data in [('metadata.json',plaintext[4:4+size]),('manager.sqlite3',plaintext[4+size:])]:
                with os.fdopen(_new(bundle/filename),'wb') as stream:stream.write(data)
            del plaintext
            result=self.durability.restore(token,bundle,destination)
        return {**result,'encrypted_backup_id':backup_id,'offsite_verified':False,'rehearsal_only':True}
