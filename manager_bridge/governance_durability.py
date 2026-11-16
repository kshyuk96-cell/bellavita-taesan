"""Private SQLite recovery copies and scoped credential-expiry observations.

No credential refresh, message delivery, worker termination, or replay occurs.
"""
import hashlib
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from .store import Conflict,PermissionDenied,canonical_json,digest
from .maintenance_registry import name
from .three_tier import ThreeTier

MAX_DATABASE_BYTES=256*1024*1024
MAX_METADATA_BYTES=65536


def _path(path):
    value=Path(path).absolute()
    if '..' in value.parts or any(p.is_symlink() for p in (value,*value.parents)):
        raise ValueError('physical private path required')
    return value


def _directory(path):
    value=_path(path);info=value.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('owned private directory required')
    return value


def _read(path,limit):
    path=_path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>limit:
            raise ValueError('bounded owned private file required')
        data=stream.read(limit+1)
    if len(data)>limit:raise ValueError('bounded private file required')
    return data


def _new(path):
    return os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)


def _schema(connection):
    rows=connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchmany(5001)
    if len(rows)>5000 or sum(len(row[3] or '') for row in rows)>1024*1024:raise ValueError('bounded manager schema required')
    tables={row[1] for row in rows if row[0]=='table'}
    if not {'bridge_tokens','bridge_tasks','bridge_projects'}<=tables:raise ValueError('manager database required')
    return digest([list(row) for row in rows]),tables


def _copy(source,destination):
    started=time.monotonic()
    def progress(status,remaining,total):
        if total*source.execute('PRAGMA page_size').fetchone()[0]>MAX_DATABASE_BYTES or time.monotonic()-started>30:
            raise ValueError('bounded SQLite backup required')
    source.backup(destination,pages=256,progress=progress,sleep=0.01)
    destination.execute('PRAGMA journal_mode=DELETE')
    if destination.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('valid SQLite backup required')


def _readonly(path):
    return sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)


class GovernanceDurability:
    def __init__(self,core,*,expiration_provider=None):
        if not isinstance(core,ThreeTier):raise ValueError('registered core required')
        if expiration_provider is not None and not callable(expiration_provider):raise ValueError('trusted expiry observer required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.expiration_provider=expiration_provider
        with self.store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS governance_auth_notices (dedupe TEXT PRIMARY KEY,project TEXT,member_id TEXT,kind TEXT,state TEXT,recorded REAL)')

    def _global_owner(self,token):
        with closing(_readonly(self.store.path)) as c:
            c.row_factory=sqlite3.Row
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner',))
            if actor['projects']!=['*']:raise PermissionDenied('global owner required for complete database recovery')
            return actor

    def backup(self,token,destination):
        self._global_owner(token)
        target=_path(destination);_directory(target.parent)
        target.mkdir(mode=0o700)  # No existing directories, symlinks or archives.
        database=target/'manager.sqlite3';metadata=target/'metadata.json'
        try:
            os.close(_new(database))
            with closing(_readonly(self.store.path)) as source,closing(sqlite3.connect(database)) as copied:
                _copy(source,copied)
                schema,tables=_schema(copied)
                epoch=copied.execute('SELECT COALESCE(MAX(epoch),0) FROM continuous_leader').fetchone()[0] if 'continuous_leader' in tables else 0
            data=_read(database,MAX_DATABASE_BYTES)
            self._global_owner(token)
            result={'version':1,'database_sha256':hashlib.sha256(data).hexdigest(),'database_bytes':len(data),
                    'schema_digest':schema,'leader_epoch':epoch,'recorded_at':self.store._now()}
            with os.fdopen(_new(metadata),'w') as stream:
                stream.write(canonical_json(result));stream.flush();os.fsync(stream.fileno())
            with database.open('rb') as stream:os.fsync(stream.fileno())
            return {**result,'state':'private_backup_complete','credentials_refreshed':False,'automatic_replay':False}
        except BaseException:
            for path in (database,metadata,Path(str(database)+'-wal'),Path(str(database)+'-shm'),Path(str(database)+'-journal')):
                if path.exists():path.unlink()
            target.rmdir();raise

    def restore(self,token,backup_directory,destination):
        self._global_owner(token)
        bundle=_directory(backup_directory)
        names=set()
        for entry in bundle.iterdir():
            names.add(entry.name)
            if len(names)>2:raise ValueError('exact bounded backup bundle required')
        if names!={'manager.sqlite3','metadata.json'}:raise ValueError('exact bounded backup bundle required')
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:raise ValueError('duplicate backup metadata')
                result[key]=value
            return result
        meta=json.loads(_read(bundle/'metadata.json',MAX_METADATA_BYTES),object_pairs_hook=pairs)
        keys={'version','database_sha256','database_bytes','schema_digest','leader_epoch','recorded_at'}
        if type(meta) is not dict or set(meta)!=keys or type(meta['version']) is not int or meta['version']!=1:
            raise ValueError('exact backup metadata required')
        if type(meta['leader_epoch']) is not int or meta['leader_epoch']<0 or type(meta['recorded_at']) not in (int,float) or not math.isfinite(meta['recorded_at']):raise ValueError('valid recovery epoch required')
        data=_read(bundle/'manager.sqlite3',MAX_DATABASE_BYTES)
        if len(data)!=meta['database_bytes'] or hashlib.sha256(data).hexdigest()!=meta['database_sha256']:raise Conflict('backup integrity mismatch')
        target=_path(destination);_directory(target.parent)
        if any(path.exists() for path in (target,Path(str(target)+'-wal'),Path(str(target)+'-shm'),Path(str(target)+'-journal'))):
            raise ValueError('new recovery destination required')
        os.close(_new(target))
        try:
            with closing(_readonly(bundle/'manager.sqlite3')) as source:
                schema,tables=_schema(source)
                if schema!=meta['schema_digest']:raise Conflict('backup schema mismatch')
                epoch=source.execute('SELECT COALESCE(MAX(epoch),0) FROM continuous_leader').fetchone()[0] if 'continuous_leader' in tables else 0
                if epoch!=meta['leader_epoch']:raise Conflict('backup epoch mismatch')
                with closing(sqlite3.connect(target)) as restored:
                    _copy(source,restored)
                    uncertain=0
                    if 'continuous_actions' in tables:
                        restored.execute("UPDATE continuous_actions SET state='uncertain',reason='restore_uncertain' WHERE state='running'")
                        uncertain=restored.execute("SELECT COUNT(*) FROM continuous_actions WHERE state='uncertain'").fetchone()[0]
                    if 'continuous_leader' in tables:restored.execute('UPDATE continuous_leader SET expires=0,epoch=epoch+1')
                    if 'continuous_tick_guard' in tables:restored.execute('DELETE FROM continuous_tick_guard')
                    restored.execute('CREATE TABLE IF NOT EXISTS governance_restore_marker (backup_digest TEXT,reconciliation_required INTEGER,recorded REAL)')
                    restored.execute('INSERT INTO governance_restore_marker VALUES (?,?,?)',(meta['database_sha256'],1,self.store._now()))
                    restored.commit()
            self._global_owner(token)
            with target.open('rb') as stream:os.fsync(stream.fileno())
            return {'state':'restored_requires_verification','backup_digest':meta['database_sha256'],
                    'uncertain_actions':uncertain,'reconciliation_required':True,'automatic_replay':False,
                    'workers_stopped_verified':False,'credentials_refreshed':False}
        except BaseException:
            for path in (target,Path(str(target)+'-wal'),Path(str(target)+'-shm'),Path(str(target)+'-journal')):
                if path.exists():path.unlink()
            raise

    def _bindings(self,c,token,projects,owner=False):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):raise ValueError('explicit bounded projects required')
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner',) if owner else ('owner','manager'))
        bindings=[]
        for project in projects:
            name(project);config=self.registry._get(c,token,project)
            rows=c.execute('SELECT * FROM hierarchy_members WHERE project=? ORDER BY member_id LIMIT 201',(project,)).fetchall()
            if len(rows)>200:raise ValueError('bounded registered credential watch required')
            for row in rows:bindings.append((project,config['fingerprint'],dict(row)))
        return bindings

    def auth_watch(self,token,projects,*,refresh_before_seconds=3600,queue_notices=False):
        if type(refresh_before_seconds) is not int or not 0<=refresh_before_seconds<=86400 or type(queue_notices) is not bool:raise ValueError('bounded expiry watch required')
        with self.store._transaction() as c:bindings=self._bindings(c,token,projects,queue_notices)
        observations={};profile_observations={}
        for project,config,member in bindings:
            value=None
            profile_key=(project,member['role'])
            if self.expiration_provider is not None and member['role'] in ('developer','reviewer') and profile_key not in profile_observations:
                # No DB transaction spans a profile expiration observer.
                with self.store._transaction() as c:
                    if self._bindings(c,token,projects,queue_notices)!=bindings:raise Conflict('credential binding changed')
                try:value=self.expiration_provider(project,member['role'])
                except Exception:value=None
                profile_observations[profile_key]=value
            value=profile_observations.get(profile_key)
            observations[(project,member['member_id'])]=value
        results=[]
        with self.store._transaction() as c:
            if self._bindings(c,token,projects,queue_notices)!=bindings:raise Conflict('credential binding changed')
            now=self.store._now()
            for project,config,member in bindings:
                credential=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(member['credential_hash'],)).fetchone()
                state='current';expires=[]
                try:self.core._member(c,project,member['member_id'],member['role'],self.registry._get(c,token,project))
                except (PermissionDenied,Conflict):state='refresh_required'
                if credential:
                    if credential['expires'] is not None:expires.append(credential['expires'])
                    web=c.execute('SELECT parent_hash FROM bridge_web_sessions WHERE token_hash=?',(member['credential_hash'],)).fetchone()
                    parent=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(web['parent_hash'],)).fetchone() if web else None
                    if web and (not parent or parent['revoked'] is not None):state='refresh_required'
                    if parent and parent['expires'] is not None:expires.append(parent['expires'])
                if expires and min(expires)<=now+refresh_before_seconds:state='refresh_required'
                value=observations[(project,member['member_id'])];profile='not_applicable'
                if member['role'] in ('developer','reviewer'):
                    profile='unavailable'
                    if type(value) is dict and set(value)=={'state','expires_at'} and value['state']=='observed' and type(value['expires_at']) in (int,float) and math.isfinite(value['expires_at']):
                        profile='refresh_required' if value['expires_at']<=now+refresh_before_seconds else 'current'
                    if profile=='refresh_required':state='refresh_required'
                    elif profile=='unavailable' and state=='current':state='unavailable'
                item={'project':project,'member_id':member['member_id'],'role':member['role'],'state':state,
                      'credential_expires_at':min(expires) if expires else None,'profile_state':profile,'refresh_performed':False}
                from .agent_directory import _reject_known_tokens
                _reject_known_tokens(c,item)
                results.append(item)
                if queue_notices and state!='current':
                    key=digest({'project':project,'member':member['member_id'],'credential':member['credential_hash'],'config':config,'state':state,'expires':item['credential_expires_at'],'profile':profile})
                    c.execute("INSERT OR IGNORE INTO governance_auth_notices VALUES (?,?,?,?,?,?)",(key,project,member['member_id'],'credential_'+state,'queued',now))
        return {'items':results,'notices_queued_locally':queue_notices,'refresh_performed':False,'messages_sent':False}

    def list_notices(self,token,projects):
        with self.store._transaction() as c:
            self._bindings(c,token,projects)
            marks=','.join('?' for _ in projects)
            rows=c.execute('SELECT project,member_id,kind,state,recorded FROM governance_auth_notices WHERE project IN ('+marks+') ORDER BY recorded DESC LIMIT 100',tuple(projects)).fetchall()
            items=[dict(row) for row in rows]
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,items)
            total=c.execute('SELECT COUNT(*) FROM governance_auth_notices WHERE project IN ('+marks+')',tuple(projects)).fetchone()[0]
            return {'items':items,'limit':100,'total':total,'truncated':total>len(rows),'messages_sent':False}
