"""Independent authenticated receipts and explicit owner-granted offline recovery."""
from dataclasses import dataclass,asdict
from contextlib import closing
import base64,hashlib,hmac,json,math,os,sqlite3,struct,tempfile,time
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from .encrypted_backup import BackupBackend,EncryptedBackup,MAGIC,MAX_ENVELOPE_BYTES,_guard_cleartext_labels
from .governance_durability import _directory,_path,_read,_new,_readonly,_copy,_schema,MAX_DATABASE_BYTES,MAX_METADATA_BYTES
from .maintenance_registry import name
from .store import Conflict,PermissionDenied,canonical_json

def _decode(data,limit=65536):
    if type(data) is not bytes or len(data)>limit:raise Conflict('bounded authenticated recovery record required')
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError()
            value[key]=item
        return value
    try:return json.loads(data,object_pairs_hook=pairs)
    except (ValueError,TypeError,UnicodeError):raise Conflict('invalid recovery record') from None

@dataclass(frozen=True)
class RecoveryGrant:
    scope:str
    owner_subject:str
    issued_at:float
    catalog_id:str
    destination:str
    expires_at:float
    nonce:str
    signature:str

class RecoveryTrust:
    """Host-installed separate recovery authority, independent of manager DB."""
    def __init__(self,scope,trust_key_provider,owner_authorizer,owner_validator,*,clock=time.time):
        name(scope)
        if not all(callable(v) for v in (trust_key_provider,owner_authorizer,owner_validator,clock)):raise ValueError('trusted recovery providers required')
        self.scope,self.provider,self.authorizer,self.validator,self.clock=scope,trust_key_provider,owner_authorizer,owner_validator,clock
    def key(self):
        try:key=self.provider()
        except Exception:raise Conflict('recovery trust key unavailable') from None
        if type(key) is not bytes or len(key)!=32:raise Conflict('separate recovery trust key required')
        return key
    def sign(self,domain,value):
        encoded=canonical_json(value).encode();key=self.key();_guard_cleartext_labels(encoded,key)
        return hmac.new(key,domain+b'\0'+encoded,hashlib.sha256).hexdigest()
    def issue_grant(self,owner_assertion,catalog_id,destination,*,ttl=300):
        name(catalog_id)
        if type(ttl) is not int or not 1<=ttl<=300:raise ValueError('bounded recovery grant required')
        try:authorized=self.authorizer(owner_assertion,self.scope)
        except Exception:authorized=False
        if type(authorized) is not str:raise PermissionDenied('explicit owner recovery authorization required')
        name(authorized)
        target=_path(destination);_directory(target.parent)
        if target.exists():raise Conflict('new recovery destination required')
        issued_at=self.clock()
        if type(issued_at) not in (int,float) or not math.isfinite(issued_at) or issued_at<=0:
            raise PermissionDenied('finite recovery clock required')
        value={'scope':self.scope,'owner_subject':authorized,'issued_at':issued_at,'catalog_id':catalog_id,'destination':str(target),'expires_at':issued_at+ttl,'nonce':os.urandom(16).hex()}
        grant=RecoveryGrant(**value,signature=self.sign(b'grant-v1',value))
        self.verify(grant)
        return grant
    def verify(self,grant):
        if type(grant) is not RecoveryGrant:raise PermissionDenied('typed owner recovery grant required')
        value=asdict(grant);signature=value.pop('signature')
        if type(grant.expires_at) not in (int,float) or not math.isfinite(grant.expires_at) or type(grant.issued_at) not in (int,float) or not math.isfinite(grant.issued_at) or grant.issued_at>self.clock() or not 0<grant.expires_at-grant.issued_at<=300:raise PermissionDenied('current bounded recovery grant required')
        name(grant.owner_subject)
        try:current=self.validator(grant.owner_subject,self.scope)
        except Exception:current=False
        if current is not True:raise PermissionDenied('owner recovery authority unavailable')
        if grant.scope!=self.scope or type(grant.expires_at) not in (int,float) or not math.isfinite(grant.expires_at) or not 0<grant.expires_at-self.clock()<=300:raise PermissionDenied('current scoped recovery grant required')
        name(grant.catalog_id)
        if type(grant.nonce) is not str or len(grant.nonce)!=32 or any(c not in '0123456789abcdef' for c in grant.nonce):raise PermissionDenied('valid recovery nonce required')
        if type(signature) is not str or len(signature)!=64 or any(c not in '0123456789abcdef' for c in signature) or not hmac.compare_digest(signature,self.sign(b'grant-v1',value)):raise PermissionDenied('authenticated owner recovery grant required')
        try:current=self.validator(grant.owner_subject,self.scope)
        except Exception:current=False
        if current is not True or not 0<grant.expires_at-self.clock()<=300:raise PermissionDenied('owner recovery authority changed')
        return _path(grant.destination)

class RecoveryCatalog:
    def __init__(self,trust,catalog_backend,object_backend,encryption_key_provider,staging_root):
        if type(trust) is not RecoveryTrust or not isinstance(catalog_backend,BackupBackend) or not isinstance(object_backend,BackupBackend) or not callable(encryption_key_provider):raise ValueError('trusted independent recovery components required')
        self.trust,self.catalog,self.objects,self.keys,self.staging=trust,catalog_backend,object_backend,encryption_key_provider,_directory(staging_root)
    def _encryption_key(self,key_id):
        try:key=self.keys(key_id)
        except Exception:raise Conflict('recovery encryption key unavailable') from None
        if type(key) is not bytes or len(key)!=32 or hmac.compare_digest(key,self.trust.key()):raise Conflict('separate encryption and trust keys required')
        return key
    def publish(self,owner,encrypted,backup_id):
        if type(encrypted) is not EncryptedBackup or encrypted.backend is not self.objects:raise ValueError('exact registered backup backend required')
        name(backup_id);encrypted._owner(owner)
        with encrypted.store._transaction() as c:
            row=c.execute("SELECT * FROM encrypted_backups WHERE backup_id=? AND state='stored'",(backup_id,)).fetchone()
            if row is None:raise Conflict('stored encrypted backup receipt required')
            row=dict(row)
        value={'version':1,'scope':self.trust.scope,'backup_id':backup_id,'key_id':row['key_id'],'ciphertext_digest':row['ciphertext_digest'],'bytes':row['bytes']}
        encoded=canonical_json(value).encode();_guard_cleartext_labels(encoded,self._encryption_key(row['key_id']))
        record=canonical_json({'receipt':value,'signature':self.trust.sign(b'receipt-v1',value)}).encode()
        encrypted._owner(owner)
        try:stored=self.catalog.put_if_absent('receipt-'+backup_id,record)
        except Exception:raise Conflict('recovery catalog publication uncertain') from None
        encrypted._owner(owner)
        if stored is not True:raise Conflict('recovery catalog publication uncertain')
        return {'backup_id':backup_id,'state':'authenticated_catalog_receipt_stored','offsite_verified':False}
    def restore(self,grant):
        target=self.trust.verify(grant);_directory(target.parent)
        if any(p.exists() for p in (target,*[Path(str(target)+s) for s in ('-wal','-shm','-journal')])):raise Conflict('new recovery destination required')
        try:record=_decode(self.catalog.get('receipt-'+grant.catalog_id,65536))
        except Exception:raise Conflict('recovery catalog unavailable or malformed') from None
        if type(record) is not dict or set(record)!={'receipt','signature'} or type(record['receipt']) is not dict:raise Conflict('authenticated recovery receipt required')
        meta=record['receipt']
        if set(meta)!={'version','scope','backup_id','key_id','ciphertext_digest','bytes'} or type(meta['version']) is not int or meta['version']!=1 or meta['scope']!=self.trust.scope or meta['backup_id']!=grant.catalog_id:raise Conflict('recovery receipt scope mismatch')
        if type(record['signature']) is not str or len(record['signature'])!=64 or any(c not in '0123456789abcdef' for c in record['signature']) or not hmac.compare_digest(record['signature'],self.trust.sign(b'receipt-v1',meta)):raise Conflict('recovery receipt authentication failed')
        name(meta['key_id'])
        if type(meta['bytes']) is not int or not 1<=meta['bytes']<=MAX_ENVELOPE_BYTES:raise Conflict('bounded recovery object required')
        try:payload=self.objects.get(grant.catalog_id,MAX_ENVELOPE_BYTES)
        except Exception:raise Conflict('recovery object unavailable') from None
        if type(payload) is not bytes or len(payload)!=meta['bytes'] or hashlib.sha256(payload).hexdigest()!=meta['ciphertext_digest']:raise Conflict('recovery object integrity mismatch')
        try:
            if not payload.startswith(MAGIC):raise ValueError()
            offset=len(MAGIC)+4;size=struct.unpack('>I',payload[len(MAGIC):offset])[0]
            if not 1<=size<=2048:raise ValueError()
            header=payload[offset:offset+size];value=_decode(header,2048)
            if type(value) is not dict or set(value)!={'version','backup_id','key_id','nonce'} or type(value['version']) is not int or value['version']!=1 or value['backup_id']!=grant.catalog_id or value['key_id']!=meta['key_id'] or canonical_json(value).encode()!=header:raise ValueError()
            nonce=base64.b64decode(value['nonce'],validate=True)
            if len(nonce)!=12:raise ValueError()
            key=self._encryption_key(meta['key_id']);_guard_cleartext_labels(header,key)
            plaintext=AESGCM(key).decrypt(nonce,payload[offset+size:],MAGIC+header)
            count=struct.unpack('>I',plaintext[:4])[0]
            if not 1<=count<=MAX_METADATA_BYTES or not 1<=len(plaintext)-4-count<=MAX_DATABASE_BYTES:raise ValueError()
            metadata=_decode(plaintext[4:4+count]);database=plaintext[4+count:]
            if type(metadata) is not dict or set(metadata)!={'version','database_sha256','database_bytes','schema_digest','leader_epoch','recorded_at'} or type(metadata['version']) is not int or metadata['version']!=1 or type(metadata['leader_epoch']) is not int or metadata['leader_epoch']<0 or type(metadata['recorded_at']) not in (int,float) or not math.isfinite(metadata['recorded_at']) or len(database)!=metadata['database_bytes'] or hashlib.sha256(database).hexdigest()!=metadata['database_sha256']:raise ValueError()
        except (ValueError,TypeError,KeyError,struct.error,InvalidTag):raise Conflict('authenticated recovery payload invalid') from None
        # Single-use grant is consumed outside the missing manager DB. A failed
        # recovery requires a fresh owner grant; no automatic execution/replay.
        self.trust.verify(grant)
        try:consumed=self.catalog.put_if_absent('grant-'+grant.nonce,b'consumed')
        except Exception:raise Conflict('recovery grant consumed or outcome uncertain') from None
        if consumed is not True:raise Conflict('recovery grant consumption uncertain')
        self.trust.verify(grant)
        _directory(self.staging)
        with tempfile.TemporaryDirectory(dir=self.staging,prefix='recovery-') as stage:
            source=Path(stage)/'manager.sqlite3'
            with os.fdopen(_new(source),'wb') as stream:stream.write(database)
            del plaintext,database
            os.close(_new(target))
            try:
                with closing(_readonly(source)) as original,closing(sqlite3.connect(target)) as restored:
                    schema,tables=_schema(original)
                    epoch=original.execute('SELECT COALESCE(MAX(epoch),0) FROM continuous_leader').fetchone()[0] if 'continuous_leader' in tables else 0
                    if schema!=metadata['schema_digest'] or epoch!=metadata['leader_epoch']:raise Conflict('recovery schema or epoch mismatch')
                    _copy(original,restored)
                    if 'continuous_actions' in tables:restored.execute("UPDATE continuous_actions SET state='uncertain',reason='restore_uncertain' WHERE state='running'")
                    if 'continuous_leader' in tables:restored.execute('UPDATE continuous_leader SET expires=0,epoch=epoch+1')
                    if 'continuous_tick_guard' in tables:restored.execute('DELETE FROM continuous_tick_guard')
                    restored.execute('CREATE TABLE IF NOT EXISTS governance_restore_marker (backup_digest TEXT,reconciliation_required INTEGER,recorded REAL)')
                    restored.execute('INSERT INTO governance_restore_marker VALUES (?,?,?)',(metadata['database_sha256'],1,self.trust.clock()));restored.commit()
                self.trust.verify(grant)
                with target.open('rb') as stream:os.fsync(stream.fileno())
            except BaseException:
                for path in (target,*[Path(str(target)+s) for s in ('-wal','-shm','-journal')]):
                    if path.exists():path.unlink()
                raise
        return {'state':'restored_requires_verification','original_manager_database_required':False,'reconciliation_required':True,'automatic_replay':False,'offsite_verified':False}
