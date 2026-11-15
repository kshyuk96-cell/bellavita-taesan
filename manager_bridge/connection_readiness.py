"""Development connection gates and one owner-action packet; no deployment or writes.

A clean source copy/test run is not a verified security sandbox, live model, or
customer acceptance. Only registered host observers can supply measured facts.
"""
from dataclasses import asdict, dataclass
import json
import math
import re
from .maintenance_registry import name,receipt
from .store import Conflict,NotFound,_reject_credentials,_sha,canonical_json,digest

CAPABILITIES=('source_snapshot','fixture_tests','build','receiver','sandbox','model','host','customer_acceptance')


@dataclass(frozen=True)
class SourcePin:
    project:str
    source_kind:str
    source_ref:str
    branch:str
    sha:str
    revision:str

    def __post_init__(self):
        name(self.project);_sha(self.sha);receipt(self.source_ref);receipt(self.branch);name(self.revision)
        if self.source_kind not in ('github','sites'):raise ValueError('known source kind required')


@dataclass(frozen=True)
class ConnectionFact:
    project:str
    capability:str
    sha:str
    state:str
    reference:str
    observed_at:float
    passed:int=0
    failed:int=0
    skipped:int=0

    def __post_init__(self):
        name(self.project);_sha(self.sha);receipt(self.reference)
        if self.capability not in CAPABILITIES or self.state not in ('verified','partial','unavailable','permission_denied','unknown','not_applicable'):
            raise ValueError('explicit connection observation required')
        if type(self.observed_at) not in (float,int) or not math.isfinite(self.observed_at) or self.observed_at<=0:
            raise ValueError('actual observation time required')
        if any(type(v) is not int or not 0<=v<=100000 for v in (self.passed,self.failed,self.skipped)):
            raise ValueError('strict test counts required')
        if self.state=='not_applicable' and self.capability!='build':raise ValueError('not-applicable only supports build step')
        if self.capability=='fixture_tests' and self.state=='verified' and (self.failed or self.skipped or self.passed==0):
            raise ValueError('failed/skipped/unobserved tests cannot be fully verified')
        if self.failed and self.state=='verified':raise ValueError('failure cannot be verified')


@dataclass(frozen=True)
class HostProfile:
    pin:SourcePin
    observe:object
    read_only:bool=True

    def __post_init__(self):
        if not isinstance(self.pin,SourcePin) or not callable(self.observe) or self.read_only is not True:
            raise ValueError('trusted read-only profile required')


class Connections:
    def __init__(self,store,registry,profiles=None):
        if registry.store is not store:raise ValueError('shared manager registry required')
        self.store,self.registry=store,registry
        self.profiles=dict(profiles or {})
        for project,profile in self.profiles.items():
            if not isinstance(profile,HostProfile) or project!=profile.pin.project:
                raise ValueError('exact source profile required')
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS manager_connection_reports (
              project TEXT PRIMARY KEY,report TEXT NOT NULL,recorded REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS manager_connection_packets (
              id TEXT PRIMARY KEY,actor TEXT,request_key TEXT,body TEXT,fingerprint TEXT,
              created REAL,UNIQUE(actor,request_key));
            ''')

    def inspect(self,token,project,*,max_age=3600):
        if type(max_age) is not int or not 1<=max_age<=86400:raise ValueError('bounded evidence age required')
        config=self.registry.get(token,project)
        profile=self.profiles.get(project)
        facts=[]
        pin=None
        if profile:
            pin=profile.pin
            if (pin.source_kind,pin.source_ref,pin.branch)!=(config['config']['source_kind'],config['config']['source_ref'],config['config']['branch']):
                raise Conflict('registered source does not match project configuration')
        for capability in CAPABILITIES:
            row={'capability':capability,'state':'not_configured','reference':None,'passed':None,'failed':None,'skipped':None,'observed_at':None}
            if profile:
                # Auth is fresh before EVERY external provider call, including reads.
                current=self.registry.get(token,project)
                if current['fingerprint']!=config['fingerprint']:raise Conflict('connection scope changed')
                try:
                    value=profile.observe(project,capability)
                    if value is not None:
                        if not isinstance(value,ConnectionFact) or (value.project,value.capability,value.sha)!=(project,capability,pin.sha):
                            raise ValueError('connection observation scope mismatch')
                        _reject_credentials(asdict(value),token)
                        age=self.store._now()-value.observed_at
                        row.update(state=value.state if 0<=age<=max_age else 'stale',reference=value.reference,
                                   passed=value.passed,failed=value.failed,skipped=value.skipped,observed_at=value.observed_at)
                except PermissionError:row['state']='permission_denied'
                except Exception:row['state']='unknown'
            facts.append(row)
        states={x['capability']:x['state'] for x in facts}
        report={'project':project,'company':config['config']['company'],'config_fingerprint':config['fingerprint'],
                'source_pin':asdict(pin) if pin else None,'facts':facts,
                'development_source_tested':states['source_snapshot']=='verified' and states['fixture_tests'] in ('verified','partial'),
                'secure_worker_ready':all(states[c]=='verified' for c in ('source_snapshot','receiver','sandbox','host')),
                'delegated_execution_enabled':config['config']['maintenance_enabled'] and all(states[c]=='verified' for c in ('source_snapshot','receiver','sandbox','host')),
                'customer_launch_verified':False,'live_model_score':None,'operational_change':False,
                'checked_at':self.store._now()}
        with self.store._transaction() as c:
            current=self.registry._get(c,token,project)
            if current['fingerprint']!=config['fingerprint']:raise Conflict('connection scope changed during observation')
            c.execute('INSERT OR REPLACE INTO manager_connection_reports VALUES (?,?,?)',(project,canonical_json(report),self.store._now()))
        return report

    def _latest(self,c,token,project,max_age):
        config=self.registry._get(c,token,project)
        row=c.execute('SELECT * FROM manager_connection_reports WHERE project=?',(project,)).fetchone()
        if row is None:return {'project':project,'state':'not_checked','secure_worker_ready':False,'delegated_execution_enabled':False}
        value=json.loads(row['report'])
        stale=(value['config_fingerprint']!=config['fingerprint'] or not 0<=self.store._now()-row['recorded']<=max_age)
        for fact in value['facts']:
            if fact['state'] in ('verified','partial','not_applicable'):
                stamp=fact.get('observed_at')
                if type(stamp) not in (int,float) or not 0<=self.store._now()-stamp<=max_age:
                    fact['state']='stale';stale=True
        if stale:value.update(secure_worker_ready=False,delegated_execution_enabled=False,development_source_tested=False)
        return {**value,'stale':stale}

    def latest(self,token,project,*,max_age=3600):
        if type(max_age) is not int or not 1<=max_age<=86400:raise ValueError('bounded report age required')
        with self.store._transaction() as c:return self._latest(c,token,project,max_age)

    def owner_packet(self,token,request_key,projects):
        name(request_key);_reject_credentials(request_key,token)
        if type(projects) is not list or not 1<=len(projects)<=20 or len(set(projects))!=len(projects):
            raise ValueError('bounded exact project batch required')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            snapshots=[self._latest(c,token,project,3600) for project in sorted(projects)]
            states=[{x['capability']:x['state'] for x in s.get('facts',[])} for s in snapshots]
            actions=[]
            if any(x.get('host')!='verified' for x in states):actions.append('confirm_manager_host_commit_origin_persistent_storage_and_cost')
            if any(x.get('receiver')!='verified' or x.get('sandbox')!='verified' for x in states):actions.append('authenticate_and_register_project_scoped_receivers_and_sandbox')
            if any(not s.get('delegated_execution_enabled') for s in snapshots):actions.append('review_exact_delegation_paths_checks_budget_and_expiry')
            if any(x.get('model')!='verified' for x in states):actions.append('connect_existing_ai_gateway_or_approve_development_credentials_if_needed')
            body={'projects':snapshots,'owner_actions':actions,
                'activation_performed':False,'deployment_performed':False,'schedule_changed':False}
            semantic=json.loads(canonical_json(body))
            for snapshot in semantic['projects']:
                snapshot.pop('checked_at',None)
                for fact in snapshot.get('facts',[]):
                    fact.pop('reference',None);fact.pop('observed_at',None)
            fingerprint=digest(semantic)
            old=c.execute('SELECT * FROM manager_connection_packets WHERE actor=? AND request_key=?',(actor['token_hash'],request_key)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise Conflict('connection packet changed; use new reviewed version')
                return {'id':old['id'],'fingerprint':fingerprint,'packet':json.loads(old['body'])}
            key=digest({'actor':actor['token_hash'],'request_key':request_key})
            c.execute('INSERT INTO manager_connection_packets VALUES (?,?,?,?,?,?)',(key,actor['token_hash'],request_key,canonical_json(body),fingerprint,self.store._now()))
            return {'id':key,'fingerprint':fingerprint,'packet':body}
