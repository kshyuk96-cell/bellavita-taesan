"""Three-tier technical acceptance over existing approved tasks.

Host observations and independent execution evidence are trusted registrations,
never HTTP fields. This module neither sends messages to arbitrary UI chats nor
merges, deploys, or grants operational approval.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
import math
import secrets
from .store import Conflict, NotFound, PermissionDenied, canonical_json, digest, _sha, _task_id, token_hash
from .maintenance_registry import name, receipt
from .coordination import scope_violations

ROLES=frozenset({'developer','reviewer','taesan'})
STATES=frozenset({'development_queued','developing','candidate_ready','reviewing','review_passed','review_failed','rework_queued','rework_developing','needs_verification','accepted'})


def _hash(value):
    if type(value) is not str or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):raise ValueError('exact digest required')
    return value


def _time(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise ValueError('observation time required')


@dataclass(frozen=True)
class SourceObservation:
    project:str
    task_id:str
    base_sha:str
    head_sha:str
    artifact_digest:str
    checks_digest:str
    reference:str
    observed_at:float
    changed_paths:tuple=()
    runtime_digest:str|None=None
    def __post_init__(self):
        name(self.project);_task_id(self.task_id);_sha(self.base_sha);_sha(self.head_sha)
        _hash(self.artifact_digest);_hash(self.checks_digest);_hash(self.runtime_digest);receipt(self.reference);_time(self.observed_at)
        from .coordination import validate_paths
        if type(self.changed_paths) is not tuple or len(self.changed_paths)>100:raise ValueError('bounded exact changed paths required')
        if self.changed_paths:validate_paths(list(self.changed_paths))


@dataclass(frozen=True)
class CheckOutcome:
    name:str
    argv_digest:str
    exit_code:int
    elapsed_seconds:float
    def __post_init__(self):
        if type(self.name) is not str or not self.name or len(self.name)>200:raise ValueError('check name required')
        _hash(self.argv_digest)
        if type(self.exit_code) is not int or not -255<=self.exit_code<=255:raise ValueError('actual command exit required')
        if type(self.elapsed_seconds) not in (int,float) or not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds<0:raise ValueError('actual elapsed required')


@dataclass(frozen=True)
class CodeAssessment:
    candidate_digest:str
    reviewer_subject:str
    recommendation:str
    findings_digest:str
    reference:str
    observed_at:float
    def __post_init__(self):
        _hash(self.candidate_digest);_hash(self.findings_digest);receipt(self.reference);_time(self.observed_at)
        if type(self.reviewer_subject) is not str or not self.reviewer_subject or len(self.reviewer_subject)>100:raise ValueError('code reviewer identity required')
        if self.recommendation not in ('pass','rework'):raise ValueError('exact independent code review recommendation required')


@dataclass(frozen=True)
class ReviewEvidence:
    candidate_digest:str
    reviewer_subject:str
    checks:tuple
    artifact_digest:str
    reference:str
    observed_at:float
    isolation_verified:bool=False
    runtime_digest:str|None=None
    code_assessment:CodeAssessment|None=None
    def __post_init__(self):
        if self.code_assessment is not None and not isinstance(self.code_assessment,CodeAssessment):raise ValueError('typed independent code assessment required')
        _hash(self.candidate_digest);_hash(self.artifact_digest);_hash(self.runtime_digest);receipt(self.reference);_time(self.observed_at)
        if type(self.reviewer_subject) is not str or not self.reviewer_subject or len(self.reviewer_subject)>100:raise ValueError('reviewer subject required')
        if type(self.checks) is not tuple or not 1<=len(self.checks)<=100 or any(not isinstance(x,CheckOutcome) for x in self.checks):raise ValueError('typed actual checks required')
        if len({x.name for x in self.checks})!=len(self.checks):raise ValueError('unique checks required')
        if type(self.isolation_verified) is not bool:raise ValueError('actual isolation required')


@dataclass(frozen=True)
class StopObservation:
    project:str
    task_id:str
    candidate_digest:str
    lease_hash:str
    reviewer_subject:str
    stopped:bool
    reference:str
    observed_at:float
    def __post_init__(self):
        name(self.project);_task_id(self.task_id);_hash(self.candidate_digest);_hash(self.lease_hash)
        if type(self.reviewer_subject) is not str or not self.reviewer_subject or len(self.reviewer_subject)>100:raise ValueError('exact reviewer identity required')
        if type(self.stopped) is not bool:raise ValueError('actual stop observation required')
        receipt(self.reference);_time(self.observed_at)


class ThreeTier:
    def __init__(self,store,registry,*,source_observers=None,review_hosts=None,stop_observers=None):
        if registry.store is not store:raise ValueError('shared registered store required')
        self.store,self.registry=store,registry
        self.source_observers=dict(source_observers or {});self.review_hosts=dict(review_hosts or {})
        self.stop_observers=dict(stop_observers or {})
        for collection in (self.source_observers,self.review_hosts,self.stop_observers):
            for project,callback in collection.items():
                name(project)
                if not callable(callback):raise ValueError('registered host callback required')
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS hierarchy_members (
                project TEXT, member_id TEXT, credential_hash TEXT, subject TEXT, role TEXT, agent_id TEXT,
                config_fingerprint TEXT, PRIMARY KEY(project,member_id));
            CREATE TABLE IF NOT EXISTS hierarchy_tasks (
                task_id TEXT PRIMARY KEY, project TEXT, version INTEGER, manifest_digest TEXT, config_fingerprint TEXT,
                state TEXT, developer_id TEXT, reviewer_id TEXT, rework_count INTEGER, candidate TEXT,
                evidence TEXT, lease_hash TEXT, lease_expires REAL, request_key TEXT, reason TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS hierarchy_rework_checkpoints (
                task_id TEXT,lease_hash TEXT,version INTEGER,manifest_digest TEXT,config_fingerprint TEXT,
                developer_id TEXT,worker_hash TEXT,rework_count INTEGER,candidate TEXT,
                PRIMARY KEY(task_id,lease_hash));
            CREATE TABLE IF NOT EXISTS hierarchy_review_runs (task_id TEXT,lease_hash TEXT,state TEXT,candidate_digest TEXT,PRIMARY KEY(task_id,lease_hash));
            CREATE TABLE IF NOT EXISTS hierarchy_review_limits (scope TEXT PRIMARY KEY,max_active INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS hierarchy_next_assignments (request_key TEXT PRIMARY KEY,parent_id TEXT,next_id TEXT,parent_digest TEXT,developer_id TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS hierarchy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,kind TEXT,state TEXT,recorded REAL,payload_digest TEXT);
            ''')

    def _config(self,c,token,project,roles=('owner','manager','worker')):
        config=self.registry._get(c,token,project,roles)
        return config

    def _live(self,c,hashed,project):
        row=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(hashed,)).fetchone();now=self.store._now()
        if row is None or row['revoked'] is not None or (row['expires'] is not None and row['expires']<=now):raise PermissionDenied('hierarchy identity unavailable')
        identity={'role':row['role'],'projects':json.loads(row['projects'])}
        if not self.store._scope(identity,project):raise PermissionDenied('hierarchy identity outside project')
        # Web session parent revocation must invalidate the registered child too.
        web=c.execute('SELECT parent_hash FROM bridge_web_sessions WHERE token_hash=?',(hashed,)).fetchone()
        if web:
            parent=c.execute('SELECT revoked,expires FROM bridge_tokens WHERE token_hash=?',(web['parent_hash'],)).fetchone()
            if parent is None or parent['revoked'] is not None or (parent['expires'] is not None and parent['expires']<=now):raise PermissionDenied('hierarchy parent identity unavailable')
        return row

    def _member(self,c,project,member_id,role,config):
        row=c.execute('SELECT * FROM hierarchy_members WHERE project=? AND member_id=?',(project,member_id)).fetchone()
        if row is None or row['role']!=role:raise PermissionDenied('registered hierarchy role required')
        live=self._live(c,row['credential_hash'],project)
        if live['subject']!=row['subject'] or row['config_fingerprint']!=config['fingerprint']:raise Conflict('hierarchy member binding changed')
        if live['role']!=('manager' if role=='taesan' else 'worker'):raise PermissionDenied('underlying hierarchy role changed')
        capability='development' if role=='developer' else 'test' if role=='reviewer' else None
        if capability is not None and capability not in json.loads(live['capabilities']):raise PermissionDenied('required hierarchy capability missing')
        return dict(row)

    def _actor(self,c,token,project,role,config):
        actor=self.store._authenticate(c,token)
        member=c.execute('SELECT member_id FROM hierarchy_members WHERE project=? AND credential_hash=? AND role=?',(project,actor['token_hash'],role)).fetchall()
        if len(member)!=1:raise PermissionDenied('registered distinct hierarchy identity required')
        return self._member(c,project,member[0]['member_id'],role,config)

    def _event(self,c,task,kind,state,payload=None):
        c.execute('INSERT INTO hierarchy_events(task_id,kind,state,recorded,payload_digest) VALUES (?,?,?,?,?)',
                  (task,kind,state,self.store._now(),digest(payload) if payload is not None else None))

    def register(self,token,project,member_id,credential_hash,role,agent_id=None):
        name(project);name(member_id);_hash(credential_hash)
        if type(role) is not str or role not in ROLES:raise ValueError('exact hierarchy role required')
        if agent_id is not None:name(agent_id)
        with self.store._transaction() as c:
            owner=self.store._authenticate(c,token);self.store._role(owner,('owner',))
            config=self._config(c,token,project,('owner',));live=self._live(c,credential_hash,project)
            if live['role']!=('manager' if role=='taesan' else 'worker'):raise PermissionDenied('worker or manager role mismatch')
            needed='development' if role=='developer' else 'test' if role=='reviewer' else None
            if needed is not None and needed not in json.loads(live['capabilities']):raise PermissionDenied('required hierarchy capability missing')
            other=c.execute('SELECT role FROM hierarchy_members WHERE project=? AND subject=? AND member_id!=?',(project,live['subject'],member_id)).fetchall()
            if any(row['role']!=role for row in other):raise Conflict('same subject cannot hold independent hierarchy roles')
            existing=c.execute('SELECT * FROM hierarchy_members WHERE project=? AND member_id=?',(project,member_id)).fetchone()
            wanted=(credential_hash,live['subject'],role,agent_id,config['fingerprint'])
            if existing is not None and tuple(existing[k] for k in ('credential_hash','subject','role','agent_id','config_fingerprint'))!=wanted:
                if c.execute("SELECT 1 FROM hierarchy_tasks WHERE project=? AND state!='accepted' AND (developer_id=? OR reviewer_id=?)",(project,member_id,member_id)).fetchone():raise Conflict('active hierarchy membership cannot change')
            c.execute('INSERT OR REPLACE INTO hierarchy_members VALUES (?,?,?,?,?,?,?)',(project,member_id,*wanted))
            return {'project':project,'member_id':member_id,'role':role,'subject':live['subject'],'agent_id':agent_id,'connection_verified':False,'operational_change':False}

    def set_limits(self,token,project=None,*,max_active=2,max_review=2,max_reviewers=1):
        if project is not None:name(project)
        if any(type(x) is not int or not 1<=x<=64 for x in (max_active,max_review,max_reviewers)):raise ValueError('bounded explicit capacity required')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner',))
            if project is None:
                if actor['projects']!=['*']:raise PermissionDenied('global owner capacity scope required')
                scope='*'
            else:self._config(c,token,project,('owner',));scope=project
            c.execute('INSERT OR REPLACE INTO bridge_capacity_limits VALUES (?,?,?)',(scope,max_active,max_review))
            c.execute('INSERT OR REPLACE INTO hierarchy_review_limits VALUES (?,?)',(scope,max_reviewers))
            return {'project':project,'max_active':max_active,'max_review':max_review,'max_reviewers':max_reviewers,'running_jobs_interrupted':False}

    def _capacity(self,c,project):
        policies={row['scope']:dict(row) for row in c.execute('SELECT * FROM bridge_capacity_limits WHERE scope IN (?,?)',('*',project))}
        if '*' not in policies or project not in policies:raise Conflict('global and project capacity limits must be configured')
        for scope in ('*',project):
            if not c.execute('SELECT 1 FROM hierarchy_review_limits WHERE scope=?',(scope,)).fetchone():raise Conflict('independent review concurrency limits must be configured')
        return policies

    def _bound(self,c,token,task_id,role=None,*,allow_cancelled=False,stop_only=False):
        actor=self.store._authenticate(c,token)
        if actor['role']=='worker' and 'development' not in actor['capabilities']:
            _task_id(task_id)
            task=c.execute('SELECT * FROM bridge_tasks WHERE id=?',(task_id,)).fetchone()
            if task is None or not self.store._scope(actor,task['project']):raise NotFound('hierarchy task unavailable')
            config=self._config(c,token,task['project'])
            self._actor(c,token,task['project'],'reviewer',config)
        else:
            task=self.store._get(c,actor,task_id)
            config=self._config(c,token,task['project'])
        row=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone()
        if row is None:raise NotFound('managed hierarchy task unavailable')
        if row['version']!=task['version'] or row['manifest_digest']!=task['manifest_digest'] or (row['config_fingerprint']!=config['fingerprint'] and not stop_only):raise Conflict('managed task binding changed')
        approval=json.loads(task['approval']) if task['approval'] else {}
        policy=c.execute('SELECT * FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone()
        if (approval.get('version')!=task['version'] or approval.get('manifest_digest')!=task['manifest_digest'] or
                policy is None or approval.get('policy_digest')!=policy['policy_digest']):raise Conflict('exact approved task and path policy required')
        if not stop_only:
            if not config['config']['maintenance_enabled']:raise PermissionDenied('managed development disabled')
            self.store._validate_execution(c,task,check_source_age=False)
        if task['state']=='pending_approval' or (task['state']=='needs_verification' and not stop_only) or (task['state']=='cancelled' and not allow_cancelled):raise Conflict('upstream task unavailable')
        if role is not None:
            if stop_only:
                if role!='taesan' or actor['role']!='manager':raise PermissionDenied('current technical manager required for stop reconciliation')
                prior=c.execute("SELECT subject FROM hierarchy_members WHERE project=? AND credential_hash=? AND role='taesan'",(task['project'],actor['token_hash'])).fetchall()
                if len(prior)!=1 or prior[0]['subject']!=actor['subject']:raise PermissionDenied('registered technical manager required for stop reconciliation')
            else:self._actor(c,token,task['project'],role,config)
        if stop_only:
            prior=c.execute('SELECT * FROM hierarchy_members WHERE project=? AND member_id=?',(task['project'],row['developer_id'])).fetchone()
            if prior is None or prior['role']!='developer':raise Conflict('prior developer binding unavailable')
            dev=dict(prior)
        else:dev=self._member(c,task['project'],row['developer_id'],'developer',config)
        return task,config,row,dev

    def start(self,token,task_id,developer_id,request_key):
        _task_id(task_id);name(developer_id);name(request_key)
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);task=self.store._get(c,actor,task_id)
            config=self._config(c,token,task['project']);manager=self._actor(c,token,task['project'],'taesan',config)
            developer=self._member(c,task['project'],developer_id,'developer',config)
            if developer['subject']==manager['subject']:raise PermissionDenied('technical manager must be independent')
            self._capacity(c,task['project'])
            approval=json.loads(task['approval']) if task['approval'] else {}
            if approval.get('version')!=task['version'] or approval.get('manifest_digest')!=task['manifest_digest']:raise Conflict('exact approved queued task required')
            self.store._validate_execution(c,task,check_source_age=True)
            policy=c.execute('SELECT * FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone()
            if policy is None or approval.get('policy_digest')!=policy['policy_digest']:raise Conflict('exact owner-approved path policy required')
            if not config['config']['maintenance_enabled']:raise PermissionDenied('managed development disabled')
            old=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone()
            if old:
                if old['request_key']!=request_key or old['developer_id']!=developer_id or old['manifest_digest']!=task['manifest_digest'] or old['version']!=task['version'] or old['config_fingerprint']!=config['fingerprint']:raise Conflict('hierarchy assignment changed')
                return self._public(c,task,old)
            if task['state']!='queued':raise Conflict('exact approved queued task required')
            now=self.store._now()
            c.execute("INSERT INTO hierarchy_tasks VALUES (?,?,?,?,?,'development_queued',?,NULL,0,NULL,NULL,NULL,NULL,?,NULL,?)",
                      (task_id,task['project'],task['version'],task['manifest_digest'],config['fingerprint'],developer_id,request_key,now))
            self._event(c,task_id,'assigned','development_queued')
            return self._public(c,task,c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone())

    def claim_development(self,token,task_id,lease_seconds=300):
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id,'developer')
            actor=self.store._authenticate(c,token)
            if actor['token_hash']!=developer['credential_hash']:raise PermissionDenied('assigned developer required')
            self._capacity(c,task['project'])
            if row['state']!='development_queued':raise Conflict('development is not queued; reconcile existing work')
        assignment=self.store.claim(token,task['project'],task_id=task_id,lease_seconds=lease_seconds)
        if assignment is None:return None
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id,'developer')
            if row['state'] not in ('development_queued','developing') or task['worker_token_hash']!=developer['credential_hash']:raise Conflict('concurrent hierarchy development claim')
            if row['state']=='development_queued':
                c.execute("UPDATE hierarchy_tasks SET state='developing',updated=? WHERE task_id=?",(self.store._now(),task_id))
                self._event(c,task_id,'development_claimed','developing')
        return assignment

    def _observe(self,task,config):
        callback=self.source_observers.get(task['project'])
        if callback is None:raise Conflict('current candidate source observer not connected')
        observation=callback(task)
        if not isinstance(observation,SourceObservation):raise Conflict('typed current source observation required')
        if (observation.project,observation.task_id,observation.base_sha)!=(task['project'],task['id'],task['manifest']['base_sha']):raise Conflict('source scope mismatch')
        if not 0<=self.store._now()-observation.observed_at<=300:raise Conflict('fresh source observation required')
        if task.get('report') is None or observation.head_sha!=task['report']['head_sha']:raise Conflict('actual source differs from developer report')
        with self.store._transaction() as c:
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,asdict(observation))
            policy=c.execute('SELECT policy FROM bridge_task_policy WHERE task=?',(task['id'],)).fetchone()
            if policy is None or scope_violations(list(observation.changed_paths),json.loads(policy['policy'])['allowed_paths']):raise Conflict('candidate changes outside approved path scope')
        return observation

    def _candidate(self,task,config,developer,observation):
        body={'task':task,'source':asdict(observation),'config_fingerprint':config['fingerprint'],'developer_subject':developer['subject']}
        # Volatile observation time/reference and upstream updated/report metadata
        # are outside identity, while exact source/check/task/config bindings remain.
        identity={'task_id':task['id'],'version':task['version'],'manifest_digest':task['manifest_digest'],
                  'config_fingerprint':config['fingerprint'],'developer_subject':developer['subject'],
                  'base_sha':observation.base_sha,'head_sha':observation.head_sha,
                  'artifact_digest':observation.artifact_digest,'checks_digest':observation.checks_digest,
                  'runtime_digest':observation.runtime_digest,'changed_paths':list(observation.changed_paths),'report_digest':digest(task['report'])}
        body['digest']=digest(identity);body['identity']=identity
        return body

    def submit_candidate(self,token,task_id):
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id,'developer')
            actor=self.store._authenticate(c,token)
            if actor['token_hash']!=developer['credential_hash']:raise PermissionDenied('assigned developer required')
            if row['state'] not in ('developing','rework_developing') or task['state']!='reported' or task['worker_token_hash']!=actor['token_hash']:raise Conflict('assigned developer actual report required')
            public=self.store._public(task);old_state=row['state']
        observed=self._observe(public,config);candidate=self._candidate(public,config,developer,observed)
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id,'developer')
            if row['state']!=old_state or digest(self.store._public(task)['report'])!=candidate['identity']['report_digest']:raise Conflict('candidate changed during observation')
            c.execute("UPDATE hierarchy_tasks SET state='candidate_ready',candidate=?,evidence=NULL,reviewer_id=NULL,lease_hash=NULL,lease_expires=NULL,updated=? WHERE task_id=?",
                      (canonical_json(candidate),self.store._now(),task_id));self._event(c,task_id,'candidate_frozen','candidate_ready',candidate['identity'])
            return self._public(c,task,c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone())

    def _fresh_candidate(self,token,task_id,role=None):
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id,role)
            if row['candidate'] is None:raise Conflict('frozen candidate required')
            saved=json.loads(row['candidate']);public=self.store._public(task)
        current=self._candidate(public,config,developer,self._observe(public,config))
        if current['digest']!=saved['digest']:
            with self.store._transaction() as c:
                c.execute("UPDATE hierarchy_tasks SET state='needs_verification',evidence=NULL,reason='source_changed',updated=? WHERE task_id=?",(self.store._now(),task_id))
                self._event(c,task_id,'source_changed','needs_verification')
            raise Conflict('frozen candidate changed; previous review invalid')
        return saved

    def claim_review(self,token,task_id,lease_seconds=300):
        if type(lease_seconds) is not int or not 1<=lease_seconds<=600:raise ValueError('bounded review lease required')
        candidate=self._fresh_candidate(token,task_id,'reviewer')
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id,'reviewer');reviewer=self._actor(c,token,task['project'],'reviewer',config)
            if developer['subject']==reviewer['subject']:raise PermissionDenied('self review is forbidden')
            self._capacity(c,task['project'])
            if row['state']=='reviewing':
                if row['lease_expires']<=self.store._now():
                    c.execute("UPDATE hierarchy_tasks SET state='needs_verification',reason='review_lease_expired' WHERE task_id=?",(task_id,))
                    self._event(c,task_id,'review_lease_expired','needs_verification')
                    return {'task_id':task_id,'state':'needs_verification','automatic_retry_safe':False}
                raise Conflict('review already active')
            if row['state']!='candidate_ready':raise Conflict('frozen pending review required')
            if json.loads(row['candidate'])['digest']!=candidate['digest']:raise Conflict('candidate changed before review lease')
            # Review sublease does not claim another developer task or release repo reservation.
            self._capacity(c,task['project'])
            for scope in ('*',task['project']):
                cap=c.execute('SELECT max_active FROM hierarchy_review_limits WHERE scope=?',(scope,)).fetchone()[0]
                active=c.execute("SELECT COUNT(*) FROM hierarchy_tasks WHERE (state='reviewing' OR (state='needs_verification' AND reviewer_id IS NOT NULL AND lease_hash IS NOT NULL))"+(" AND project=?" if scope!='*' else ''),(scope,) if scope!='*' else ()).fetchone()[0]
                if active>=cap:raise Conflict('independent review execution capacity full')
            if c.execute("SELECT 1 FROM hierarchy_tasks h JOIN hierarchy_members m ON m.project=h.project AND m.member_id=h.reviewer_id WHERE m.subject=? AND (h.state='reviewing' OR (h.state='needs_verification' AND h.lease_hash IS NOT NULL))",(reviewer['subject'],)).fetchone():raise Conflict('reviewer already busy or outcome uncertain')
            lease=secrets.token_hex(32);expires=self.store._now()+lease_seconds
            c.execute("UPDATE hierarchy_tasks SET state='reviewing',reviewer_id=?,lease_hash=?,lease_expires=?,updated=? WHERE task_id=?",
                      (reviewer['member_id'],hashlib.sha256(lease.encode()).hexdigest(),expires,self.store._now(),task_id))
            self._event(c,task_id,'review_claimed','reviewing')
            return {'task_id':task_id,'state':'reviewing','lease_token':lease,'lease_expires_at':expires,'candidate':candidate,'operational_change':False}

    def _reviewer_lease(self,c,token,task_id,lease_token):
        task,config,row,developer=self._bound(c,token,task_id,'reviewer');reviewer=self._actor(c,token,task['project'],'reviewer',config)
        if row['state']!='reviewing' or row['reviewer_id']!=reviewer['member_id'] or type(lease_token) is not str or row['lease_hash']!=hashlib.sha256(lease_token.encode()).hexdigest():raise PermissionDenied('exact reviewer lease required')
        if row['lease_expires']<=self.store._now():raise Conflict('review lease expired; reconcile before replay')
        return task,config,row,developer,reviewer

    def verify_review(self,token,task_id,lease_token):
        candidate=self._fresh_candidate(token,task_id,'reviewer')
        with self.store._transaction() as c:
            task,config,row,developer,reviewer=self._reviewer_lease(c,token,task_id,lease_token)
            host=self.review_hosts.get(task['project'])
            if host is None:raise Conflict('independent execution host not connected')
            execution_hash=row['lease_hash']
            if c.execute('SELECT 1 FROM hierarchy_review_runs WHERE task_id=? AND lease_hash=?',(task_id,execution_hash)).fetchone():raise Conflict('review execution already recorded; reconcile without replay')
            c.execute("INSERT INTO hierarchy_review_runs VALUES (?,?,'running',?)",(task_id,execution_hash,candidate['digest']))
        def heartbeat():
            with self.store._transaction() as c:
                current_task,current_config,current,_,current_reviewer=self._reviewer_lease(c,token,task_id,lease_token)
                if json.loads(current['candidate'])['digest']!=candidate['digest']:raise Conflict('review candidate changed during execution')
            return True
        heartbeat()
        try:evidence=host(candidate,reviewer,heartbeat=heartbeat)
        except Exception:
            with self.store._transaction() as c:
                c.execute("UPDATE hierarchy_tasks SET state='needs_verification',reason='review_execution_uncertain',updated=? WHERE task_id=? AND state='reviewing'",(self.store._now(),task_id))
                c.execute("UPDATE hierarchy_review_runs SET state='uncertain' WHERE task_id=? AND lease_hash=?",(task_id,execution_hash))
                self._event(c,task_id,'review_execution_uncertain','needs_verification')
            raise Conflict('independent review execution outcome uncertain') from None
        if not isinstance(evidence,ReviewEvidence) or evidence.isolation_verified is not True:raise Conflict('verified isolated execution evidence required')
        if evidence.candidate_digest!=candidate['digest'] or evidence.artifact_digest!=candidate['source']['artifact_digest'] or evidence.reviewer_subject!=reviewer['subject'] or evidence.runtime_digest!=candidate['source']['runtime_digest']:raise Conflict('independent evidence binding mismatch')
        if not 0<=self.store._now()-evidence.observed_at<=300:raise Conflict('fresh independent execution evidence required')
        if {x.name for x in evidence.checks}!=set(candidate['task']['manifest']['checks']):raise Conflict('all exact approved checks required')
        if digest({x.name:x.argv_digest for x in evidence.checks})!=candidate['source']['checks_digest']:raise Conflict('actual check commands differ from frozen specification')
        assessment=evidence.code_assessment
        if candidate['task']['manifest']['kind']=='development':
            if assessment is None or assessment.candidate_digest!=candidate['digest'] or assessment.reviewer_subject!=reviewer['subject'] or not 0<=self.store._now()-assessment.observed_at<=300:raise Conflict('fresh independent code review assessment required')
        self._fresh_candidate(token,task_id,'reviewer')
        with self.store._transaction() as c:
            task,config,row,developer,reviewer=self._reviewer_lease(c,token,task_id,lease_token)
            if json.loads(row['candidate'])['digest']!=evidence.candidate_digest:raise Conflict('candidate changed before evidence persistence')
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,asdict(evidence))
            passed=all(x.exit_code==0 for x in evidence.checks) and (assessment is None or assessment.recommendation=='pass');state='review_passed' if passed else 'review_failed'
            c.execute("UPDATE hierarchy_review_runs SET state='completed' WHERE task_id=? AND lease_hash=?",(task_id,execution_hash))
            c.execute('UPDATE hierarchy_tasks SET state=?,evidence=?,lease_hash=NULL,lease_expires=NULL,reason=?,updated=? WHERE task_id=?',
                      (state,canonical_json(asdict(evidence)),None if passed else 'independent_check_failed',self.store._now(),task_id))
            self._event(c,task_id,'independent_review_completed',state,asdict(evidence))
            return self._public(c,task,c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone())

    def reconcile_review(self,token,task_id):
        """Reconcile one exact prior execution; never starts or retries a review."""
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id,'taesan',allow_cancelled=True,stop_only=True)
            if row['state'] not in ('reviewing','needs_verification') or row['lease_hash'] is None or row['reviewer_id'] is None or row['candidate'] is None:raise Conflict('exact uncertain review execution required')
            reviewer=c.execute('SELECT subject FROM hierarchy_members WHERE project=? AND member_id=?',(task['project'],row['reviewer_id'])).fetchone()
            if reviewer is None:raise Conflict('prior reviewer binding unavailable')
            candidate=json.loads(row['candidate']);context={'project':task['project'],'task_id':task_id,'candidate_digest':candidate['digest'],'lease_hash':row['lease_hash'],'reviewer_subject':reviewer['subject']}
            observer=self.stop_observers.get(task['project'])
            if observer is None:raise Conflict('independent stop observer not connected')
        observation=observer(candidate,context)
        if not isinstance(observation,StopObservation) or observation.stopped is not True:raise Conflict('prior review stop is not verified')
        if any(getattr(observation,key)!=value for key,value in context.items()):raise Conflict('stop evidence execution binding mismatch')
        if not 0<=self.store._now()-observation.observed_at<=300:raise Conflict('fresh exact stop evidence required')
        source_current=True
        try:self._fresh_candidate(token,task_id,'taesan')
        except (Conflict,PermissionDenied):source_current=False
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id,'taesan',allow_cancelled=True,stop_only=True)
            if row['state'] not in ('reviewing','needs_verification') or row['lease_hash']!=observation.lease_hash or json.loads(row['candidate'])['digest']!=observation.candidate_digest:raise Conflict('review execution changed during stop reconciliation')
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,asdict(observation))
            state='candidate_ready' if source_current else 'needs_verification'
            reason='review_stop_confirmed' if source_current else 'source_changed'
            c.execute('UPDATE hierarchy_tasks SET state=?,reason=?,evidence=NULL,reviewer_id=NULL,lease_hash=NULL,lease_expires=NULL,updated=? WHERE task_id=?',(state,reason,self.store._now(),task_id))
            self._event(c,task_id,'review_stop_confirmed',state,asdict(observation))
            result=self._public(c,task,c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone())
            result.update(stop_confirmed=True,execution_started=False,automatic_retry_performed=False)
            return result

    def decide(self,token,task_id,candidate_digest,decision='accept'):
        _hash(candidate_digest)
        if decision not in ('accept','rework'):raise ValueError('technical accept or rework required')
        self._fresh_candidate(token,task_id,'taesan')
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id,'taesan');manager=self._actor(c,token,task['project'],'taesan',config)
            candidate=json.loads(row['candidate']);evidence=json.loads(row['evidence']) if row['evidence'] else None
            if candidate['digest']!=candidate_digest:raise Conflict('exact reviewed candidate required')
            if row['reviewer_id'] is None:raise Conflict('independent reviewer required')
            reviewer=self._member(c,task['project'],row['reviewer_id'],'reviewer',config)
            if len({developer['subject'],reviewer['subject'],manager['subject']})!=3:raise PermissionDenied('three distinct technical identities required')
            if decision=='accept':
                if row['state']!='review_passed' or evidence is None or evidence.get('candidate_digest')!=candidate_digest or evidence.get('reviewer_subject')!=reviewer['subject'] or evidence.get('isolation_verified') is not True or any(x['exit_code']!=0 for x in evidence['checks']):raise Conflict('actual independent passing checks required')
                self._require_code_assessment(candidate,evidence,reviewer['subject'])
                if task['state']!='reported':raise Conflict('upstream developer report changed')
                from .qa_jobs import require_task_qa
                require_task_qa(c, task, candidate['source']['head_sha'])
                record={'report_digest':digest(json.loads(task['report'])),'scope':'three_tier_technical_only','actor':manager['subject'],'reviewer':reviewer['subject'],'candidate_digest':candidate_digest,'evidence_digest':digest(evidence),'deployment_approved':False}
                c.execute("UPDATE bridge_tasks SET state='reviewed',review=?,updated=? WHERE id=?",(canonical_json(record),self.store._now(),task_id))
                state='accepted'
            else:
                if row['state'] not in ('review_passed','review_failed'):raise Conflict('completed independent review required before rework')
                if row['rework_count']>=2:
                    c.execute("UPDATE hierarchy_tasks SET state='needs_verification',reason='rework_limit',updated=? WHERE task_id=?",(self.store._now(),task_id))
                    self._event(c,task_id,'rework_limit','needs_verification')
                    return self._public(c,task,c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone())
                state='rework_queued'
                c.execute('UPDATE hierarchy_tasks SET rework_count=rework_count+1 WHERE task_id=?',(task_id,))
            c.execute('UPDATE hierarchy_tasks SET state=?,updated=? WHERE task_id=?',(state,self.store._now(),task_id));self._event(c,task_id,'technical_'+decision,state,{'candidate_digest':candidate_digest})
            return self._public(c,task,c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone())

    def claim_rework(self,token,task_id,lease_seconds=300):
        if type(lease_seconds) is not int or not 1<=lease_seconds<=600:raise ValueError('bounded development lease required')
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id,'developer');actor=self.store._authenticate(c,token)
            if actor['token_hash']!=developer['credential_hash'] or row['state']!='rework_queued' or task['state']!='reported':raise Conflict('assigned queued rework required')
            policies=self._capacity(c,task['project'])
            self.store._validate_execution(c,task,check_source_age=True)
            for scope,limit in policies.items():
                rows=c.execute('SELECT * FROM bridge_tasks'+(' WHERE project=?' if scope!='*' else ''),(scope,) if scope!='*' else ()).fetchall()
                active=sum(r['state'] in ('running','needs_verification') or (r['state']=='cancelled' and r['worker_subject'] is not None and not c.execute('SELECT 1 FROM bridge_stop_confirmations WHERE task=?',(r['id'],)).fetchone()) for r in rows if r['id']!=task_id)
                pending=sum(r['state']=='reported' for r in rows if r['id']!=task_id)
                if active>=limit['max_active'] or pending>=limit['max_review']:raise Conflict('development capacity full')
            if c.execute("SELECT 1 FROM bridge_tasks t WHERE id!=? AND worker_token_hash=? AND (state IN ('running','needs_verification') OR (state='cancelled' AND NOT EXISTS (SELECT 1 FROM bridge_stop_confirmations s WHERE s.task=t.id)))",(task_id,actor['token_hash'])).fetchone():raise Conflict('developer already busy')
            policy=json.loads(c.execute('SELECT policy FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone()['policy'])
            from .coordination import Coordination
            if not Coordination.dependencies_ready(c,policy):raise Conflict('approved dependencies changed')
            if not 1<=row['rework_count']<=2 or row['candidate'] is None:raise Conflict('approved frozen rework checkpoint required')
            candidate=json.loads(row['candidate']);source=dict(candidate['source']);source['changed_paths']=tuple(source['changed_paths'])
            observation=SourceObservation(**source)
            expected=self._candidate(self.store._public(task),config,developer,observation)
            if candidate['digest']!=expected['digest'] or candidate['identity']!=expected['identity']:raise Conflict('exact approved rework candidate binding required')
            lease=secrets.token_hex(32);expires=self.store._now()+lease_seconds
            c.execute('INSERT INTO hierarchy_rework_checkpoints VALUES (?,?,?,?,?,?,?,?,?)',(task_id,hashlib.sha256(lease.encode()).hexdigest(),task['version'],task['manifest_digest'],config['fingerprint'],row['developer_id'],actor['token_hash'],row['rework_count'],canonical_json(candidate)))
            c.execute("UPDATE bridge_tasks SET state='running',lease_hash=?,lease_expires=?,report=NULL,review=NULL,updated=? WHERE id=?",(hashlib.sha256(lease.encode()).hexdigest(),expires,self.store._now(),task_id))
            c.execute("UPDATE hierarchy_tasks SET state='rework_developing',candidate=NULL,evidence=NULL,reviewer_id=NULL,reason=NULL,updated=? WHERE task_id=?",(self.store._now(),task_id))
            self._event(c,task_id,'rework_claimed','rework_developing')
            return {**self.store._public(self.store._get(c,actor,task_id)),'lease_token':lease}

    def approve_next(self,token,current_task_id,next_task_id,expected_parent_digest,request_key,developer_id=None):
        """Authorize assignment only inside an already owner-approved next task.

        This never creates an owner approval, changes its scope, or starts a
        worker. The explicit dependency policy must bind the accepted parent.
        """
        _task_id(current_task_id);_task_id(next_task_id);_hash(expected_parent_digest);name(request_key)
        if current_task_id==next_task_id:raise Conflict('next task must differ from accepted parent')
        candidate=self._fresh_candidate(token,current_task_id,'taesan')
        with self.store._transaction() as c:
            parent,config,current,developer=self._bound(c,token,current_task_id,'taesan')
            manager=self._actor(c,token,parent['project'],'taesan',config)
            if current['state']!='accepted' or candidate['digest']!=expected_parent_digest:raise Conflict('current accepted parent proof required')
            evidence=json.loads(current['evidence']) if current['evidence'] else None
            if evidence is None or evidence['candidate_digest']!=expected_parent_digest or evidence['isolation_verified'] is not True or any(x['exit_code']!=0 for x in evidence['checks']):raise Conflict('independent parent proof required')
            reviewer=self._member(c,parent['project'],current['reviewer_id'],'reviewer',config)
            self._require_code_assessment(candidate,evidence,reviewer['subject'])
            if len({developer['subject'],reviewer['subject'],manager['subject']})!=3:raise PermissionDenied('parent independence required')
            actor=self.store._authenticate(c,token);following=self.store._get(c,actor,next_task_id)
            manifest=json.loads(following['manifest']);parent_manifest=json.loads(parent['manifest'])
            if following['project']!=parent['project'] or manifest['repository']!=parent_manifest['repository']:raise Conflict('next task must be same project and repository')
            if manifest['base_sha']!=candidate['source']['head_sha']:raise Conflict('next approved base must equal accepted parent HEAD')
            self._capacity(c,parent['project'])
            from .coordination import Coordination
            policy=c.execute('SELECT * FROM bridge_task_policy WHERE task=?',(next_task_id,)).fetchone()
            approval=json.loads(following['approval']) if following['approval'] else {}
            if policy is None or approval.get('version')!=following['version'] or approval.get('manifest_digest')!=following['manifest_digest'] or approval.get('policy_digest')!=policy['policy_digest']:raise Conflict('next task needs exact prior owner or delegated approval')
            policy_value=json.loads(policy['policy'])
            dependency={'id':current_task_id,'version':parent['version'],'manifest_digest':parent['manifest_digest']}
            if dependency not in policy_value['dependencies'] or not Coordination.dependencies_ready(c,policy_value):raise Conflict('next task must explicitly depend on this exact accepted parent')
            self.store._validate_execution(c,following,check_source_age=True)
            developer_id=developer_id or current['developer_id'];name(developer_id)
            chosen=self._member(c,parent['project'],developer_id,'developer',config)
            if chosen['subject']==manager['subject']:raise PermissionDenied('independent next developer required')
            key=digest({'project':parent['project'],'request':request_key})
            previous=c.execute('SELECT * FROM hierarchy_next_assignments WHERE request_key=?',(key,)).fetchone()
            binding=(current_task_id,next_task_id,expected_parent_digest,developer_id)
            if previous is not None:
                if tuple(previous[k] for k in ('parent_id','next_id','parent_digest','developer_id'))!=binding:raise Conflict('next development instruction key changed')
                managed=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(next_task_id,)).fetchone()
                if managed is None:raise Conflict('next instruction record missing')
                return self._public(c,following,managed)
            if following['state']!='queued':raise Conflict('next task must remain approved and queued')
            if c.execute('SELECT 1 FROM hierarchy_tasks WHERE task_id=?',(next_task_id,)).fetchone():raise Conflict('next task already assigned through a different instruction')
            now=self.store._now()
            c.execute("INSERT INTO hierarchy_tasks VALUES (?,?,?,?,?,'development_queued',?,NULL,0,NULL,NULL,NULL,NULL,?,NULL,?)",
                      (next_task_id,parent['project'],following['version'],following['manifest_digest'],config['fingerprint'],developer_id,request_key,now))
            c.execute('INSERT INTO hierarchy_next_assignments VALUES (?,?,?,?,?,?)',(key,*binding,now))
            self._event(c,next_task_id,'next_development_authorized','development_queued',{'parent_id':current_task_id,'parent_digest':expected_parent_digest})
            result=self._public(c,following,c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(next_task_id,)).fetchone())
            result.update(parent_task_id=current_task_id,parent_candidate_digest=expected_parent_digest,
                          worker_id=developer_id,agent_id=chosen['agent_id'],instruction_state='ready_for_registered_receiver',execution_started=False)
            return result

    @staticmethod
    def _require_code_assessment(candidate,evidence,reviewer_subject):
        if candidate['task']['manifest']['kind']!='development':return
        assessment=evidence.get('code_assessment')
        if type(assessment) is not dict or assessment.get('candidate_digest')!=candidate['digest'] or assessment.get('reviewer_subject')!=reviewer_subject or assessment.get('recommendation')!='pass':raise Conflict('independent code review pass required')

    def _public(self,c,task,row):
        candidate=json.loads(row['candidate']) if row['candidate'] else None;evidence=json.loads(row['evidence']) if row['evidence'] else None
        return {'task_id':task['id'],'project':task['project'],'state':row['state'],'version':row['version'],'manifest_digest':row['manifest_digest'],
                'developer_id':row['developer_id'],'reviewer_id':row['reviewer_id'],'rework_count':row['rework_count'],
                'candidate_digest':candidate['digest'] if candidate else None,'source':candidate['source'] if candidate else None,
                'checks':evidence['checks'] if evidence else [],'code_assessment':evidence.get('code_assessment') if evidence else None,'review_state':'passed' if row['state'] in ('review_passed','accepted') else 'failed' if row['state']=='review_failed' else 'not_measured',
                'technical_accepted':row['state']=='accepted','operational_approved':False,'connection_verified':False,
                'requires_owner_decision':False,'blocked_reason':row['reason'],'operational_change':False,'existing_chat_control_verified':False}

    def _read_freshness(self,token,task_id):
        try:
            self._fresh_candidate(token,task_id)
            return None
        except PermissionDenied:return 'authority_changed'
        except (Conflict,NotFound):return 'source_unavailable'
        except Exception:return 'source_unavailable'

    def get(self,token,task_id):
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id)
            actor=self.store._authenticate(c,token)
            if actor['role']=='worker' and actor['token_hash']!=developer['credential_hash']:
                if row['reviewer_id'] is None or self._member(c,task['project'],row['reviewer_id'],'reviewer',config)['credential_hash']!=actor['token_hash']:raise PermissionDenied('assigned hierarchy worker required')
            refresh=row['state'] in ('accepted','review_passed')
        reason=self._read_freshness(token,task_id) if refresh else None
        with self.store._transaction() as c:
            task,config,row,developer=self._bound(c,token,task_id)
            result=self._public(c,task,row)
            if reason:result.update(technical_accepted=False,review_state='not_measured',blocked_reason=reason)
            return result

    def overview(self,token,projects,*,limit=50,cursor=None,freshness_limit=3):
        """Read a bounded global page, with expensive freshness checks capped.

        Cursor pins the task row set's high-water mark, project order, current
        configuration and authenticated credential. Counts describe that row set
        in its current stored states, not an immutable historical-state snapshot.
        Unchecked acceptance is history, never current executable authorization.
        """
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):raise ValueError('explicit bounded projects required')
        for project in projects:name(project)
        if type(limit) is not int or not 1<=limit<=200:raise ValueError('page limit must be 1 to 200')
        if type(freshness_limit) is not int or not 0<=freshness_limit<=20:raise ValueError('bounded freshness budget required')
        if cursor is not None and (type(cursor) is not str or len(cursor)!=32 or any(x not in '0123456789abcdef' for x in cursor)):raise ValueError('opaque page cursor required')
        marks=','.join('?' for _ in projects)
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            configs={project:self._config(c,token,project,('owner','manager')) for project in projects}
            binding=digest({'projects':list(projects),'configs':{p:v['fingerprint'] for p,v in configs.items()}})
            c.execute('CREATE TABLE IF NOT EXISTS hierarchy_overview_cursors (id TEXT PRIMARY KEY,actor TEXT NOT NULL,binding TEXT NOT NULL,anchor INTEGER NOT NULL,last_row INTEGER NOT NULL,expires REAL NOT NULL)')
            c.execute('DELETE FROM hierarchy_overview_cursors WHERE expires<=?',(self.store._now(),))
            if cursor is None:
                anchor=c.execute('SELECT COALESCE(MAX(rowid),0) FROM hierarchy_tasks WHERE project IN ('+marks+')',tuple(projects)).fetchone()[0]
                last=anchor+1
            else:
                saved=c.execute('SELECT * FROM hierarchy_overview_cursors WHERE id=?',(cursor,)).fetchone()
                if saved is None or saved['actor']!=actor['token_hash'] or saved['binding']!=binding:raise Conflict('page cursor expired or scope changed')
                anchor,last=saved['anchor'],saved['last_row']
            page=[dict(row) for row in c.execute('SELECT rowid AS page_row,* FROM hierarchy_tasks WHERE project IN ('+marks+') AND rowid<=? AND rowid<? ORDER BY rowid DESC LIMIT ?',(*projects,anchor,last,limit+1))]
            more=len(page)>limit;page=page[:limit]
            # No full-table source traversal: only the displayed, bounded window.
            refresh=[(row['task_id'],json.loads(row['candidate'])['digest'] if row['candidate'] else None) for row in page if row['state'] in ('accepted','review_passed')][:freshness_limit]
        freshness={task_id:(self._read_freshness(token,task_id),candidate_digest) for task_id,candidate_digest in refresh}
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            current={project:self._config(c,token,project,('owner','manager')) for project in projects}
            if any(current[p]['fingerprint']!=configs[p]['fingerprint'] for p in projects):raise Conflict('project changed during overview collection')
            counts={project:{} for project in projects}
            for row in c.execute('SELECT project,state,COUNT(*) AS amount FROM hierarchy_tasks WHERE project IN ('+marks+') AND rowid<=? GROUP BY project,state',(*projects,anchor)):
                label=row['state'] if row['state'] in STATES else 'unknown'
                counts[row['project']][label]=counts[row['project']].get(label,0)+row['amount']
            selected={project:[] for project in projects}
            for original in page:
                row=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(original['task_id'],)).fetchone()
                if row is None:continue
                task=self.store._get(c,actor,row['task_id']);item=self._public(c,task,row)
                recorded=row['state']=='accepted';reason=None;fresh='not_applicable'
                if row['state'] in ('accepted','review_passed'):
                    fresh='not_checked'
                    if row['task_id'] in freshness:
                        reason,checked_digest=freshness[row['task_id']]
                        actual=json.loads(row['candidate'])['digest'] if row['candidate'] else None
                        if actual!=checked_digest:reason='source_changed'
                        fresh='checked' if reason is None else 'unavailable'
                    else:reason='source_freshness_not_checked'
                try:self._bound(c,token,row['task_id'])
                except (Conflict,PermissionDenied,NotFound):reason='authority_changed'
                if row['config_fingerprint']!=current[row['project']]['fingerprint']:reason='configuration_changed'
                if reason:item.update(technical_accepted=False,review_state='not_measured',blocked_reason=reason)
                item.update(source_freshness=fresh,acceptance_recorded=recorded)
                selected[row['project']].append(item)
            results=[];all_counts={}
            for project in projects:
                config=current[project]
                members=[{'member_id':row['member_id'],'role':row['role'],'subject':row['subject'],'agent_id':row['agent_id'],'connection_verified':False} for row in c.execute('SELECT * FROM hierarchy_members WHERE project=? ORDER BY member_id LIMIT 200',(project,))]
                role_counts={row['role']:row['amount'] for row in c.execute('SELECT role,COUNT(*) AS amount FROM hierarchy_members WHERE project=? GROUP BY role',(project,))}
                member_total=sum(role_counts.values())
                policies={row['scope']:dict(row) for row in c.execute('SELECT * FROM bridge_capacity_limits WHERE scope IN (?,?)',('*',project))}
                policy=policies.get(project,{})
                total=sum(counts[project].values());tasks=selected[project]
                for state,amount in counts[project].items():all_counts[state]=all_counts.get(state,0)+amount
                results.append({'project':project,'config_fingerprint':config['fingerprint'],
                    'capacity':{'configured':'*' in policies and project in policies,'max_active':policy.get('max_active'),'max_review':policy.get('max_review')},
                    'members':members,'members_total':member_total,'members_truncated':member_total>len(members),'role_counts':role_counts,
                    'tasks':tasks,'total_tasks':total,'displayed_tasks':len(tasks),'tasks_truncated':total>len(tasks),'state_counts':counts[project]})
            next_cursor=None
            if more and page:
                # Short lived metadata only; bound server-side storage even if a
                # caller continually asks for another copy of a read cursor.
                c.execute('DELETE FROM hierarchy_overview_cursors WHERE id IN (SELECT id FROM hierarchy_overview_cursors ORDER BY expires,id LIMIT MAX(0,(SELECT COUNT(*) FROM hierarchy_overview_cursors)-1023))')
                next_cursor=secrets.token_hex(16)
                c.execute('INSERT INTO hierarchy_overview_cursors VALUES (?,?,?,?,?,?)',(next_cursor,actor['token_hash'],binding,anchor,page[-1]['page_row'],self.store._now()+300))
            return {'projects':results,'total_tasks':sum(all_counts.values()),'displayed_tasks':sum(len(v) for v in selected.values()),
                    'state_counts':all_counts,'next_cursor':next_cursor,'truncated':more,'freshness_checks':len(refresh),
                    'counts_scope':'page_start_task_set_current_recorded_states','pagination_order':'task_creation_desc',
                    'operational_change':False,'existing_chat_control_verified':False}
