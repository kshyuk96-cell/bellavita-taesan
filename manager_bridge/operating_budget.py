"""Project-local admission budgets; not subscription metering or monetary spend.

Only new-work admission is limited. Unknown/reserved work never expires itself
out of concurrency. Auth observations come from registered host callbacks, never
HTTP success flags. Existing collection, review and reconciliation remain usable.
"""
from dataclasses import dataclass,asdict
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import re
from .maintenance_registry import name,receipt
from .store import Conflict,PermissionDenied,NotFound,canonical_json,digest,_task_id,_hierarchy_acceptance,token_hash
from .three_tier import ThreeTier


def _hex(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('exact admission digest required')
    return value


def _clock(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise ValueError('finite observation clock required')
    return value


@dataclass(frozen=True)
class BudgetPolicy:
    max_inflight:int
    max_starts:int
    window_seconds:int
    auth_max_age_seconds:int=300
    def __post_init__(self):
        if (type(self.max_inflight) is not int or not 1<=self.max_inflight<=100
                or type(self.max_starts) is not int or not 1<=self.max_starts<=10000
                or type(self.window_seconds) is not int or not 1<=self.window_seconds<=86400
                or type(self.auth_max_age_seconds) is not int or not 1<=self.auth_max_age_seconds<=300):
            raise ValueError('bounded owner-installed admission policy required')


@dataclass(frozen=True)
class BudgetAuthObservation:
    project:str
    config_fingerprint:str
    state:str
    observed_at:float
    valid_until:float|None
    reference:str
    def __post_init__(self):
        name(self.project);_hex(self.config_fingerprint);receipt(self.reference);_clock(self.observed_at)
        if self.state not in ('ready','expired','unknown','unavailable'):raise ValueError('typed authentication observation required')
        if self.valid_until is not None:_clock(self.valid_until)
        if self.state=='ready' and (self.valid_until is None or not self.observed_at<self.valid_until<=self.observed_at+300):
            raise ValueError('bounded authentication observation validity required')


class OperatingBudget:
    def __init__(self,core,*,auth_observer=None,require_policy=False):
        if not isinstance(core,ThreeTier):raise ValueError('registered hierarchy core required')
        if auth_observer is not None and not callable(auth_observer):raise ValueError('trusted auth observer required')
        if type(require_policy) is not bool:raise ValueError('explicit admission default required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.auth_observer,self.require_policy=auth_observer,require_policy
        self._execution_scope=ContextVar('operating-budget-'+str(id(self)),default=None)
        with self.store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS operating_budget_policies (project TEXT PRIMARY KEY,config_fingerprint TEXT,revision INTEGER,policy TEXT,policy_digest TEXT,updated REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS operating_budget_auth (project TEXT PRIMARY KEY,config_fingerprint TEXT,state TEXT,observed_at REAL,valid_until REAL,reference TEXT,checked_at REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS operating_budget_auth_requests (project TEXT PRIMARY KEY,sequence INTEGER NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS operating_budget_reservations (id TEXT PRIMARY KEY,project TEXT,action_key TEXT UNIQUE,task_id TEXT,version INTEGER,manifest_digest TEXT,config_fingerprint TEXT,policy_digest TEXT,actor_hash TEXT,state TEXT,created REAL,started_at REAL,updated REAL)')
            c.execute('CREATE INDEX IF NOT EXISTS operating_budget_project_state ON operating_budget_reservations(project,state,created)')
            c.execute('CREATE TABLE IF NOT EXISTS operating_budget_events (id INTEGER PRIMARY KEY AUTOINCREMENT,reservation_id TEXT,state TEXT,recorded REAL)')

    @staticmethod
    def _quarantined(c):
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='governance_restore_marker'").fetchone() and c.execute('SELECT 1 FROM governance_restore_marker WHERE reconciliation_required=1 LIMIT 1').fetchone())

    @contextmanager
    def execution_scope(self,token,action_key):
        """Trusted coordinator context; never accepted from HTTP fields."""
        _hex(action_key)
        mark=self._execution_scope.set((token_hash(token),action_key))
        try:yield
        finally:self._execution_scope.reset(mark)

    def authorize_core_admission(self,c,token,task_id):
        actor=self.store._authenticate(c,token);task=self.store._get(c,actor,task_id)
        decision=self._check(c,token,task['project'])
        if not decision['configured']:
            if not decision['allowed']:raise PermissionDenied('admission policy required')
            return
        # Its reservation already counts toward quota. Only those count limits
        # may be ignored here; auth/config/quarantine remain mandatory.
        if decision.get('reason') not in (None,'max_inflight','max_starts_in_window'):
            raise PermissionDenied('admission authentication or policy unavailable')
        active=self._execution_scope.get()
        if active is None or active[0]!=actor['token_hash']:raise PermissionDenied('registered budgeted coordinator required')
        row=c.execute('SELECT * FROM operating_budget_reservations WHERE action_key=?',(active[1],)).fetchone()
        if (row is None or row['task_id']!=task_id or row['state']!='reserved' or row['actor_hash']!=actor['token_hash'] or
                row['version']!=task['version'] or row['manifest_digest']!=task['manifest_digest'] or
                row['config_fingerprint']!=self.registry._get(c,token,task['project'])['fingerprint'] or
                row['policy_digest']!=decision['policy_digest']):
            raise PermissionDenied('exact reserved admission required')
        action=self._action(c,row)
        leader=c.execute('SELECT * FROM continuous_leader WHERE singleton=1').fetchone()
        guard=c.execute('SELECT * FROM continuous_tick_guard WHERE singleton=1').fetchone()
        now=self.store._now()
        if (action is None or action['state']!='running' or leader is None or guard is None or
                leader['epoch']!=action['epoch'] or guard['epoch']!=action['epoch'] or
                leader['actor_hash']!=actor['token_hash'] or leader['expires']<=now or guard['expires']<=now):
            raise PermissionDenied('live budgeted execution intent required')

    def _auth(self,c,token,project,*,owner=False,manager=False):
        roles=('owner',) if owner else ('manager',) if manager else ('owner','manager')
        config=self.core._config(c,token,project,roles)
        actor=self.store._authenticate(c,token)
        if manager:self.core._actor(c,token,project,'taesan',config)
        return actor,config

    def _public(self,c,value):
        from .agent_directory import _reject_known_tokens
        _reject_known_tokens(c,value)
        return value

    def install(self,owner,project,policy,*,expected_revision=0):
        name(project)
        if type(policy) is not BudgetPolicy or type(expected_revision) is not int or expected_revision<0:raise ValueError('typed policy and exact prior revision required')
        with self.store._transaction() as c:
            _,config=self._auth(c,owner,project,owner=True)
            if self._quarantined(c):raise Conflict('restored admission policy requires verification')
            old=c.execute('SELECT revision FROM operating_budget_policies WHERE project=?',(project,)).fetchone()
            if expected_revision!=(old['revision'] if old else 0):raise Conflict('admission policy revision changed')
            revision=expected_revision+1;body=asdict(policy);fingerprint=digest({'project':project,'config_fingerprint':config['fingerprint'],'revision':revision,'policy':body})
            c.execute('INSERT OR REPLACE INTO operating_budget_policies VALUES (?,?,?,?,?,?)',(project,config['fingerprint'],revision,canonical_json(body),fingerprint,self.store._now()))
            return self._public(c,{'project':project,'revision':revision,'policy_digest':fingerprint,'policy':body,'execution_authorized':False})

    def refresh_auth(self,token,project):
        """Observe current metadata outside transactions; valid_until is freshness, not OAuth expiry."""
        name(project)
        with self.store._transaction() as c:
            actor,config=self._auth(c,token,project)
            if self._quarantined(c):raise Conflict('restored authentication observation requires verification')
            identity=actor['token_hash']
            c.execute('INSERT OR IGNORE INTO operating_budget_auth_requests VALUES (?,0)',(project,))
            c.execute('UPDATE operating_budget_auth_requests SET sequence=sequence+1 WHERE project=?',(project,))
            sequence=c.execute('SELECT sequence FROM operating_budget_auth_requests WHERE project=?',(project,)).fetchone()[0]
        value=None
        if self.auth_observer is not None:
            try:value=self.auth_observer(project)
            except Exception:value=None
        now=_clock(self.store._now())
        if (type(value) is not BudgetAuthObservation or value.project!=project or value.config_fingerprint!=config['fingerprint']
                or not 0<=now-value.observed_at<=300):
            value=BudgetAuthObservation(project,config['fingerprint'],'unknown',now,None,'budget-auth/unverified')
        if value.state=='ready' and value.valid_until<=now:
            value=BudgetAuthObservation(value.project,value.config_fingerprint,'expired',value.observed_at,value.valid_until,value.reference)
        with self.store._transaction() as c:
            current,latest=self._auth(c,token,project)
            if current['token_hash']!=identity or latest['fingerprint']!=config['fingerprint']:raise Conflict('authentication observation authority changed')
            active=c.execute('SELECT sequence FROM operating_budget_auth_requests WHERE project=?',(project,)).fetchone()[0]
            prior=c.execute('SELECT observed_at FROM operating_budget_auth WHERE project=?',(project,)).fetchone()
            if active!=sequence or (prior and prior['observed_at']>value.observed_at):
                return {'project':project,'state':'superseded','valid_until':None,'credential_expiry_inferred':False,'model_calls':0}
            self._public(c,asdict(value))
            c.execute('INSERT OR REPLACE INTO operating_budget_auth VALUES (?,?,?,?,?,?,?)',(project,value.config_fingerprint,value.state,value.observed_at,value.valid_until,value.reference,self.store._now()))
            return {'project':project,'state':value.state,'valid_until':value.valid_until,'credential_expiry_inferred':False,'model_calls':0}

    def poll_auth(self,token,projects,*,interval_seconds=60,limit=10):
        """Bounded due-only host metadata polling, outside manager transactions.

        The cache's checked_at persists restart throttling. Shorter policy/auth
        validity wins over the requested interval; unknown observations retry at
        the interval rather than storming every manager tick. No policy, no poll.
        """
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):raise ValueError('explicit scoped projects required')
        if type(interval_seconds) is not int or not 1<=interval_seconds<=300 or type(limit) is not int or not 1<=limit<=20:raise ValueError('bounded authentication polling required')
        with self.store._transaction() as c:
            configs={p:self._auth(c,token,p)[1] for p in projects}
            if self._quarantined(c):raise Conflict('restored authentication polling requires verification')
            due=[];considered=0;now=_clock(self.store._now())
            for project in projects:
                row=c.execute('SELECT * FROM operating_budget_policies WHERE project=?',(project,)).fetchone()
                if row is None:continue
                considered+=1
                if row['config_fingerprint']!=configs[project]['fingerprint']:continue
                policy=BudgetPolicy(**json.loads(row['policy']))
                auth=c.execute('SELECT * FROM operating_budget_auth WHERE project=?',(project,)).fetchone()
                checked=auth['checked_at'] if auth is not None else None
                valid=type(checked) in (int,float) and math.isfinite(checked) and checked<=now
                until=checked+min(interval_seconds,policy.auth_max_age_seconds) if valid else None
                if valid and auth['state']=='ready' and auth['valid_until'] is not None:until=min(until,auth['valid_until'])
                if not valid or now>=until:due.append((checked if valid else -1,project))
        observed=[self.refresh_auth(token,project) for _,project in sorted(due)[:limit]]
        with self.store._transaction() as c:
            for project in projects:
                if self._auth(c,token,project)[1]['fingerprint']!=configs[project]['fingerprint']:raise Conflict('authentication polling configuration changed')
            return self._public(c,{'state':'not_configured' if considered==0 else 'observed' if observed else 'no_due_observations',
                'observed':observed,'policies_considered':considered,'due_count':len(due),'truncated':len(due)>limit,
                'model_calls':0,'credential_expiry_inferred':False})

    def _counts(self,c,project,window,now):
        row=c.execute("""SELECT COUNT(*) total,
            COALESCE(SUM(state IN ('reserved','started','uncertain')),0) inflight,
            COALESCE(SUM(state IN ('reserved','uncertain')),0) held_unknown,
            COALESCE(SUM(state IN ('reserved','uncertain') OR (state IN ('started','completed') AND COALESCE(started_at,created)>=?)),0) starts_in_window
            FROM operating_budget_reservations WHERE project=?""",(now-window,project)).fetchone()
        return {k:row[k] for k in ('total','inflight','held_unknown','starts_in_window')}

    def _check(self,c,token,project):
        _,config=self._auth(c,token,project);now=_clock(self.store._now())
        policy=c.execute('SELECT * FROM operating_budget_policies WHERE project=?',(project,)).fetchone()
        if self._quarantined(c):return {'project':project,'allowed':False,'state':'paused','reason':'restore_quarantine','configured':policy is not None,'admission_only':True,'execution_authorized':False}
        if policy is None:
            return {'project':project,'allowed':not self.require_policy,'state':'not_configured','reason':'policy_missing','configured':False,'admission_only':True,'execution_authorized':False}
        body=json.loads(policy['policy']);validated=BudgetPolicy(**body)
        counts=self._counts(c,project,validated.window_seconds,now);reason=None
        if policy['config_fingerprint']!=config['fingerprint'] or policy['policy_digest']!=digest({'project':project,'config_fingerprint':config['fingerprint'],'revision':policy['revision'],'policy':body}):reason='policy_binding_changed'
        auth=c.execute('SELECT * FROM operating_budget_auth WHERE project=?',(project,)).fetchone()
        auth_state='unknown'
        if auth:
            auth_state=auth['state']
            if (auth['config_fingerprint']!=config['fingerprint'] or auth['state']!='ready' or
                    not 0<=now-auth['observed_at']<=validated.auth_max_age_seconds or
                    auth['valid_until'] is None or not now<auth['valid_until']):
                auth_state='expired' if auth['state']=='ready' else auth['state']
        if reason is None and auth_state!='ready':reason='authentication_'+auth_state
        if reason is None and counts['inflight']>=validated.max_inflight:reason='max_inflight'
        if reason is None and counts['starts_in_window']>=validated.max_starts:reason='max_starts_in_window'
        return {'project':project,'allowed':reason is None,'state':'ready' if reason is None else 'paused','reason':reason,
                'configured':True,'policy_revision':policy['revision'],'policy_digest':policy['policy_digest'],
                'counts':counts,'limits':body,'auth_state':auth_state,'subscription_usage_measured':False,'monetary_spend':None,'admission_only':True,'execution_authorized':False}

    def check(self,token,project):
        name(project)
        with self.store._transaction() as c:return self._public(c,self._check(c,token,project))

    def reserve_in_transaction(self,c,token,project,action_key,task_id):
        """Called in the manager's IMMEDIATE fence, before its execution intent."""
        name(project);_hex(action_key);_task_id(task_id)
        actor,config=self._auth(c,token,project,manager=True)
        self._public(c,{'action_key':action_key,'task_id':task_id})
        task=self.store._get(c,actor,task_id)
        if task['project']!=project:raise PermissionDenied('admission task outside project')
        old=c.execute('SELECT * FROM operating_budget_reservations WHERE action_key=?',(action_key,)).fetchone()
        if old and (old['project'],old['task_id'],old['version'],old['manifest_digest'],old['config_fingerprint'])!=(project,task_id,task['version'],task['manifest_digest'],config['fingerprint']):raise Conflict('admission reservation binding changed')
        decision=self._public(c,self._check(c,token,project))
        if not decision['allowed'] or not decision['configured']:return {**decision,'reservation_id':old['id'] if old else None}
        if not config['config']['maintenance_enabled']:raise PermissionDenied('new development disabled')
        approval=json.loads(task['approval']) if task['approval'] else {}
        if task['state']!='queued' or approval.get('version')!=task['version'] or approval.get('manifest_digest')!=task['manifest_digest']:raise Conflict('exact owner-approved queued work required for admission')
        self.store._validate_execution(c,task,check_source_age=True)
        if old and old['state']!='confirmed_not_started':
            return {**decision,'allowed':False,'state':'paused','reason':'reservation_requires_reconciliation','reservation_id':old['id']}
        if old:
            prior=self._action(c,old)
            if prior is None or prior['state']!='waiting' or prior['reason']!='readiness_pending':raise Conflict('no-start reservation cannot be retried without durable proof')
        now=self.store._now();key=old['id'] if old else digest({'project':project,'action_key':action_key,'task_id':task_id,'version':task['version'],'manifest_digest':task['manifest_digest']})
        if old:
            c.execute("UPDATE operating_budget_reservations SET policy_digest=?,actor_hash=?,state='reserved',created=?,started_at=NULL,updated=? WHERE id=?",(decision['policy_digest'],actor['token_hash'],now,now,key))
        else:c.execute("INSERT INTO operating_budget_reservations VALUES (?,?,?,?,?,?,?,?,?,'reserved',?,NULL,?)",(key,project,action_key,task_id,task['version'],task['manifest_digest'],config['fingerprint'],decision['policy_digest'],actor['token_hash'],now,now))
        self._event(c,key,'reserved')
        return {**decision,'state':'reserved','reservation_id':key}

    def _get(self,c,token,key):
        _hex(key);row=c.execute('SELECT * FROM operating_budget_reservations WHERE id=?',(key,)).fetchone()
        if row is None:raise Conflict('admission reservation unavailable')
        self._auth(c,token,row['project'])
        return row

    def _action(self,c,row):
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='continuous_actions'").fetchone():return None
        action=c.execute('SELECT * FROM continuous_actions WHERE action_key=?',(row['action_key'],)).fetchone()
        if action is None:return None
        request=json.loads(action['request']);following=request.get('phase')=='approve_next'
        target=request.get('next_task_id') if following else request.get('task_id')
        version=request.get('next_version') if following else request.get('version')
        manifest=request.get('next_manifest_digest') if following else request.get('manifest_digest')
        if (request.get('phase') not in ('start','approve_next') or request.get('action_key')!=row['action_key'] or action['project']!=row['project'] or
                action['phase']!=request.get('phase') or action['task_id']!=request.get('task_id') or request.get('project')!=row['project'] or
                target!=row['task_id'] or version!=row['version'] or manifest!=row['manifest_digest'] or
                request.get('config_fingerprint')!=row['config_fingerprint'] or action['actor_hash']!=row['actor_hash']):
            raise Conflict('durable admission action binding changed')
        return action

    def _event(self,c,key,state):
        c.execute('INSERT INTO operating_budget_events(reservation_id,state,recorded) VALUES (?,?,?)',(key,state,self.store._now()))

    def finish_in_transaction(self,c,token,reservation_id):
        """Read a recorded manager outcome, never a client supplied success/count."""
        row=self._get(c,token,reservation_id);self._auth(c,token,row['project'],manager=True)
        if row['state'] in ('completed','confirmed_not_started'):return {'reservation_id':row['id'],'state':row['state'],'automatic_replay':False}
        action=self._action(c,row)
        state=('started' if action and action['state']=='confirmed' else
               'confirmed_not_started' if action and action['state']=='waiting' and action['reason']=='readiness_pending' else 'uncertain')
        if row['state']=='uncertain':state='uncertain'  # Explicit reconcile only.
        c.execute('UPDATE operating_budget_reservations SET state=?,started_at=CASE WHEN ?=\'started\' THEN COALESCE(started_at,?) ELSE started_at END,updated=? WHERE id=?',(state,state,self.store._now(),self.store._now(),row['id']))
        self._event(c,row['id'],state)
        return {'reservation_id':row['id'],'state':state,'automatic_replay':False,'monetary_spend':None}

    def _complete(self,c,token,reservation_id):
        row=self._get(c,token,reservation_id);actor,config=self._auth(c,token,row['project'])
        if self._quarantined(c):raise Conflict('restored reservation requires verification')
        task=self.store._get(c,actor,row['task_id'])
        current=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(row['task_id'],)).fetchone()
        valid=False
        try:
            if current is not None:
                self.core._bound(c,token,row['task_id'])
                self.core._member(c,row['project'],current['reviewer_id'],'reviewer',config)
                valid=_hierarchy_acceptance(c,task)
        except (Conflict,PermissionDenied,NotFound,ValueError,TypeError,KeyError):pass
        if (task['version']!=row['version'] or task['manifest_digest']!=row['manifest_digest'] or
                config['fingerprint']!=row['config_fingerprint'] or current is None or current['state']!='accepted' or
                current['version']!=row['version'] or current['manifest_digest']!=row['manifest_digest'] or
                task['state']!='reviewed' or not valid):
            return {'reservation_id':row['id'],'state':row['state'],'reconciliation_required':True,'released':False,'automatic_replay':False}
        c.execute("UPDATE operating_budget_reservations SET state='completed',started_at=COALESCE(started_at,?),updated=? WHERE id=?",(self.store._now(),self.store._now(),row['id']))
        self._event(c,row['id'],'completed')
        return {'reservation_id':row['id'],'state':'completed','released':True,'automatic_replay':False,'monetary_spend':None}


    def reconcile(self,token,reservation_id):
        with self.store._transaction() as c:
            return self._complete(c,token,reservation_id)

    def complete_task_in_transaction(self,c,token,task_id):
        """Release only on the exact durable accepted proof, never on a timer."""
        actor=self.store._authenticate(c,token);task=self.store._get(c,actor,task_id)
        self._auth(c,token,task['project'])
        rows=c.execute("SELECT id FROM operating_budget_reservations WHERE task_id=? AND state IN ('reserved','started','uncertain') LIMIT 101",(task_id,)).fetchall()
        if len(rows)>100:raise Conflict('bounded admission completion required')
        return [self._complete(c,token,row['id']) for row in rows]

    def report(self,token,projects,*,limit=50):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):raise ValueError('explicit scoped projects required')
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('bounded admission report required')
        with self.store._transaction() as c:
            for project in projects:self._auth(c,token,project)
            checks=[self._check(c,token,p) for p in projects];marks=','.join('?' for _ in projects)
            rows=c.execute('SELECT id,project,task_id,version,state,created,started_at,updated FROM operating_budget_reservations WHERE project IN ('+marks+') ORDER BY updated DESC,id LIMIT ?',(*projects,limit+1)).fetchall()
            return self._public(c,{'projects':checks,'reservations':[dict(row) for row in rows[:limit]],'truncated':len(rows)>limit,
                'subscription_usage_measured':False,'monetary_spend':None,'new_paid_resources':False})
