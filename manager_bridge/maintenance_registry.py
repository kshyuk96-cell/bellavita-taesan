"""Pinned project inventory, credential *references*, read-only observations and incidents.

No credentials, provider bodies, customer data, network calls or deployment actions.
Host callbacks and source mappings are trusted local configuration, never model input.
"""
from dataclasses import dataclass
import json
import math
import re
from urllib.parse import urlsplit
from .store import Conflict, NotFound, PermissionDenied, _REPOSITORY, _project, _reject_credentials, canonical_json, digest


def name(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',value):
        raise ValueError('registered identifier required')
    return value


def receipt(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_:/#.-]{1,200}',value):
        raise ValueError('observation reference required')
    return value


def scoped(store,c,token,project,roles=('owner','manager')):
    actor=store._authenticate(c,token)
    store._role(actor,roles)
    store._project_access(c,actor,project)
    return actor


def configuration(value):
    keys={'company','name','source_kind','source_ref','branch','services','maintenance_enabled'}
    if type(value) is not dict or set(value)!=keys:
        raise ValueError('exact project configuration required')
    _project(value['company'])
    if type(value['branch']) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,127}',value['branch']) or any(p in ('','.','..') for p in value['branch'].split('/')):
        raise ValueError('exact source branch required')
    if type(value['name']) is not str or not value['name'].strip() or len(value['name'])>100:
        raise ValueError('project display name required')
    if value['source_kind']=='github':
        if type(value['source_ref']) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value['source_ref']):
            raise ValueError('GitHub repository required')
    elif value['source_kind']=='sites':
        if type(value['source_ref']) is not str or not re.fullmatch(r'appgprj_[A-Za-z0-9_]+',value['source_ref']):
            raise ValueError('exact Sites project required')
    else:raise ValueError('unsupported source')
    if type(value['maintenance_enabled']) is not bool or type(value['services']) is not list or len(value['services'])>10:
        raise ValueError('explicit maintenance flag and services required')
    seen=set()
    for item in value['services']:
        if type(item) is not dict or set(item)!={'kind','id','url','purpose'} or item['kind'] not in {'render','sites'} or item['purpose'] not in {'review','production'}:
            raise ValueError('exact service binding required')
        name(item['id'])
        parsed=urlsplit(item['url'])
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise ValueError('HTTPS service origin without credentials required')
        if (item['kind'],item['id']) in seen:raise ValueError('duplicate service binding')
        seen.add((item['kind'],item['id']))
    return json.loads(canonical_json(value))


@dataclass(frozen=True)
class Observation:
    state:str
    target:str
    reference:str
    observed_at:float

    def __post_init__(self):
        if self.state not in {'ready','degraded','unavailable','permission_denied','unknown'}:
            raise ValueError('invalid observation state')
        receipt(self.target);receipt(self.reference)
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:
            raise ValueError('invalid observation clock')


@dataclass(frozen=True)
class ReadProbe:
    target:str
    observe:object
    read_only:bool=True

    def __post_init__(self):
        receipt(self.target)
        if not callable(self.observe) or self.read_only is not True:raise ValueError('registered read-only probe required')


@dataclass(frozen=True)
class CredentialObservation:
    company:str
    project:str
    alias:str
    capabilities:tuple
    expires_at:float
    reference:str

    def __post_init__(self):
        _project(self.company);_project(self.project);name(self.alias);receipt(self.reference)
        if type(self.capabilities) is not tuple or not self.capabilities or not set(self.capabilities)<={'read','test','develop'} or len(set(self.capabilities))!=len(self.capabilities):
            raise ValueError('non-operational connection capabilities required')
        if type(self.expires_at) not in (int,float) or not math.isfinite(self.expires_at) or self.expires_at<=0:
            raise ValueError('connection expiry required')


class Registry:
    def __init__(self,store,*,probes=None,credential_observers=None,fixtures=None):
        self.store=store
        self.fixtures=json.loads(canonical_json(fixtures or {}))
        self.probes=dict(probes or {})
        self.credential_observers=dict(credential_observers or {})
        for (project,key),probe in self.probes.items():
            _project(project);name(key)
            if not isinstance(probe,ReadProbe):raise ValueError('registered probe required')
        for (project,alias),callback in self.credential_observers.items():
            _project(project);name(alias)
            if not callable(callback):raise ValueError('credential observer required')
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS maintenance_projects (
                project TEXT PRIMARY KEY, config TEXT NOT NULL, revision INTEGER NOT NULL,
                fingerprint TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS maintenance_health (
                project TEXT PRIMARY KEY, report TEXT NOT NULL, recorded REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS maintenance_incidents (
                id TEXT PRIMARY KEY,project TEXT,code TEXT,target TEXT,severity TEXT,
                first_seen REAL,last_seen REAL,occurrences INTEGER,revision INTEGER,
                state TEXT,reference TEXT, UNIQUE(project,code,target));
            ''')

    def register(self,token,project,config,*,expected_revision=0):
        _project(project);config=configuration(config)
        if type(expected_revision) is not int or expected_revision<0:raise ValueError('expected revision required')
        _reject_credentials(config,token)
        repository=config['source_ref'] if config['source_kind']=='github' else 'sites/'+config['source_ref']
        if not _REPOSITORY.fullmatch(repository):raise ValueError('exact repository binding required')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner',))
            if not self.store._scope(actor,project):raise PermissionDenied('project outside owner scope')
            # Atomically retain the bridge's immutable repository binding together with
            # the maintenance configuration; invalid revisions leave no orphan project.
            bound=c.execute('SELECT repository FROM bridge_projects WHERE project=?',(project,)).fetchone()
            if bound and bound['repository']!=repository:raise Conflict('registered repository cannot change')
            old=c.execute('SELECT * FROM maintenance_projects WHERE project=?',(project,)).fetchone()
            if expected_revision!=(old['revision'] if old else 0):raise Conflict('project configuration changed')
            if old and json.loads(old['config'])['company']!=config['company']:
                raise Conflict('company ownership cannot change in place')
            if not bound:
                c.execute('INSERT INTO bridge_projects VALUES (?,?,?)',(project,repository,self.store._now()))
            revision=(old['revision'] if old else 0)+1
            fingerprint=digest({'project':project,'config':config,'revision':revision})
            c.execute('INSERT OR REPLACE INTO maintenance_projects VALUES (?,?,?,?,?)',
                      (project,canonical_json(config),revision,fingerprint,self.store._now()))
            return {'project':project,'revision':revision,'fingerprint':fingerprint,'config':config}

    def _get(self,c,token,project,roles=('owner','manager')):
        scoped(self.store,c,token,project,roles)
        row=c.execute('SELECT * FROM maintenance_projects WHERE project=?',(project,)).fetchone()
        if row is None:raise NotFound('maintenance project not registered')
        return {'project':project,'revision':row['revision'],'fingerprint':row['fingerprint'],'config':json.loads(row['config'])}

    def get(self,token,project):
        with self.store._transaction() as c:return self._get(c,token,project)

    def list(self,token):
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            rows=c.execute('SELECT project FROM maintenance_projects ORDER BY project').fetchall()
            return [self._get(c,token,row['project']) for row in rows if self.store._scope(actor,row['project'])]

    def credential(self,token,project,alias,*,required=('read',)):
        name(alias)
        _reject_credentials({'alias':alias,'required':required},token)
        if type(required) is not tuple or not required or not set(required)<={'read','test','develop'}:
            raise ValueError('registered non-operational capability required')
        config=self.get(token,project)
        callback=self.credential_observers.get((project,alias))
        result={'alias':alias,'state':'not_configured','reference':None}
        if callback:
            try:
                observation=callback(project,alias)
                if not isinstance(observation,CredentialObservation):raise ValueError('invalid credential observation')
                _reject_credentials(observation.reference,token)
                if (observation.project,observation.company,observation.alias)!=(project,config['config']['company'],alias):
                    result['state']='permission_denied'
                elif observation.expires_at<=self.store._now():result['state']='expired'
                elif not set(required)<=set(observation.capabilities):result['state']='permission_denied'
                else:result={'alias':alias,'state':'ready','reference':observation.reference}
            except Exception:result['state']='unknown'
        current=self.get(token,project)
        if current['fingerprint']!=config['fingerprint']:raise Conflict('configuration changed during check')
        return result

    def inspect(self,token,project,*,max_age=300):
        if type(max_age) is not int or not 1<=max_age<=3600:raise ValueError('observation age required')
        config=self.get(token,project)
        targets={config['config']['source_ref'],*(s['id'] for s in config['config']['services'])}
        probes=[(key,probe) for (scope,key),probe in self.probes.items() if scope==project]
        results=[]
        configured={p.target for _,p in probes}
        for target in sorted(targets-configured):
            results.append({'name':'unconnected:'+target,'state':'not_configured','reference':None})
        for key,probe in sorted(probes):
            current=self.get(token,project)
            if current['fingerprint']!=config['fingerprint']:
                raise Conflict('configuration changed during observation')
            result={'name':key,'state':'unknown','reference':None}
            if probe.target not in targets:raise Conflict('probe target is not registered for this project')
            try:
                observed=probe.observe(project,config['config'])
                if not isinstance(observed,Observation) or observed.target!=probe.target:raise ValueError('observation target mismatch')
                _reject_credentials(observed.reference,token)
                age=self.store._now()-observed.observed_at
                result.update(state=observed.state if 0<=age<=max_age else 'stale',reference=observed.reference)
            except Exception:pass
            results.append(result)
        states={r['state'] for r in results}
        report={'project':project,'config_fingerprint':config['fingerprint'],'checked_at':self.store._now(),
                'state':'ready' if states=={'ready'} else 'attention','observations':results,
                'read_only':True,'operational_change':False}
        with self.store._transaction() as c:
            current=self._get(c,token,project)
            if current['fingerprint']!=config['fingerprint']:raise Conflict('configuration changed during observation')
            c.execute('INSERT OR REPLACE INTO maintenance_health VALUES (?,?,?)',
                      (project,canonical_json(report),self.store._now()))
        return report

    def incident(self,token,project,*,code,target,severity,reference):
        name(code);receipt(target);receipt(reference)
        if severity not in {'critical','high','normal'}:raise ValueError('incident severity required')
        _reject_credentials({'code':code,'target':target,'severity':severity,'reference':reference},token)
        with self.store._transaction() as c:
            config=self._get(c,token,project)
            targets={config['config']['source_ref'],*(s['id'] for s in config['config']['services'])}
            if target not in targets:raise Conflict('incident source not registered')
            old=c.execute('SELECT * FROM maintenance_incidents WHERE project=? AND code=? AND target=?',(project,code,target)).fetchone()
            key=digest({'project':project,'code':code,'target':target})
            now=self.store._now()
            if old:
                ranks={'normal':0,'high':1,'critical':2}
                severity=max((old['severity'],severity),key=ranks.get)
                c.execute("UPDATE maintenance_incidents SET last_seen=?,occurrences=occurrences+1,reference=?,severity=?,revision=revision+?,state='open' WHERE id=?",
                          (now,reference,severity,int(old['state']!='open' or old['severity']!=severity),key))
            else:c.execute('INSERT INTO maintenance_incidents VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (key,project,code,target,severity,now,now,1,1,'open',reference))
            row=c.execute('SELECT * FROM maintenance_incidents WHERE id=?',(key,)).fetchone()
            return dict(row)

    def incidents(self,token,project):
        with self.store._transaction() as c:
            self._get(c,token,project)
            return [dict(r) for r in c.execute('SELECT * FROM maintenance_incidents WHERE project=? ORDER BY first_seen,id',(project,))]

    def reproduction(self,token,project,incident_id,*,fixture_id):
        """Trusted fixture registry, not automatic copying of private failure payloads."""
        name(fixture_id)
        _reject_credentials({'fixture_id':fixture_id,'incident':incident_id},token)
        fixtures=self.fixtures
        if type(fixtures) is not dict or fixture_id not in fixtures:raise NotFound('synthetic reproduction fixture not registered')
        spec=fixtures[fixture_id]
        if type(spec) is not dict or set(spec)!={'project','check','source_sha'} or spec['project']!=project or not re.fullmatch('[a-f0-9]{40}',spec['source_sha']):
            raise ValueError('exact project fixture and commit required')
        name(spec['check'])
        _reject_credentials(spec,token)
        with self.store._transaction() as c:
            self._get(c,token,project)
            row=c.execute('SELECT * FROM maintenance_incidents WHERE id=? AND project=?',(incident_id,project)).fetchone()
            if row is None:raise NotFound('incident not in project')
        return {'project':project,'incident':incident_id,'fixture_id':fixture_id,**spec,
                'fixture_only':True,'private_payload_copied':False,'state':'pending_approval'}
