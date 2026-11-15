"""Backup facts and owner-approved SQLite fixture restore/migration rehearsals.

Operational backups are metadata-only. A registered local fixture can be copied
into a fresh temporary directory and tested, never restored over a source DB.
No task-supplied paths/SQL, network access, credential bodies, or row logging.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
from .maintenance_registry import name, receipt
from .store import Conflict, NotFound, _reject_credentials, canonical_json, digest

MAX_BACKUP_BYTES = 32 * 1024 * 1024


def _hash(value):
    if type(value) is not str or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ValueError('exact SHA256 required')
    return value


def _table(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', value):
        raise ValueError('registered SQLite identifier required')
    return value


def _positive(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('positive observation clock required')
    return value


@dataclass(frozen=True)
class BackupMetadata:
    company: str
    project: str
    backup_id: str
    source_snapshot: str
    sha256: str
    size: int
    encrypted: bool
    created_at: float
    retention_until: float
    reference: str
    restore_checked_at: float | None = None
    restore_reference: str | None = None
    readable: bool = False

    def __post_init__(self):
        for value in (self.company, self.project, self.backup_id, self.source_snapshot):
            name(value)
        _hash(self.sha256)
        if type(self.size) is not int or not 0 < self.size <= MAX_BACKUP_BYTES or type(self.encrypted) is not bool or type(self.readable) is not bool:
            raise ValueError('bounded backup size and explicit encryption fact required')
        _positive(self.created_at); _positive(self.retention_until); receipt(self.reference)
        if self.retention_until <= self.created_at:
            raise ValueError('backup retention must follow creation')
        if (self.restore_checked_at is None) != (self.restore_reference is None):
            raise ValueError('restore evidence and observation time must be paired')
        if self.restore_checked_at is not None:
            _positive(self.restore_checked_at); receipt(self.restore_reference)
            if self.restore_checked_at < self.created_at:raise ValueError('restore observation predates backup')


@dataclass(frozen=True)
class BackupSpec:
    project: str
    backup_id: str
    observe: object
    fixture_path: str | None = None
    fixture_only: bool = False

    def __post_init__(self):
        name(self.project); name(self.backup_id)
        if not callable(self.observe) or type(self.fixture_only) is not bool:
            raise ValueError('registered read-only backup observer required')
        if self.fixture_path is not None and (type(self.fixture_path) is not str or not Path(self.fixture_path).is_absolute()):
            raise ValueError('trusted absolute fixture path required')
        if self.fixture_only and self.fixture_path is None:
            raise ValueError('fixture path required')
        if not self.fixture_only and self.fixture_path is not None:
            raise ValueError('operational paths cannot enter rehearsal executor')


@dataclass(frozen=True)
class MigrationSpec:
    project: str
    id: str
    statements: tuple
    expected_schema: dict

    def __post_init__(self):
        name(self.project); name(self.id)
        if type(self.statements) is not tuple or not 1 <= len(self.statements) <= 100 or any(
                type(s) is not str or not 0 < len(s) <= 65536 or '\x00' in s for s in self.statements):
            raise ValueError('registered migration statements required')
        if type(self.expected_schema) is not dict or not self.expected_schema:
            raise ValueError('expected table/column contract required')
        copied = {}
        for table, columns in self.expected_schema.items():
            _table(table)
            if type(columns) is not tuple or not columns or len(set(columns)) != len(columns):
                raise ValueError('exact expected columns required')
            for column in columns: _table(column)
            copied[table] = tuple(columns)
        object.__setattr__(self, 'expected_schema', copied)

    @property
    def fingerprint(self):
        return digest(asdict(self))


class DataChecks:
    def __init__(self, store, registry, *, backups=None, migrations=None):
        if registry.store is not store:
            raise ValueError('shared manager store required')
        self.store, self.registry = store, registry
        self.backups, self.migrations = dict(backups or {}), dict(migrations or {})
        for key, spec in self.backups.items():
            if not isinstance(spec, BackupSpec) or key != (spec.project, spec.backup_id):
                raise ValueError('project-bound backup registration required')
        for key, spec in self.migrations.items():
            if not isinstance(spec, MigrationSpec) or key != (spec.project, spec.id):
                raise ValueError('project-bound migration registration required')
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS maintenance_data_plans (
              id TEXT PRIMARY KEY, project TEXT NOT NULL, body TEXT NOT NULL,
              digest TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
              request_key TEXT, UNIQUE(project, request_key));
            CREATE TABLE IF NOT EXISTS maintenance_backup_reports (
              project TEXT PRIMARY KEY, config_fingerprint TEXT NOT NULL,
              report TEXT NOT NULL, recorded REAL NOT NULL);
            ''')

    def _config(self, token, project, *, owner=False):
        with self.store._transaction() as c:
            return self.registry._get(c, token, project, ('owner',) if owner else ('owner','manager'))

    def _observe(self, token, project, spec, config):
        try:
            value = spec.observe(project, spec.backup_id)
            if not isinstance(value, BackupMetadata):
                return None, 'unknown'
            _reject_credentials(asdict(value), token)
            if (value.company,value.project,value.backup_id) != (config['config']['company'],project,spec.backup_id):
                return None, 'permission_denied'
            now = self.store._now()
            if value.created_at > now or (value.restore_checked_at is not None and value.restore_checked_at > now):
                return None, 'unknown'
            return value, 'expired' if value.retention_until <= now else 'observed'
        except PermissionError:
            return None, 'permission_denied'
        except Exception:
            return None, 'unknown'

    def inspect(self, token, project):
        config = self._config(token, project)
        facts = []
        for (scope, key), spec in sorted(self.backups.items()):
            if scope != project: continue
            current=self._config(token,project)
            if current['fingerprint']!=config['fingerprint']:
                raise Conflict('project configuration changed before backup observer')
            metadata, state = self._observe(token, project, spec, config)
            fact = {'backup_id':key,'state':state,'encrypted':None,'size':None,
                    'checksum_recorded':False,'restore_verified':False,'readable':None,'reference':None}
            if metadata is not None:
                fact.update(encrypted=metadata.encrypted,size=metadata.size,readable=metadata.readable,
                            checksum_recorded=True,reference=metadata.reference,
                            retention_until=metadata.retention_until,
                            restore_verified=metadata.restore_checked_at is not None,
                            restore_reference=metadata.restore_reference)
            facts.append(fact)
        report={'project':project,'state':'observed' if facts else 'not_configured',
                'backups':facts,'read_only':True,'production_restore_performed':False,
                'config_fingerprint':config['fingerprint'],'checked_at':self.store._now()}
        _reject_credentials(report,token)
        with self.store._transaction() as c:
            current=self.registry._get(c,token,project)
            if current['fingerprint']!=config['fingerprint']:
                raise Conflict('project configuration changed during backup observation')
            c.execute('INSERT OR REPLACE INTO maintenance_backup_reports VALUES (?,?,?,?)',
                      (project,config['fingerprint'],canonical_json(report),self.store._now()))
        return report

    def latest(self,token,project,*,max_age=300):
        if type(max_age) is not int or not 1<=max_age<=3600:
            raise ValueError('backup observation age required')
        with self.store._transaction() as c:
            config=self.registry._get(c,token,project)
            row=c.execute('SELECT * FROM maintenance_backup_reports WHERE project=?',(project,)).fetchone()
            if row is None:return {'project':project,'state':'not_checked','backups':[]}
            result=json.loads(row['report'])
            changed=row['config_fingerprint']!=config['fingerprint']
            stale=changed or not 0<=self.store._now()-row['recorded']<=max_age
            return {**result,'state':'stale' if stale else result['state'],
                    'stale':stale,'configuration_changed':changed}

    def prepare(self, token, project, backup_id, migration_id=None):
        name(backup_id)
        config = self._config(token, project)
        spec = self.backups.get((project,backup_id))
        if spec is None: raise NotFound('backup not registered for project')
        if not spec.fixture_only: raise Conflict('operational restore execution is not enabled')
        metadata, state = self._observe(token,project,spec,config)
        if state != 'observed' or not metadata.readable: raise Conflict('current scoped readable backup facts required')
        data = self._read_fixture(spec)
        if len(data) != metadata.size or hashlib.sha256(data).hexdigest() != metadata.sha256:
            raise Conflict('fixture does not match backup observation')
        migration = self._migration(project,migration_id)
        body = {'project':project,'company':config['config']['company'],
                'config_fingerprint':config['fingerprint'],'backup_id':backup_id,
                'backup_fingerprint':digest(asdict(metadata)), 'backup_sha256':metadata.sha256,
                'source_snapshot':metadata.source_snapshot,'migration_id':migration_id,
                'migration_fingerprint':migration.fingerprint if migration else None,
                'fixture_only':True}
        _reject_credentials(body,token)
        key, fingerprint = secrets.token_hex(16), digest(body)
        with self.store._transaction() as c:
            current = self.registry._get(c,token,project)
            if current['fingerprint'] != config['fingerprint']:raise Conflict('project configuration changed')
            c.execute('INSERT INTO maintenance_data_plans VALUES (?,?,?,?,?,NULL,NULL)',
                      (key,project,canonical_json(body),fingerprint,'pending_approval'))
        return {'id':key,'project':project,'digest':fingerprint,'state':'pending_approval',
                'fixture_only':True,'production_restore_authorized':False}

    def _migration(self, project, migration_id):
        if migration_id is None:return None
        name(migration_id)
        spec = self.migrations.get((project,migration_id))
        if spec is None:raise NotFound('migration not registered for project')
        return spec

    def approve(self, token, plan_id, plan_digest):
        name(plan_id);_hash(plan_digest)
        with self.store._transaction() as c:
            row = c.execute('SELECT * FROM maintenance_data_plans WHERE id=?',(plan_id,)).fetchone()
            if row is None:raise NotFound('data check plan not found')
            config = self.registry._get(c,token,row['project'],('owner',))
            body = json.loads(row['body'])
            if row['digest'] != plan_digest or digest(body) != plan_digest:raise Conflict('exact plan digest required')
            if config['fingerprint'] != body['config_fingerprint']:raise Conflict('configuration changed')
            if row['state'] not in ('pending_approval','approved'):raise Conflict('plan already executed')
            c.execute("UPDATE maintenance_data_plans SET state='approved' WHERE id=?",(plan_id,))
        return {'id':plan_id,'state':'approved','digest':plan_digest,'production_restore_authorized':False}

    def run(self, token, plan_id, request_key):
        name(plan_id);name(request_key)
        with self.store._transaction() as c:
            row = c.execute('SELECT * FROM maintenance_data_plans WHERE id=?',(plan_id,)).fetchone()
            if row is None:raise NotFound('data check plan not found')
            config = self.registry._get(c,token,row['project'])
            body = json.loads(row['body'])
            if config['fingerprint'] != body['config_fingerprint']:raise Conflict('configuration changed')
            if row['request_key'] is not None:
                if row['request_key'] != request_key:raise Conflict('plan execution key changed')
                if row['result']:return json.loads(row['result'])
                return {'id':plan_id,'state':'uncertain','replayed':False}
            if row['state'] != 'approved':raise Conflict('owner-approved exact plan required')
            old = c.execute('SELECT id FROM maintenance_data_plans WHERE project=? AND request_key=?',
                            (row['project'],request_key)).fetchone()
            if old:raise Conflict('execution key belongs to another plan')
            c.execute("UPDATE maintenance_data_plans SET state='running',request_key=? WHERE id=?",(request_key,plan_id))
        try:
            spec = self.backups.get((body['project'],body['backup_id']))
            if spec is None or not spec.fixture_only:raise Conflict('registered fixture not available')
            metadata,state = self._observe(token,body['project'],spec,config)
            if state != 'observed' or not metadata.readable or digest(asdict(metadata)) != body['backup_fingerprint']:
                raise Conflict('backup facts changed after approval')
            migration = self._migration(body['project'],body['migration_id'])
            if (migration.fingerprint if migration else None) != body['migration_fingerprint']:
                raise Conflict('registered migration changed after approval')
            current=self._config(token,body['project'])
            if current['fingerprint']!=body['config_fingerprint']:raise Conflict('configuration changed before rehearsal')
            data = self._read_fixture(spec)
            if hashlib.sha256(data).hexdigest() != body['backup_sha256'] or len(data) != metadata.size:
                raise Conflict('approved backup bytes changed')
            facts = self._rehearse(data,migration)
            result = {'id':plan_id,'project':body['project'],'state':'passed' if facts['compatible'] else 'failed',
                      'backup_sha256':metadata.sha256,'source_snapshot':body['source_snapshot'],
                      'fixture_only':True,'production_restore_performed':False,**facts}
        except Conflict:
            result = {'id':plan_id,'project':body['project'],'state':'blocked',
                      'reason':'approved_facts_changed','fixture_only':True,'production_restore_performed':False}
        except Exception:
            result = {'id':plan_id,'project':body['project'],'state':'failed',
                      'reason':'fixture_rehearsal_failed','fixture_only':True,'production_restore_performed':False}
        with self.store._transaction() as c:
            current = self.registry._get(c,token,body['project'])
            if current['fingerprint'] != body['config_fingerprint']:
                raise Conflict('configuration changed during rehearsal; outcome needs review')
            _reject_credentials(result,token)
            c.execute('UPDATE maintenance_data_plans SET state=?,result=? WHERE id=?',
                      (result['state'],canonical_json(result),plan_id))
        return result

    @staticmethod
    def _read_fixture(spec):
        path = Path(spec.fixture_path)
        if any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
            raise Conflict('self-contained SQLite backup required; live sidecars forbidden')
        if any(p.is_symlink() for p in (path,*path.parents)):
            raise Conflict('fixture symlinks forbidden')
        descriptor = os.open(path,os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            import stat
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BACKUP_BYTES:
                raise Conflict('bounded regular fixture required')
            data = os.read(descriptor,MAX_BACKUP_BYTES+1)
            after = os.fstat(descriptor)
            if len(data) != info.st_size or (info.st_size,info.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                raise Conflict('fixture changed while reading')
            return data
        finally:os.close(descriptor)

    @staticmethod
    def _schema(connection):
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if len(tables)>100:raise ValueError('fixture schema too large')
        result={}
        for table in tables:
            _table(table)
            result[table]=[tuple(row[1:]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
        return result

    @staticmethod
    def _rows(connection, table, columns):
        for column in columns:_table(column)
        sql='SELECT '+','.join('"'+c+'"' for c in columns)+' FROM "'+_table(table)+'"'
        values=[]
        def serial(value):return {'blob':value.hex()} if isinstance(value,bytes) else value
        for row in connection.execute(sql):
            values.append(canonical_json([serial(v) for v in row]))
            if len(values)>10000:raise ValueError('bounded fixture rows required')
        return sorted(values)

    @classmethod
    def _rehearse(cls,data,migration):
        with tempfile.TemporaryDirectory(prefix='taesan-data-rehearsal-') as folder:
            root=Path(folder); path=root/'restore.sqlite3'; path.write_bytes(data);path.chmod(0o600)
            connection=sqlite3.connect(path)
            try:
                connection.enable_load_extension(False)
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024*1024)
                connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65536)
                connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
                page_size=connection.execute('PRAGMA page_size').fetchone()[0]
                connection.execute('PRAGMA max_page_count='+str(MAX_BACKUP_BYTES//page_size))
                deadline=time.monotonic()+2
                connection.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
                connection.execute('PRAGMA foreign_keys=ON')
                if connection.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('integrity failure')
                before=cls._schema(connection)
                rows={table:cls._rows(connection,table,[column[0] for column in columns]) for table,columns in before.items()}
                connection.execute('BEGIN')
                if migration:
                    def authorize(action,arg1,arg2,database,trigger):
                        if action in (sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH,sqlite3.SQLITE_PRAGMA,sqlite3.SQLITE_TRANSACTION):return sqlite3.SQLITE_DENY
                        if action==sqlite3.SQLITE_FUNCTION and str(arg2).lower()=='load_extension':return sqlite3.SQLITE_DENY
                        # SQLite passes a dropped column in the database slot for ALTER.
                        if action==sqlite3.SQLITE_ALTER_TABLE:return sqlite3.SQLITE_OK if arg1=='main' else sqlite3.SQLITE_DENY
                        if database not in (None,'main','temp'):return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK
                    connection.set_authorizer(authorize)
                    try:
                        for statement in migration.statements:
                            if not re.match(r'^\s*(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE)\b',statement,re.I):raise ValueError('unsupported fixture migration statement')
                            connection.execute(statement)
                    finally:connection.set_authorizer(None)
                after=cls._schema(connection)
                shape={table:tuple(column[0] for column in columns) for table,columns in after.items()}
                expected=migration.expected_schema if migration else {table:tuple(column[0] for column in columns) for table,columns in before.items()}
                schema_matches=shape==expected
                data_preserved=True;types_preserved=True
                for table,columns in before.items():
                    old_names=[column[0] for column in columns]
                    new_columns={column[0]:column for column in after.get(table,[])}
                    if not set(old_names)<=new_columns.keys():data_preserved=False;types_preserved=False;continue
                    if any(tuple(column[1:])!=tuple(new_columns[column[0]][1:]) for column in columns):types_preserved=False
                    if cls._rows(connection,table,old_names)!=rows[table]:data_preserved=False
                foreign_keys_ok=not list(connection.execute('PRAGMA foreign_key_check'))
                integrity_ok=connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                connection.rollback()
                rollback_schema=cls._schema(connection)
                rollback_ok=rollback_schema==before and all(cls._rows(connection,t,[c[0] for c in cols])==rows[t] for t,cols in before.items())
                return {'compatible':all((schema_matches,data_preserved,types_preserved,foreign_keys_ok,integrity_ok,rollback_ok)),
                        'schema_matches':schema_matches,'data_preserved':data_preserved,'existing_column_contract_preserved':types_preserved,
                        'foreign_keys_ok':foreign_keys_ok,'integrity_ok':integrity_ok,'rollback_verified':rollback_ok,
                        'table_counts':{t:len(values) for t,values in rows.items()},
                        'before_schema_digest':digest(before),'after_schema_digest':digest(after)}
            finally:connection.close()
