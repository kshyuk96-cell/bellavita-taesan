"""Scoped inventory and bounded session provisioning on registered hosts.

A host is an explicit server registration, not an account-wide Codex UI API.
Provisioning only creates session metadata. Executing work requires the separate
approved task/worker lease transport. Unknown create outcomes are never replayed.
"""
from dataclasses import dataclass, asdict
import json
import math
import re
from .maintenance_registry import name, receipt
from .store import (Conflict, NotFound, PermissionDenied, canonical_json, digest,
                    _sha, _task_id, _reject_credentials)
from .agent_directory import _reject_known_tokens

KINDS=frozenset({'codex_cli','codex_appserver','agents_api','existing_ui'})
MAX_OBSERVATIONS=200


def _time(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
        raise ValueError('finite positive observation time required')


def _repository(config):
    return config['source_ref'] if config['source_kind']=='github' else 'sites/'+config['source_ref']


@dataclass(frozen=True)
class SessionObservation:
    project:str
    external_ref:str
    session_id:str
    kind:str
    repository:str
    source_sha:str
    can_receive:bool
    reference:str
    observed_at:float
    def __post_init__(self):
        name(self.project);name(self.external_ref);name(self.session_id)
        if type(self.kind) is not str or self.kind not in KINDS:raise ValueError('known session surface required')
        receipt(self.repository);_sha(self.source_sha);receipt(self.reference);_time(self.observed_at)
        for value in (self.external_ref,self.session_id,self.reference):
            if value.startswith(('/', 'sk-', 'ghp_', 'github_pat_', 'AIza')):raise ValueError('public opaque session metadata required')
        if type(self.can_receive) is not bool:raise ValueError('explicit host transport availability required')
        if self.kind=='existing_ui' and self.can_receive:raise ValueError('unverified UI control cannot be registered as supported')


@dataclass(frozen=True)
class DeliveryObservation:
    project:str
    session_id:str
    task_id:str
    task_version:int
    manifest_digest:str
    execution_ref:str
    state:str
    completed_commands:int
    failed_commands:int
    turn_completed:bool
    reference:str
    observed_at:float
    worker_subject_digest:str
    lease_digest:str
    def __post_init__(self):
        name(self.project);name(self.session_id);_task_id(self.task_id)
        if type(self.task_version) is not int or self.task_version<1:raise ValueError('exact task version required')
        for value in (self.manifest_digest,self.worker_subject_digest,self.lease_digest):
            if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('exact receipt binding required')
        receipt(self.execution_ref);receipt(self.reference);_time(self.observed_at)
        for value in (self.session_id,self.execution_ref,self.reference):
            if value.startswith(('/', 'sk-', 'ghp_', 'github_pat_', 'AIza')):raise ValueError('public execution metadata required')
        if self.state not in {'commands_observed','turn_completed','execution_failed','uncertain'}:raise ValueError('typed execution outcome required')
        if any(type(v) is not int or not 0<=v<=4000 for v in (self.completed_commands,self.failed_commands)) or type(self.turn_completed) is not bool:raise ValueError('actual bounded event counts required')
        if self.state=='commands_observed' and (not self.turn_completed or not self.completed_commands or self.failed_commands):raise ValueError('successful command events required')
        if self.state=='turn_completed' and (not self.turn_completed or self.completed_commands or self.failed_commands):raise ValueError('turn-only evidence required')


@dataclass(frozen=True)
class FleetProvider:
    discover:object
    create:object=None
    discovery_read_only:bool=True
    delivery_observer:object=None
    def __post_init__(self):
        if not callable(self.discover) or (self.create is not None and not callable(self.create)) or self.discovery_read_only is not True or (self.delivery_observer is not None and not callable(self.delivery_observer)):
            raise ValueError('trusted host discovery and optional provisioning required')


class Fleet:
    def __init__(self,store,registry,providers=None,*,max_age=300):
        if registry.store is not store:raise ValueError('shared project registry required')
        if type(max_age) is not int or not 1<=max_age<=300:raise ValueError('bounded freshness required')
        self.store,self.registry,self.max_age=store,registry,max_age
        self.providers=dict(providers or {})
        for key,provider in self.providers.items():
            if type(key) is not tuple or len(key)!=2 or not isinstance(provider,FleetProvider):raise ValueError('explicit project host provider required')
            for value in key:name(value)
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS fleet_hosts (
                project TEXT,host_ref TEXT,config_fingerprint TEXT,owner_hash TEXT,created REAL,
                PRIMARY KEY(project,host_ref));
            CREATE TABLE IF NOT EXISTS fleet_templates (
                project TEXT,template_ref TEXT,host_ref TEXT,config_fingerprint TEXT,owner_hash TEXT,
                max_sessions INTEGER,expires REAL,created REAL,
                PRIMARY KEY(project,template_ref));
            CREATE TABLE IF NOT EXISTS fleet_sessions (
                project TEXT,host_ref TEXT,external_ref TEXT,session_id TEXT,kind TEXT,repository TEXT,
                source_sha TEXT,can_receive INTEGER,reference TEXT,observed_at REAL,enrolled INTEGER,
                config_fingerprint TEXT,created_by TEXT,
                PRIMARY KEY(project,host_ref,external_ref));
            CREATE TABLE IF NOT EXISTS fleet_creates (
                id TEXT PRIMARY KEY,actor_hash TEXT,project TEXT,task_id TEXT,template_ref TEXT,
                request_key TEXT,binding TEXT,config_fingerprint TEXT,state TEXT,external_ref TEXT,
                created REAL,updated REAL,UNIQUE(actor_hash,project,request_key));
            CREATE TABLE IF NOT EXISTS fleet_deliveries (
                project TEXT,host_ref TEXT,external_ref TEXT,task_id TEXT,observation TEXT,config_fingerprint TEXT,
                observed_at REAL,PRIMARY KEY(project,host_ref,external_ref,task_id));
            CREATE TABLE IF NOT EXISTS fleet_discoveries (
                project TEXT,host_ref TEXT,state TEXT,observed_at REAL,
                PRIMARY KEY(project,host_ref));
            ''')

    def _auth(self,c,token,project,roles=('owner','manager')):
        actor=self.store._authenticate(c,token);self.store._role(actor,roles)
        config=self.registry._get(c,token,project,roles)
        return actor,config

    def _owner_live(self,c,hashed,project):
        row=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(hashed,)).fetchone()
        now=self.store._now()
        if row is None or row['role']!='owner' or row['revoked'] is not None or (row['expires'] is not None and row['expires']<=now):
            raise PermissionDenied('fleet owner authorization expired or revoked')
        identity={'role':row['role'],'projects':json.loads(row['projects'])}
        if not self.store._scope(identity,project):raise PermissionDenied('fleet owner outside project')
        web=c.execute('SELECT parent_hash FROM bridge_web_sessions WHERE token_hash=?',(hashed,)).fetchone()
        if web:
            parent=c.execute('SELECT revoked,expires FROM bridge_tokens WHERE token_hash=?',(web['parent_hash'],)).fetchone()
            if parent is None or parent['revoked'] is not None or (parent['expires'] is not None and parent['expires']<=now):
                raise PermissionDenied('fleet owner parent session unavailable')

    def _host(self,c,token,project,host_ref,expected=None):
        actor,config=self._auth(c,token,project)
        row=c.execute('SELECT * FROM fleet_hosts WHERE project=? AND host_ref=?',(project,host_ref)).fetchone()
        if row is None:raise NotFound('host not registered')
        if row['config_fingerprint']!=config['fingerprint'] or (expected is not None and expected!=config['fingerprint']):
            raise Conflict('fleet host project binding changed')
        self._owner_live(c,row['owner_hash'],project)
        return actor,config,row

    def register_host(self,token,project,host_ref):
        name(project);name(host_ref)
        if (project,host_ref) not in self.providers:raise NotFound('trusted host provider is not configured')
        with self.store._transaction() as c:
            actor,config=self._auth(c,token,project,('owner',))
            _reject_known_tokens(c,{'project':project,'host_ref':host_ref})
            existing=c.execute('SELECT * FROM fleet_hosts WHERE project=? AND host_ref=?',(project,host_ref)).fetchone()
            if existing and (existing['config_fingerprint']!=config['fingerprint'] or existing['owner_hash']!=actor['token_hash']):
                raise Conflict('existing host registration changed; explicit reconciliation required')
            c.execute('INSERT OR IGNORE INTO fleet_hosts VALUES (?,?,?,?,?)',(project,host_ref,config['fingerprint'],actor['token_hash'],self.store._now()))
        return {'project':project,'host_ref':host_ref,'state':'registered','connection_verified':False,'operational_change':False}

    def register_template(self,token,project,template_ref,host_ref,*,max_sessions,expires_at):
        name(project);name(template_ref);name(host_ref);_time(expires_at)
        if type(max_sessions) is not int or not 1<=max_sessions<=100:raise ValueError('explicit creation cap 1..100 required')
        with self.store._transaction() as c:
            actor,config=self._auth(c,token,project,('owner',))
            self._host(c,token,project,host_ref)
            if not self.store._now()<expires_at<=self.store._now()+7*86400:raise ValueError('future template expiry within seven days required')
            if self.providers[(project,host_ref)].create is None:raise Conflict('host session provisioning not configured')
            value=(host_ref,config['fingerprint'],actor['token_hash'],max_sessions,float(expires_at))
            old=c.execute('SELECT * FROM fleet_templates WHERE project=? AND template_ref=?',(project,template_ref)).fetchone()
            if old and tuple(old[k] for k in ('host_ref','config_fingerprint','owner_hash','max_sessions','expires'))!=value:
                raise Conflict('template changes require a new explicit template reference')
            _reject_known_tokens(c,{'template_ref':template_ref,'host_ref':host_ref})
            c.execute('INSERT OR IGNORE INTO fleet_templates VALUES (?,?,?,?,?,?,?,?)',(project,template_ref,*value,self.store._now()))
        return {'project':project,'template_ref':template_ref,'host_ref':host_ref,'max_sessions':max_sessions,
                'expires_at':float(expires_at),'new_work_approved':False,'operational_change':False}

    def _observations(self,value,project,config):
        if type(value) is not tuple or len(value)>MAX_OBSERVATIONS or any(not isinstance(item,SessionObservation) for item in value):
            raise Conflict('bounded typed host inventory required')
        refs=set();sessions=set()
        for item in value:
            if item.project!=project or item.repository!=_repository(config['config']):raise Conflict('host session source scope mismatch')
            if not 0<=self.store._now()-item.observed_at<=self.max_age:raise Conflict('fresh host session observation required')
            if item.external_ref in refs or item.session_id in sessions:raise Conflict('ambiguous duplicate host session reference')
            refs.add(item.external_ref);sessions.add(item.session_id)
        return value

    def _save_observation(self,c,actor,config,host_ref,item,*,enrolled=False):
        existing=c.execute('SELECT * FROM fleet_sessions WHERE project=? AND host_ref=? AND external_ref=?',(item.project,host_ref,item.external_ref)).fetchone()
        alias=c.execute('SELECT 1 FROM fleet_sessions WHERE project=? AND host_ref=? AND session_id=? AND external_ref!=?',(item.project,host_ref,item.session_id,item.external_ref)).fetchone()
        if alias:raise Conflict('session already bound under another reference')
        if existing and (existing['session_id'],existing['kind'],existing['repository'])!=(item.session_id,item.kind,item.repository):
            raise Conflict('registered session identity changed')
        _reject_known_tokens(c,{'external_ref':item.external_ref,'session_id':item.session_id,'reference':item.reference})
        enrolled=bool(enrolled or (existing and existing['enrolled']))
        c.execute('INSERT OR REPLACE INTO fleet_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (item.project,host_ref,item.external_ref,item.session_id,item.kind,item.repository,item.source_sha,int(item.can_receive),
                   item.reference,item.observed_at,int(enrolled),config['fingerprint'],actor['token_hash']))

    def discover(self,token,project,host_ref):
        name(project);name(host_ref)
        with self.store._transaction() as c:
            actor,config,_=self._host(c,token,project,host_ref)
        provider=self.providers.get((project,host_ref))
        if provider is None:raise Conflict('trusted discovery provider unavailable')
        try:observed=self._observations(provider.discover(project),project,config)
        except Exception:
            with self.store._transaction() as c:
                self._host(c,token,project,host_ref,config['fingerprint'])
                c.execute('INSERT OR REPLACE INTO fleet_discoveries VALUES (?,?,?,?)',(project,host_ref,'unavailable',self.store._now()))
            raise Conflict('host discovery unavailable; no sessions executed') from None
        with self.store._transaction() as c:
            actor,current,_=self._host(c,token,project,host_ref,config['fingerprint'])
            for item in observed:self._save_observation(c,actor,current,host_ref,item)
            # A successful bounded inventory is authoritative for this host scope.
            refs=tuple(item.external_ref for item in observed)
            suffix=(' AND external_ref NOT IN ('+','.join('?' for _ in refs)+')') if refs else ''
            c.execute('UPDATE fleet_sessions SET can_receive=0,observed_at=0 WHERE project=? AND host_ref=? '
                      'AND (can_receive!=0 OR observed_at!=0)'+suffix,(project,host_ref,*refs))
            c.execute('INSERT OR REPLACE INTO fleet_discoveries VALUES (?,?,?,?)',(project,host_ref,'observed',self.store._now()))
        return {'project':project,'host_ref':host_ref,'state':'observed','observed_count':len(observed),
                'account_wide_inventory_verified':False,'operational_change':False}

    def enroll(self,token,project,host_ref,external_ref):
        name(external_ref);self.discover(token,project,host_ref)
        with self.store._transaction() as c:
            actor,config,_=self._host(c,token,project,host_ref)
            row=c.execute('SELECT * FROM fleet_sessions WHERE project=? AND host_ref=? AND external_ref=?',(project,host_ref,external_ref)).fetchone()
            if row is None or row['observed_at']<=0 or self.store._now()-row['observed_at']>self.max_age:raise NotFound('requested session not observed on registered host')
            if row['kind']=='existing_ui':raise Conflict('existing UI transport unsupported')
            if not row['can_receive']:raise Conflict('session receiving transport is not ready')
            c.execute('UPDATE fleet_sessions SET enrolled=1 WHERE project=? AND host_ref=? AND external_ref=?',(project,host_ref,external_ref))
            return self._public_session(dict(row),config,enrolled=True)

    def _template(self,c,token,project,template_ref):
        actor,config=self._auth(c,token,project)
        row=c.execute('SELECT * FROM fleet_templates WHERE project=? AND template_ref=?',(project,template_ref)).fetchone()
        if row is None:raise NotFound('session template not registered')
        self._host(c,token,project,row['host_ref'],config['fingerprint']);self._owner_live(c,row['owner_hash'],project)
        if row['config_fingerprint']!=config['fingerprint']:raise Conflict('template project binding changed')
        if row['expires']<=self.store._now():raise PermissionDenied('session creation authorization expired')
        if not config['config']['maintenance_enabled']:raise PermissionDenied('project development disabled')
        return actor,config,row

    def _approved_task(self,c,token,task_id):
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        row=self.store._get(c,actor,task_id)
        if row['state']!='queued':raise Conflict('only an exact approved queued task may provision a session')
        approval=json.loads(row['approval']) if row['approval'] else {}
        policy=c.execute('SELECT policy_digest FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone()
        if approval.get('version')!=row['version'] or approval.get('manifest_digest')!=row['manifest_digest'] or policy is None or approval.get('policy_digest')!=policy['policy_digest']:
            raise Conflict('current owner/delegated task and path approval required')
        self.store._validate_execution(c,row,check_source_age=True)
        return row,self.store._public(row)

    def _uncertain(self,identifier):
        with self.store._transaction() as c:
            c.execute("UPDATE fleet_creates SET state='uncertain',updated=? WHERE id=? AND state='creating'",(self.store._now(),identifier))

    def create_for_task(self,token,task_id,template_ref,request_key):
        _task_id(task_id);name(template_ref);name(request_key)
        with self.store._transaction() as c:
            task,public=self._approved_task(c,token,task_id);project=task['project']
            actor,config,template=self._template(c,token,project,template_ref)
            binding=digest({'task':task_id,'version':task['version'],'manifest':task['manifest_digest'],
                            'template':template_ref,'host':template['host_ref'],'config':config['fingerprint']})
            old=c.execute('SELECT * FROM fleet_creates WHERE actor_hash=? AND project=? AND request_key=?',(actor['token_hash'],project,request_key)).fetchone()
            if old:
                if old['binding']!=binding:raise Conflict('session provisioning request changed')
                return self._public_create(old)
            # Count reservations too. A crash or timeout does not refund capacity.
            used=c.execute('SELECT COUNT(*) FROM fleet_creates WHERE project=? AND template_ref=?',(project,template_ref)).fetchone()[0]
            if used>=template['max_sessions']:raise Conflict('approved new session cap reached')
            identifier=digest({'actor':actor['token_hash'],'project':project,'request':request_key})
            now=self.store._now()
            c.execute("INSERT INTO fleet_creates VALUES (?,?,?,?,?,?,?,?, 'creating',NULL,?,?)",
                      (identifier,actor['token_hash'],project,task_id,template_ref,request_key,binding,config['fingerprint'],now,now))
            host_ref=template['host_ref']
        provider=self.providers.get((project,host_ref))
        try:
            if provider is None or provider.create is None:raise Conflict('session provisioning host unavailable')
            # Reauthorize immediately before a host-side mutation, then again after.
            with self.store._transaction() as c:
                before_task,_=self._approved_task(c,token,task_id)
                _,before_config,before_template=self._template(c,token,project,template_ref)
                if before_task['version']!=task['version'] or before_task['manifest_digest']!=task['manifest_digest'] or before_config['fingerprint']!=config['fingerprint'] or before_template['host_ref']!=template['host_ref']:
                    raise Conflict('session creation binding changed before provisioning')
            observation=provider.create(project,public,identifier)
            self._observations((observation,),project,config)
            if observation.source_sha!=public['manifest']['base_sha']:raise Conflict('new session source differs from approved base')
            if observation.kind=='existing_ui' or not observation.can_receive:raise Conflict('provisioned session does not support registered receiving')
            with self.store._transaction() as c:
                current_task,_=self._approved_task(c,token,task_id)
                fresh_actor,fresh_config,fresh_template=self._template(c,token,project,template_ref)
                if config['fingerprint']!=fresh_config['fingerprint'] or template['host_ref']!=fresh_template['host_ref'] or current_task['manifest_digest']!=task['manifest_digest'] or current_task['version']!=task['version']:
                    raise Conflict('session creation binding changed during provisioning')
                intent=c.execute('SELECT * FROM fleet_creates WHERE id=?',(identifier,)).fetchone()
                if intent is None or intent['state']!='creating':raise Conflict('session creation intent changed')
                if c.execute('SELECT 1 FROM fleet_sessions WHERE project=? AND host_ref=? AND external_ref=?',(project,host_ref,observation.external_ref)).fetchone():
                    raise Conflict('new creation returned an already observed session')
                self._save_observation(c,fresh_actor,fresh_config,host_ref,observation,enrolled=True)
                c.execute("UPDATE fleet_creates SET state='created',external_ref=?,updated=? WHERE id=?",(observation.external_ref,self.store._now(),identifier))
                result=self._public_create(c.execute('SELECT * FROM fleet_creates WHERE id=?',(identifier,)).fetchone())
            return result
        except BaseException as error:
            self._uncertain(identifier)
            if not isinstance(error,Exception):raise
            if isinstance(error,PermissionDenied):raise PermissionDenied('session provisioning authority changed; reconcile without replay') from None
            raise Conflict('session provisioning outcome requires reconciliation; not automatically retried') from None

    def get_creation(self,token,creation_id):
        if type(creation_id) is not str or not re.fullmatch(r'[a-f0-9]{64}',creation_id):raise ValueError('exact creation reference required')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            row=c.execute('SELECT * FROM fleet_creates WHERE id=?',(creation_id,)).fetchone()
            if row is None:raise NotFound('session creation record unavailable')
            self._auth(c,token,row['project'])
            return self._public_create(row)

    @staticmethod
    def _public_create(row):
        return {'id':row['id'],'project':row['project'],'task_id':row['task_id'],'template_ref':row['template_ref'],
                'state':row['state'],'external_ref':row['external_ref'],'execution_started':False,
                'automatic_retry_safe':False,'existing_ui_chat_control_verified':False,'operational_change':False}

    def _public_session(self,row,config,*,enrolled=None):
        age=self.store._now()-row['observed_at']
        selected=bool(row['enrolled']) if enrolled is None else enrolled
        state=('unsupported' if row['kind']=='existing_ui' else 'stale' if not 0<=age<=self.max_age else
               'ready' if selected and row['can_receive'] else 'observed')
        if row['config_fingerprint']!=config['fingerprint']:state='stale'
        return {'project':row['project'],'host_ref':row['host_ref'],'external_ref':row['external_ref'],
                'session_id':row['session_id'],'kind':row['kind'],'repository':row['repository'],'source_sha':row['source_sha'],
                'state':state,'enrolled':selected,'host_transport_observed':state=='ready',
                'instruction_delivery_verified':False,'existing_ui_chat_control_verified':False}

    def _rework_source(self,c,task,session,config):
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hierarchy_rework_checkpoints'").fetchone():return False
        checkpoint=c.execute('SELECT * FROM hierarchy_rework_checkpoints WHERE task_id=? AND lease_hash=?',(task['id'],task['lease_hash'])).fetchone()
        hierarchy=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone()
        if checkpoint is None or hierarchy is None or hierarchy['state']!='rework_developing' or not 1<=hierarchy['rework_count']<=2:return False
        if task['lease_expires'] is None or task['lease_expires']<=self.store._now():return False
        if any(checkpoint[k]!=expected for k,expected in (('version',task['version']),('manifest_digest',task['manifest_digest']),('config_fingerprint',config['fingerprint']),('worker_hash',task['worker_token_hash']),('developer_id',hierarchy['developer_id']),('rework_count',hierarchy['rework_count']))):return False
        if any(hierarchy[k]!=expected for k,expected in (('project',task['project']),('version',task['version']),('manifest_digest',task['manifest_digest']),('config_fingerprint',config['fingerprint']))):return False
        member=c.execute("SELECT * FROM hierarchy_members WHERE project=? AND member_id=? AND role='developer'",(task['project'],hierarchy['developer_id'])).fetchone()
        if member is None or member['credential_hash']!=task['worker_token_hash'] or member['subject']!=task['worker_subject'] or member['config_fingerprint']!=config['fingerprint']:return False
        candidate=json.loads(checkpoint['candidate']);identity=candidate['identity'];source=candidate['source']
        if digest(identity)!=candidate['digest'] or source['head_sha']!=session['source_sha'] or identity['head_sha']!=session['source_sha']:return False
        if any(source[k]!=identity[k] for k in ('base_sha','head_sha','artifact_digest','checks_digest','runtime_digest','changed_paths')):return False
        prior=candidate['task']
        if prior['id']!=task['id'] or prior['version']!=task['version'] or prior['manifest_digest']!=task['manifest_digest'] or digest(prior['report'])!=identity['report_digest']:return False
        if any(identity[k]!=expected for k,expected in (('task_id',task['id']),('version',task['version']),('manifest_digest',task['manifest_digest']),('config_fingerprint',config['fingerprint']),('developer_subject',task['worker_subject']),('base_sha',json.loads(task['manifest'])['base_sha']))):return False
        return (source['project']==task['project'] and source['task_id']==task['id'] and source['base_sha']==identity['base_sha'] and candidate['config_fingerprint']==config['fingerprint'] and candidate['developer_subject']==task['worker_subject'])

    def _delivery_binding(self,c,token,project,host_ref,external_ref,task_id):
        actor,config,_=self._host(c,token,project,host_ref)
        discovery=c.execute('SELECT * FROM fleet_discoveries WHERE project=? AND host_ref=?',(project,host_ref)).fetchone()
        if discovery and (discovery['state']!='observed' or not 0<=self.store._now()-discovery['observed_at']<=self.max_age):raise Conflict('current host observation unavailable')
        session=c.execute('SELECT * FROM fleet_sessions WHERE project=? AND host_ref=? AND external_ref=?',(project,host_ref,external_ref)).fetchone()
        if session is None:raise NotFound('session not registered')
        if self._public_session(session,config)['state']!='ready':raise Conflict('supported enrolled fresh session required')
        task=self.store._get(c,actor,task_id)
        if task['project']!=project or task['state'] not in {'running','reported','reviewed'}:raise Conflict('current assigned execution required')
        manifest=json.loads(task['manifest']);approval=json.loads(task['approval'] or '{}')
        policy=c.execute('SELECT policy_digest FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone()
        if manifest['repository']!=session['repository']:raise Conflict('execution source mismatch')
        if session['source_sha']!=manifest['base_sha'] and not self._rework_source(c,task,session,config):raise Conflict('execution source mismatch')
        if approval.get('version')!=task['version'] or approval.get('manifest_digest')!=task['manifest_digest'] or policy is None or approval.get('policy_digest')!=policy['policy_digest']:raise Conflict('current exact execution approval required')
        self.store._validate_execution(c,task,check_source_age=False)
        if not task['worker_subject'] or not task['lease_hash'] or not task['worker_token_hash']:raise Conflict('assigned worker receipt required')
        worker=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(task['worker_token_hash'],)).fetchone()
        if worker is None or worker['revoked'] is not None or (worker['expires'] is not None and worker['expires']<=self.store._now()):raise PermissionDenied('worker authorization unavailable')
        if task['state']=='running' and task['lease_expires']<=self.store._now():raise Conflict('execution lease expired')
        binding=(config['fingerprint'],session['session_id'],task['version'],task['manifest_digest'],digest(task['worker_subject']),task['lease_hash'])
        checkpoint=None
        if session['source_sha']!=manifest['base_sha']:
            checkpoint=dict(c.execute('SELECT * FROM hierarchy_rework_checkpoints WHERE task_id=? AND lease_hash=?',(task['id'],task['lease_hash'])).fetchone())
        return config,binding+(digest({'source_sha':session['source_sha'],'checkpoint':checkpoint}),)

    @staticmethod
    def _delivery_public(item,verified):
        return {'task_id':item.task_id,'session_id':item.session_id,'execution_ref':item.execution_ref,
                'state':item.state,'completed_commands':item.completed_commands,'failed_commands':item.failed_commands,
                'turn_completed':item.turn_completed,'reference':item.reference,'observed_at':item.observed_at,
                'instruction_delivery_verified':verified,'tests_verified':False,'technical_accepted':False,
                'existing_ui_chat_control_verified':False,'operational_change':False}

    def verify_delivery(self,token,project,host_ref,external_ref,task_id):
        for value in (project,host_ref,external_ref):name(value)
        _task_id(task_id)
        with self.store._transaction() as c:config,binding=self._delivery_binding(c,token,project,host_ref,external_ref,task_id)
        provider=self.providers.get((project,host_ref))
        if provider is None or provider.delivery_observer is None:raise Conflict('actual delivery observer unavailable')
        try:item=provider.delivery_observer(project,external_ref,task_id)
        except Exception:raise Conflict('actual delivery observation unavailable') from None
        if not isinstance(item,DeliveryObservation):raise Conflict('typed actual execution receipt required')
        expected=(config['fingerprint'],item.session_id,item.task_version,item.manifest_digest,item.worker_subject_digest,item.lease_digest)
        if item.project!=project or item.task_id!=task_id or expected!=binding[:6] or not 0<=self.store._now()-item.observed_at<=self.max_age:raise Conflict('execution receipt binding mismatch')
        with self.store._transaction() as c:
            _,current=self._delivery_binding(c,token,project,host_ref,external_ref,task_id)
            if current!=binding:raise Conflict('execution assignment changed during observation')
            _reject_known_tokens(c,asdict(item))
            c.execute('INSERT OR REPLACE INTO fleet_deliveries VALUES (?,?,?,?,?,?,?)',(project,host_ref,external_ref,task_id,canonical_json(asdict(item)),config['fingerprint'],item.observed_at))
            c.execute('DELETE FROM fleet_deliveries WHERE project=? AND host_ref=? AND external_ref=? AND task_id NOT IN (SELECT task_id FROM fleet_deliveries WHERE project=? AND host_ref=? AND external_ref=? ORDER BY observed_at DESC,task_id LIMIT 20)',(project,host_ref,external_ref,project,host_ref,external_ref))
        return self._delivery_public(item,item.state=='commands_observed')

    def _inventory_delivery(self,c,token,row,item):
        record=c.execute('SELECT * FROM fleet_deliveries WHERE project=? AND host_ref=? AND external_ref=? ORDER BY observed_at DESC,task_id LIMIT 1',(row['project'],row['host_ref'],row['external_ref'])).fetchone()
        if record is None:return
        evidence=DeliveryObservation(**json.loads(record['observation']));verified=False
        try:
            _,binding=self._delivery_binding(c,token,row['project'],row['host_ref'],row['external_ref'],record['task_id'])
            expected=(record['config_fingerprint'],evidence.session_id,evidence.task_version,evidence.manifest_digest,evidence.worker_subject_digest,evidence.lease_digest)
            verified=(item['state']=='ready' and binding[:6]==expected and 0<=self.store._now()-evidence.observed_at<=self.max_age and evidence.state=='commands_observed')
        except (Conflict,PermissionDenied,NotFound):pass
        item['last_delivery']=self._delivery_public(evidence,verified)
        item['instruction_delivery_verified']=verified

    def inventory(self,token,projects):
        if type(projects) not in (tuple,list) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):raise ValueError('explicit project list required')
        with self.store._transaction() as c:
            configs={project:self._auth(c,token,project)[1] for project in projects}
            output=[]
            for project,config in configs.items():
                hosts=[]
                for host in c.execute('SELECT * FROM fleet_hosts WHERE project=? ORDER BY host_ref',(project,)):
                    discovery=c.execute('SELECT * FROM fleet_discoveries WHERE project=? AND host_ref=?',(project,host['host_ref'])).fetchone()
                    state='registered'
                    try:self._host(c,token,project,host['host_ref'])
                    except (Conflict,PermissionDenied,NotFound):state='stale'
                    else:
                        if (project,host['host_ref']) not in self.providers:state='unavailable'
                        elif discovery:state=discovery['state'] if 0<=self.store._now()-discovery['observed_at']<=self.max_age else 'stale'
                    hosts.append({'host_ref':host['host_ref'],'state':state})
                host_states={h['host_ref']:h['state'] for h in hosts}
                sessions=[]
                for row in c.execute('SELECT * FROM fleet_sessions WHERE project=? ORDER BY host_ref,external_ref LIMIT 201',(project,)):
                    item=self._public_session(dict(row),config)
                    if host_states.get(row['host_ref']) in {'unavailable','stale'}:
                        item.update(state=host_states[row['host_ref']],host_transport_observed=False)
                    self._inventory_delivery(c,token,row,item)
                    sessions.append(item)
                uncertain=c.execute("SELECT COUNT(*) FROM fleet_creates WHERE project=? AND state IN ('creating','uncertain')",(project,)).fetchone()[0]
                session_count=c.execute('SELECT COUNT(*) FROM fleet_sessions WHERE project=?',(project,)).fetchone()[0]
                output.append({'project':project,'hosts':hosts,'sessions':sessions[:200],'displayed_session_count':min(session_count,200),'total_session_count':session_count,'sessions_truncated':session_count>200,'uncertain_creations':uncertain})
            result={'projects':output,'account_wide_inventory_verified':False,'existing_ui_chat_control_verified':False,'operational_change':False}
            _reject_known_tokens(c,result)
            return result
