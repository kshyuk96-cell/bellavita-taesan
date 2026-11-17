"""Create-only bounded chunk transport for opaque encrypted backup envelopes.

Hashes detect inconsistency, not authenticated authorship. The outer AEAD and
independent recovery catalog remain required. Unknown uploads are not replayed.
"""
import hashlib
import json
import re
import secrets
from .encrypted_backup import BackupBackend,MAX_ENVELOPE_BYTES
from .maintenance_registry import name
from .store import Conflict,canonical_json

MANIFEST_LIMIT=65536
MAX_CHUNK_BYTES=1024*1024


def _hash(data):return hashlib.sha256(data).hexdigest()


def _json(raw):
    if type(raw) is not bytes or len(raw)>MANIFEST_LIMIT:raise Conflict('bounded chunk manifest required')
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError()
            value[key]=item
        return value
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError):raise Conflict('invalid chunk manifest') from None


class ChunkedBackupBackend(BackupBackend):
    def __init__(self,backend,*,chunk_bytes=512*1024,max_parts=64,max_payload_bytes=32*1024*1024):
        if (not isinstance(backend,BackupBackend) or type(chunk_bytes) is not int or not 1<=chunk_bytes<=MAX_CHUNK_BYTES
                or type(max_parts) is not int or not 1<=max_parts<=256
                or type(max_payload_bytes) is not int or not 1<=max_payload_bytes<=min(MAX_ENVELOPE_BYTES,chunk_bytes*max_parts)):
            raise ValueError('bounded registered chunk backend required')
        self.backend,self.chunk_bytes,self.max_parts,self.max_payload_bytes=backend,chunk_bytes,max_parts,max_payload_bytes

    @staticmethod
    def _id(backup_id):
        name(backup_id)
        if backup_id.startswith(('part-','claim-')):raise ValueError('reserved chunk namespace')
        return backup_id

    def put_if_absent(self,backup_id,payload):
        self._id(backup_id)
        if type(payload) is not bytes or not 1<=len(payload)<=self.max_payload_bytes:raise ValueError('bounded encrypted envelope required')
        nonce=secrets.token_hex(16)
        claim='claim-'+_hash(backup_id.encode())[:48]
        # This immutable claim survives a partial transfer. No retry can silently
        # generate a second set of parts under the same logical backup ID.
        if self.backend.put_if_absent(claim,canonical_json({'version':1,'backup_id':backup_id,'nonce':nonce}).encode()) is not True:
            raise Conflict('chunk reservation unverified')
        parts=[]
        for index,start in enumerate(range(0,len(payload),self.chunk_bytes)):
            part=payload[start:start+self.chunk_bytes]
            key='part-'+_hash(canonical_json([backup_id,nonce,index]).encode())[:48]
            if self.backend.put_if_absent(key,part) is not True:raise Conflict('chunk upload unverified')
            parts.append({'id':key,'bytes':len(part),'sha256':_hash(part)})
        manifest=canonical_json({'version':1,'bytes':len(payload),'sha256':_hash(payload),'parts':parts}).encode()
        if len(manifest)>MANIFEST_LIMIT:raise Conflict('chunk manifest exceeds bound')
        # Publication is the final create-only write. Partial objects are retained
        # for reconciliation and never mistaken for a completed backup.
        if self.backend.put_if_absent(backup_id,manifest) is not True:raise Conflict('chunk publication unverified')
        return True

    def get(self,backup_id,max_bytes):
        self._id(backup_id)
        if type(max_bytes) is not int or max_bytes<1:raise ValueError('bounded envelope read required')
        limit=min(max_bytes,self.max_payload_bytes)
        manifest_raw=self.backend.get(backup_id,MANIFEST_LIMIT);m=_json(manifest_raw)
        if (type(m) is not dict or set(m)!={'version','bytes','sha256','parts'} or type(m['version']) is not int or m['version']!=1
                or type(m['bytes']) is not int or not 1<=m['bytes']<=limit
                or type(m['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',m['sha256'])
                or type(m['parts']) is not list or not 1<=len(m['parts'])<=self.max_parts):raise Conflict('bounded exact chunk manifest required')
        total=0;seen=set()
        for part in m['parts']:
            if type(part) is not dict or set(part)!={'id','bytes','sha256'}:raise Conflict('exact part metadata required')
            key=part['id']
            if (type(key) is not str or not key.startswith('part-') or len(key)!=53 or any(c not in '0123456789abcdef' for c in key[5:])
                    or key in seen or type(part['bytes']) is not int or not 1<=part['bytes']<=self.chunk_bytes
                    or type(part['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',part['sha256'])):raise Conflict('bounded distinct chunk required')
            seen.add(key);total+=part['bytes']
        if total!=m['bytes']:raise Conflict('chunk total mismatch')
        assembled=bytearray()
        for part in m['parts']:
            value=self.backend.get(part['id'],part['bytes'])
            if type(value) is not bytes or len(value)!=part['bytes'] or _hash(value)!=part['sha256']:raise Conflict('chunk integrity mismatch')
            assembled.extend(value)
        result=bytes(assembled)
        if _hash(result)!=m['sha256']:raise Conflict('envelope integrity mismatch')
        # Recheck the publication after the last remote read; no mutable manifest
        # can swap the intended envelope silently during reconstruction.
        if self.backend.get(backup_id,MANIFEST_LIMIT)!=manifest_raw:raise Conflict('chunk publication changed')
        return result
