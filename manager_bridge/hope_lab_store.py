"""Dedicated nonproduction fixture DB; cannot open an existing business DB."""
from contextlib import contextmanager
from dataclasses import dataclass,field
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from taesan_core.hope_test_data import FixtureContract,fixture_data,assert_initial_state,DATA_VERSION
from .hope_lab_model import apply,validate_state,LabBlocked
from .store import digest,canonical_json,Conflict,_reject_credentials


@dataclass(frozen=True)
class LabSession:
    workspace: str
    contract_digest: str
    source_revision: str
    tokens: dict=field(repr=False)


class LabStore:
    def __init__(self,path):
        self.path=Path(path).absolute()
        if any(p.is_symlink() for p in (self.path,*self.path.parents)):
            raise ValueError('dedicated nonsymlink fixture DB required')
        existed=self.path.exists()
        if not existed:
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        if not self.path.is_file() or self.path.stat().st_mode&0o077:
            raise ValueError('private fixture DB required')
        with self._transaction() as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected={'lab_meta','lab_workspaces','lab_identities','lab_requests','lab_events','lab_snapshots','sqlite_sequence'}
            if existed:
                if tables!=expected:raise ValueError('existing database is not this isolated test lab')
                marker=c.execute('SELECT value FROM lab_meta WHERE key=?',('environment',)).fetchone()
                if marker is None or marker[0]!='local_reference_fixture':raise ValueError('fixture environment marker missing')
            c.executescript('''
            CREATE TABLE IF NOT EXISTS lab_meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS lab_workspaces(id TEXT PRIMARY KEY,run_key TEXT UNIQUE,contract TEXT,generation INTEGER,state TEXT);
            CREATE TABLE IF NOT EXISTS lab_identities(hash TEXT PRIMARY KEY,workspace TEXT,role TEXT,subject TEXT);
            CREATE TABLE IF NOT EXISTS lab_requests(workspace TEXT,generation INTEGER,key TEXT,fingerprint TEXT,response TEXT,PRIMARY KEY(workspace,generation,key));
            CREATE TABLE IF NOT EXISTS lab_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,workspace TEXT,generation INTEGER,kind TEXT,evidence TEXT);
            CREATE TABLE IF NOT EXISTS lab_snapshots(workspace TEXT,generation INTEGER,state TEXT,PRIMARY KEY(workspace,generation));
            ''')
            c.execute('INSERT OR IGNORE INTO lab_meta VALUES (?,?)',('environment','local_reference_fixture'))

    @contextmanager
    def _transaction(self):
        c=sqlite3.connect(self.path,timeout=10,isolation_level=None);c.row_factory=sqlite3.Row
        try:
            c.execute('BEGIN IMMEDIATE');yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:c.close()

    def create(self,run_key,contract):
        if type(run_key) is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,100}',run_key) or not isinstance(contract,FixtureContract):
            raise ValueError('bounded run and approved typed contract required')
        with self._transaction() as c:
            if c.execute('SELECT 1 FROM lab_workspaces WHERE run_key=?',(run_key,)).fetchone():raise Conflict('run already exists; no automatic replay')
            if c.execute('SELECT count(*) FROM lab_workspaces').fetchone()[0]>=1000:raise Conflict('fixture workspace limit')
            key=secrets.token_hex(16)
            c.execute('INSERT INTO lab_workspaces VALUES (?,?,?,0,NULL)',(key,run_key,canonical_json(contract.manifest)))
            tokens={}
            identities=[(u,'member',u) for u in fixture_data(contract)['members']]+[(r,r,'fixture-'+r) for r in ('owner','finance','product_admin','unauthorized')]
            for label,role,subject in identities:
                raw=secrets.token_hex(32);tokens[label]=raw
                c.execute('INSERT INTO lab_identities VALUES (?,?,?,?)',(digest(raw),key,role,subject))
            return LabSession(key,contract.digest,contract.source_revision,tokens)

    def _get(self,c,workspace,token,*,roles=None):
        if type(token) is not str:raise PermissionError('fixture authentication required')
        actor=c.execute('SELECT * FROM lab_identities WHERE hash=? AND workspace=?',(digest(token),workspace)).fetchone()
        if actor is None or actor['role']=='unauthorized' or (roles is not None and actor['role'] not in roles):
            raise PermissionError('fixture scope/role denied')
        row=c.execute('SELECT * FROM lab_workspaces WHERE id=?',(workspace,)).fetchone()
        if row is None:raise PermissionError('fixture workspace denied')
        return dict(actor),dict(row),json.loads(row['contract'])

    def inspect(self,workspace,token):
        with self._transaction() as c:
            actor,row,contract=self._get(c,workspace,token)
            state=json.loads(row['state']) if row['state'] is not None else None
            if state is not None and actor['role']=='member':
                uid=actor['subject'];state=dict(members={uid:state['members'][uid]},products=state['products'],
                    bookings={k:v for k,v in state['bookings'].items() if v['member']==uid},
                    refunds={k:v for k,v in state['refunds'].items() if state['bookings'][v['booking']]['member']==uid},
                    withdrawals={k:v for k,v in state['withdrawals'].items() if v['member']==uid},
                    journal=[r for r in state['journal'] if r['member']==uid],cards=[r for r in state['cards'] if r['member']==uid],
                    pending={uid:state['pending'][uid]} if uid in state['pending'] else {})
            elif state is not None and actor['role']=='product_admin':state={'products':state['products']}
            return dict(workspace=workspace,generation=row['generation'],contract_digest=digest(contract),
                state=state,state_digest=digest(state),source_revision=contract['source_revision'],scope='local_reference_fixture',operational_authorization=False)

    @staticmethod
    def _key(key):
        if type(key) is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,100}',key):raise ValueError('bounded request key required')

    @staticmethod
    def _cached(c,workspace,generation,key,fingerprint):
        old=c.execute('SELECT fingerprint,response FROM lab_requests WHERE workspace=? AND generation=? AND key=?',(workspace,generation,key)).fetchone()
        if old:
            if old['fingerprint']!=fingerprint:raise Conflict('request key reused with different actor/command')
            return json.loads(old['response'])

    def seed(self,workspace,token,*,expected_generation,request_key,contract_digest):
        self._key(request_key)
        _reject_credentials({'request_key':request_key},token)
        if type(expected_generation) is not int:raise ValueError('generation integer required')
        with self._transaction() as c:
            actor,row,manifest=self._get(c,workspace,token,roles=('owner',))
            fp=digest([actor['subject'],'seed',expected_generation,contract_digest])
            cached=self._cached(c,workspace,expected_generation,request_key,fp)
            if cached is not None:return cached
            if expected_generation>=100:raise Conflict('fixture reset limit')
            if row['generation']!=expected_generation or contract_digest!=digest(manifest):raise Conflict('stale fixture contract/generation')
            if row['state'] is not None and json.loads(row['state'])['pending']:
                raise Conflict('uncertain effects require explicit reconciliation before reset')
            contract=FixtureContract(**{k:manifest[k] for k in ('source_revision','seed','example_bonus_order','cancellation_fee_won','scope','max_commands')})
            state=fixture_data(contract);check=assert_initial_state(state,contract);validate_state(state)
            if row['state'] is not None:
                c.execute('INSERT INTO lab_snapshots VALUES (?,?,?)',(workspace,expected_generation,row['state']))
            generation=expected_generation+1
            c.execute('UPDATE lab_workspaces SET generation=?,state=? WHERE id=?',(generation,canonical_json(state),workspace))
            result=dict(workspace=workspace,generation=generation,initial_state_digest=digest(state),
                        contract_digest=contract.digest,source_revision=contract.source_revision,matched=check['matched'],scope='local_reference_fixture')
            c.execute('INSERT INTO lab_requests VALUES (?,?,?,?,?)',(workspace,expected_generation,request_key,fp,canonical_json(result)))
            c.execute('INSERT INTO lab_events(workspace,generation,kind,evidence) VALUES (?,?,?,?)',
                (workspace,generation,'seed' if expected_generation==0 else 'reset',canonical_json(result)))
            return result

    def command(self,workspace,token,*,generation,request_key,action,payload):
        self._key(request_key)
        _reject_credentials({'request_key':request_key,'payload':payload},token)
        if type(generation) is not int:raise ValueError('generation integer required')
        with self._transaction() as c:
            actor,row,manifest=self._get(c,workspace,token)
            if row['generation']!=generation or row['state'] is None:raise Conflict('stale/unseeded fixture execution')
            fp=digest([actor['subject'],action,payload])
            cached=self._cached(c,workspace,generation,request_key,fp)
            if cached is not None:return cached
            if c.execute("SELECT count(*) FROM lab_requests WHERE workspace=? AND generation=?",(workspace,generation)).fetchone()[0]>=manifest['max_commands']:
                raise Conflict('fixture command limit')
            before=json.loads(row['state']);after,result=apply(before,manifest,actor,action,payload,request_key)
            if after['journal'][:len(before['journal'])]!=before['journal']:raise ValueError('append-only journal violated')
            receipt=dict(workspace=workspace,generation=generation,request_key=request_key,action=action,
                actor=actor['subject'],contract_digest=digest(manifest),source_revision=manifest['source_revision'],result=result,before_digest=digest(before),after_digest=digest(after),
                journal_appended=after['journal'][len(before['journal']):],scope='local_reference_fixture',
                operational_authorization=False)
            c.execute('UPDATE lab_workspaces SET state=? WHERE id=?',(canonical_json(after),workspace))
            c.execute('INSERT INTO lab_requests VALUES (?,?,?,?,?)',(workspace,generation,request_key,fp,canonical_json(receipt)))
            c.execute('INSERT INTO lab_events(workspace,generation,kind,evidence) VALUES (?,?,?,?)',
                (workspace,generation,'command',canonical_json(receipt)))
            return receipt

    def evidence(self,workspace,token):
        with self._transaction() as c:
            self._get(c,workspace,token,roles=('owner','finance'))
            return [dict(sequence=r['seq'],generation=r['generation'],kind=r['kind'],receipt=json.loads(r['evidence']))
                    for r in c.execute('SELECT * FROM lab_events WHERE workspace=? ORDER BY seq',(workspace,))]
