"""Durable bounded coordinator for explicitly registered development handlers.

This controller does not invent work, approve new scope, create model credentials,
or claim arbitrary UI chats are connected. Registered handlers must use ThreeTier
for every transition. A callback receipt alone cannot confirm technical acceptance.
"""
from dataclasses import dataclass,asdict
import json
import math
import threading
import secrets
from .store import Conflict,PermissionDenied,NotFound,canonical_json,digest,_task_id
from .maintenance_registry import name,receipt
from .agent_directory import _reject_known_tokens
from .three_tier import ThreeTier

PHASES=frozenset({'start','dispatch_development','collect_development','review','collect_review',
    'technical_decision','rework_decision','dispatch_rework','reconcile','approve_next'})
PHASE_FOR={'development_queued':'dispatch_development','developing':'collect_development',
    'candidate_ready':'review','reviewing':'collect_review','review_passed':'technical_decision',
    'review_failed':'rework_decision','rework_queued':'dispatch_rework',
    'rework_developing':'collect_development','needs_verification':'reconcile'}
ADVANCES={'start':{'development_queued','developing'},'dispatch_development':{'developing','candidate_ready'},
 'collect_development':{'candidate_ready'},'review':{'reviewing','review_passed','review_failed'},
 'collect_review':{'review_passed','review_failed'},'technical_decision':{'accepted','rework_queued'},
 'rework_decision':{'rework_queued','needs_verification'},'dispatch_rework':{'rework_developing','candidate_ready'},
 'reconcile':{'development_queued','candidate_ready','review_passed','review_failed','accepted','rework_queued'}}


def _hex(value):
    if type(value) is not str or len(value)!=64 or any(x not in '0123456789abcdef' for x in value):raise ValueError('exact digest required')
    return value


@dataclass(frozen=True)
class ActionRequest:
    action_key:str
    task_id:str
    project:str
    phase:str
    version:int
    manifest_digest:str
    config_fingerprint:str
    state:str
    candidate_digest:str|None=None
    next_task_id:str|None=None
    prior_action_key:str|None=None
    next_version:int|None=None
    next_manifest_digest:str|None=None
    def __post_init__(self):
        _hex(self.action_key);_task_id(self.task_id);name(self.project)
        if self.phase not in PHASES or type(self.version) is not int or self.version<1:raise ValueError('registered action phase required')
        _hex(self.manifest_digest);_hex(self.config_fingerprint);name(self.state)
        for value in (self.candidate_digest,self.prior_action_key):
            if value is not None:_hex(value)
        if self.next_task_id is not None:
            _task_id(self.next_task_id)
            if type(self.next_version) is not int or self.next_version<1:raise ValueError('exact next task version required')
            _hex(self.next_manifest_digest)
        elif self.next_version is not None or self.next_manifest_digest is not None:raise ValueError('next binding without task')


@dataclass(frozen=True)
class ActionReceipt:
    action_key:str
    task_id:str
    phase:str
    state:str
    reference:str
    observed_at:float
    def __post_init__(self):
        _hex(self.action_key);_task_id(self.task_id)
        if self.phase not in PHASES or self.state not in {'observed','not_ready','uncertain','unavailable'}:raise ValueError('typed action outcome required')
        receipt(self.reference)
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:raise ValueError('actual observation time required')


class ContinuousManager:
    def __init__(self,core,*,handlers=None,max_attempts=8):
        if not isinstance(core,ThreeTier):raise ValueError('registered ThreeTier core required')
        if type(max_attempts) is not int or not 1<=max_attempts<=20:raise ValueError('bounded retry policy required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.handlers=dict(handlers or {});self.max_attempts=max_attempts
        for phase,handler in self.handlers.items():
            if phase not in PHASES or not callable(handler):raise ValueError('registered development phase handler required')
        with self.store._transaction() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS continuous_leader (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),instance TEXT,actor_hash TEXT,
                projects_digest TEXT,epoch INTEGER,expires REAL);
                CREATE TABLE IF NOT EXISTS continuous_tick_guard (singleton INTEGER PRIMARY KEY CHECK(singleton=1),nonce TEXT,epoch INTEGER,expires REAL);
                CREATE TABLE IF NOT EXISTS continuous_actions (
                action_key TEXT PRIMARY KEY,task_id TEXT,project TEXT,phase TEXT,request TEXT,
                actor_hash TEXT,epoch INTEGER,state TEXT,attempts INTEGER,next_due REAL,
                reference TEXT,reason TEXT,created REAL,updated REAL);
                CREATE INDEX IF NOT EXISTS continuous_action_task ON continuous_actions(task_id,state);
                CREATE TABLE IF NOT EXISTS continuous_scan_cursor (scope TEXT PRIMARY KEY,created REAL,task_id TEXT);
                CREATE TABLE IF NOT EXISTS continuous_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,action_key TEXT,kind TEXT,recorded REAL,payload_digest TEXT);''')

    def _auth(self,c,token,projects,*,read_only=False):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or len(set(projects))!=len(projects):raise ValueError('explicit bounded projects required')
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager') if read_only else ('manager',))
        configs={}
        for project in projects:
            name(project);config=self.registry._get(c,token,project,('owner','manager') if read_only else ('manager',))
            if read_only:
                configs[project]=config
                continue
            self.core._actor(c,token,project,'taesan',config)
            if not config['config']['maintenance_enabled']:raise PermissionDenied('continuous development disabled')
            self.core._capacity(c,project)
            configs[project]=config
        return actor,configs

    def _event(self,c,key,kind,payload=None):
        c.execute('INSERT INTO continuous_events(action_key,kind,recorded,payload_digest) VALUES (?,?,?,?)',
                  (key,kind,self.store._now(),digest(payload) if payload is not None else None))

    def _leader(self,c,token,projects,instance,seconds,nonce):
        actor,configs=self._auth(c,token,projects);now=self.store._now();scope=digest(sorted(projects))
        guard=c.execute('SELECT * FROM continuous_tick_guard WHERE singleton=1').fetchone()
        if guard is not None and guard['expires']>now:return None,configs
        current=c.execute('SELECT * FROM continuous_leader WHERE singleton=1').fetchone()
        if current is not None and current['expires']>now:
            if (current['instance'],current['actor_hash'],current['projects_digest'])!=(instance,actor['token_hash'],scope):return None,configs
            epoch=current['epoch']
        else:
            epoch=1 if current is None else current['epoch']+1
            # A dead observer does not establish that its external command stopped.
            c.execute("UPDATE continuous_actions SET state='uncertain',reason='leader_expired',updated=? WHERE state='running'",(now,))
        c.execute('INSERT OR REPLACE INTO continuous_leader VALUES (1,?,?,?,?,?)',(instance,actor['token_hash'],scope,epoch,now+seconds))
        c.execute('INSERT OR REPLACE INTO continuous_tick_guard VALUES (1,?,?,?)',(nonce,epoch,now+seconds))
        return epoch,configs

    def _fence(self,c,token,projects,instance,epoch,seconds,request=None,stop_event=None,*,nonce,require_initial_state=False):
        if stop_event is not None and stop_event.is_set():raise Conflict('continuous shutdown requested')
        actor,configs=self._auth(c,token,projects);leader=c.execute('SELECT * FROM continuous_leader WHERE singleton=1').fetchone();now=self.store._now()
        if leader is None or (leader['instance'],leader['actor_hash'],leader['epoch'],leader['projects_digest'])!=(instance,actor['token_hash'],epoch,digest(sorted(projects))) or leader['expires']<=now:raise Conflict('continuous leader lease lost')
        guard=c.execute('SELECT * FROM continuous_tick_guard WHERE singleton=1').fetchone()
        if guard is None or (guard['nonce'],guard['epoch'])!=(nonce,epoch) or guard['expires']<=now:raise Conflict('continuous tick ownership lost')
        if request is not None:
            if configs[request.project]['fingerprint']!=request.config_fingerprint:raise Conflict('continuous project configuration changed')
            task=self.store._get(c,actor,request.task_id)
            if task['version']!=request.version or task['manifest_digest']!=request.manifest_digest or task['state'] in ('cancelled','pending_approval','needs_verification') and request.phase!='reconcile':raise Conflict('continuous task authority changed')
            if request.next_task_id is not None:
                following=self.store._get(c,actor,request.next_task_id)
                if following['project']!=request.project or following['version']!=request.next_version or following['manifest_digest']!=request.next_manifest_digest:raise Conflict('next task binding changed')
            if require_initial_state:
                managed=c.execute('SELECT state,candidate FROM hierarchy_tasks WHERE task_id=?',(request.task_id,)).fetchone()
                current_state=managed['state'] if managed is not None else 'unassigned'
                current_candidate=json.loads(managed['candidate'])['digest'] if managed is not None and managed['candidate'] else None
                if current_state!=request.state or current_candidate!=request.candidate_digest:raise Conflict('continuous phase changed before dispatch')
            if request.phase!='reconcile':self.store._validate_execution(c,task,check_source_age=False)
        c.execute('UPDATE continuous_leader SET expires=? WHERE singleton=1 AND epoch=?',(now+seconds,epoch))
        c.execute('UPDATE continuous_tick_guard SET expires=? WHERE singleton=1 AND nonce=? AND epoch=?',(now+seconds,nonce,epoch))
        return actor,configs

    def _request(self,task,config,state,phase,*,candidate=None,next_task=None,rework=0,prior=None,next_version=None,next_manifest_digest=None):
        identity={'task_id':task['id'],'project':task['project'],'phase':phase,'version':task['version'],
            'manifest_digest':task['manifest_digest'],'config_fingerprint':config['fingerprint'],
            'state':state,'candidate_digest':candidate,'next_task_id':next_task,'rework_count':rework,'prior_action_key':prior,'next_version':next_version,'next_manifest_digest':next_manifest_digest}
        return ActionRequest(digest(identity),task['id'],task['project'],phase,task['version'],task['manifest_digest'],config['fingerprint'],state,candidate,next_task,prior,next_version,next_manifest_digest)

    def _plans(self,c,actor,configs,scan_limit):
        placeholders=','.join('?' for _ in configs)
        scan_scope=digest({'actor':actor['token_hash'],'projects':sorted(configs)})
        cursor=c.execute('SELECT created,task_id FROM continuous_scan_cursor WHERE scope=?',(scan_scope,)).fetchone()
        after_created,after_id=(cursor['created'],cursor['task_id']) if cursor else (0,'')
        rows=c.execute("SELECT t.*,h.state AS hierarchy_state,h.candidate AS hierarchy_candidate,h.rework_count "
            "FROM bridge_tasks t LEFT JOIN hierarchy_tasks h ON h.task_id=t.id WHERE t.project IN ("+placeholders+") "
            "AND (t.state='queued' OR (h.state IS NOT NULL AND h.state!='accepted' AND t.state!='cancelled') OR EXISTS (SELECT 1 FROM continuous_actions a WHERE a.task_id=t.id AND a.state IN ('running','uncertain'))) "
            "AND (t.created>? OR (t.created=? AND t.id>?)) ORDER BY t.created,t.id LIMIT ?",(*configs,after_created,after_created,after_id,scan_limit)).fetchall()
        last=(rows[-1]['created'],rows[-1]['id']) if len(rows)==scan_limit else (0,'')
        c.execute('INSERT OR REPLACE INTO continuous_scan_cursor VALUES (?,?,?)',(scan_scope,*last))
        plans=[]
        for task in rows:
            config=configs[task['project']]
            prior=c.execute("SELECT action_key FROM continuous_actions WHERE task_id=? AND phase!='reconcile' AND state IN ('running','uncertain') ORDER BY created LIMIT 1",(task['id'],)).fetchone()
            candidate=json.loads(task['hierarchy_candidate'])['digest'] if task['hierarchy_candidate'] else None
            state=task['hierarchy_state'] or 'unassigned'
            if prior:
                plans.append(self._request(task,config,state,'reconcile',candidate=candidate,rework=task['rework_count'] or 0,prior=prior['action_key']));continue
            approval=json.loads(task['approval']) if task['approval'] else {}
            policy=c.execute('SELECT * FROM bridge_task_policy WHERE task=?',(task['id'],)).fetchone()
            if policy is None or approval.get('version')!=task['version'] or approval.get('manifest_digest')!=task['manifest_digest'] or approval.get('policy_digest')!=policy['policy_digest']:continue
            try:self.store._validate_execution(c,task,check_source_age=task['state']=='queued')
            except (Conflict,PermissionDenied,NotFound):continue
            if task['hierarchy_state'] is None:
                if task['state']!='queued':continue
                dependencies=json.loads(policy['policy'])['dependencies']
                if dependencies:
                    # Follow-on code must use an explicit exact accepted parent.
                    if len(dependencies)!=1:continue
                    parent=c.execute('SELECT t.*,h.candidate,h.state AS parent_state FROM bridge_tasks t JOIN hierarchy_tasks h ON h.task_id=t.id WHERE t.id=?',(dependencies[0]['id'],)).fetchone()
                    from .store import _hierarchy_acceptance
                    if parent is None or parent['parent_state']!='accepted' or parent['project']!=task['project'] or not _hierarchy_acceptance(c,parent):continue
                    parent_candidate=json.loads(parent['candidate'])
                    if json.loads(task['manifest'])['base_sha']!=parent_candidate['source']['head_sha']:continue
                    plans.append(self._request(parent,config,'accepted','approve_next',candidate=parent_candidate['digest'],next_task=task['id'],next_version=task['version'],next_manifest_digest=task['manifest_digest']));continue
                plans.append(self._request(task,config,state,'start'));continue
            phase=PHASE_FOR.get(state)
            if phase:plans.append(self._request(task,config,state,phase,candidate=candidate,rework=task['rework_count']))
        return plans

    def _progress(self,token,request):
        if request.phase=='reconcile' and request.prior_action_key:
            # Recovery observes the original effect, not a transition away from
            # the state already reached before its response was lost.
            with self.store._transaction() as c:
                actor=self.store._authenticate(c,token)
                row=c.execute('SELECT * FROM continuous_actions WHERE action_key=?',(request.prior_action_key,)).fetchone()
                if row is None or row['actor_hash']!=actor['token_hash'] or row['state']!='uncertain' or row['task_id']!=request.task_id or row['project']!=request.project:return False,None
                original=ActionRequest(**json.loads(row['request']))
                if original.phase=='reconcile' or original.version!=request.version or original.manifest_digest!=request.manifest_digest or original.config_fingerprint!=request.config_fingerprint:return False,None
                for task_id,version,manifest in ((original.task_id,original.version,original.manifest_digest),(original.next_task_id,original.next_version,original.next_manifest_digest)):
                    if task_id is None:continue
                    task=self.store._get(c,actor,task_id)
                    if task['version']!=version or task['manifest_digest']!=manifest:return False,None
            return self._progress(token,original)
        target=request.next_task_id if request.phase=='approve_next' else request.task_id
        try:current=self.core.get(token,target)
        except (Conflict,PermissionDenied,NotFound):return False,None
        if request.phase=='approve_next':
            with self.store._transaction() as c:
                actual=c.execute('SELECT 1 FROM hierarchy_next_assignments WHERE parent_id=? AND next_id=? AND parent_digest=?',(request.task_id,target,request.candidate_digest)).fetchone()
            valid=actual is not None and current['state'] in ('development_queued','developing')
        else:valid=current['state'] in ADVANCES.get(request.phase,set()) and current['state']!=request.state
        if current['state']=='accepted':valid=valid and current.get('technical_accepted') is True
        return bool(valid),current['state']

    def _mark_uncertain(self,key,epoch,reason):
        with self.store._transaction() as c:
            c.execute("UPDATE continuous_actions SET state='uncertain',reason=?,updated=? WHERE action_key=? AND epoch=? AND state='running'",(reason,self.store._now(),key,epoch))
            self._event(c,key,reason)

    def tick(self,token,projects,instance_id,*,limit=1,lease_seconds=30,stop_event=None,_reconcile_key=None):
        name(instance_id)
        if _reconcile_key is not None:_hex(_reconcile_key)
        if type(limit) is not int or not 1<=limit<=20 or type(lease_seconds) is not int or not 5<=lease_seconds<=300:raise ValueError('bounded continuous tick required')
        if stop_event is not None and not isinstance(stop_event,threading.Event):raise ValueError('registered stop event required')
        if stop_event is not None and stop_event.is_set():return {'state':'stopped','actions':[],'always_on_verified':False,'operational_change':False}
        nonce=secrets.token_hex(16)
        with self.store._transaction() as c:
            epoch,configs=self._leader(c,token,projects,instance_id,lease_seconds,nonce)
            if epoch is None:return {'state':'leader_busy','actions':[],'always_on_verified':False,'operational_change':False}
            actor=self.store._authenticate(c,token);plans=self._plans(c,actor,configs,200)
        results=[]
        for request in plans:
            if _reconcile_key is not None and (request.phase!='reconcile' or request.prior_action_key!=_reconcile_key):continue
            if len(results)>=limit:break
            if stop_event is not None and stop_event.is_set():break
            handler=self.handlers.get(request.phase)
            with self.store._transaction() as c:
                actor,_=self._fence(c,token,projects,instance_id,epoch,lease_seconds,request,stop_event,nonce=nonce,require_initial_state=True)
                # An orphan may still hold a live subprocess against this source.
                # Only an explicit reconciliation request may enter a quarantined
                # project; ordinary ticks cannot infer that the old worker stopped.
                quarantined=c.execute("SELECT 1 FROM continuous_actions WHERE project=? AND state='uncertain' LIMIT 1",(request.project,)).fetchone()
                if quarantined is not None and not (request.phase=='reconcile' and _reconcile_key==request.prior_action_key):
                    results.append({'task_id':request.task_id,'phase':request.phase,'state':'project_quarantined','execution_started':False})
                    continue
                old=c.execute('SELECT * FROM continuous_actions WHERE action_key=?',(request.action_key,)).fetchone();now=self.store._now()
                recovered_handler=old is not None and old['state']=='blocked' and old['reason']=='handler_not_connected' and old['attempts']==0 and handler is not None
                if old is not None and not recovered_handler and (old['state']!='waiting' or old['next_due']>now):continue
                if handler is None:
                    if old is None:
                        c.execute("INSERT INTO continuous_actions VALUES (?,?,?,?,?,?,?,'blocked',0,?,NULL,'handler_not_connected',?,?)",
                            (request.action_key,request.task_id,request.project,request.phase,canonical_json(asdict(request)),actor['token_hash'],epoch,now,now,now))
                    results.append({'task_id':request.task_id,'phase':request.phase,'state':'not_configured','execution_started':False});continue
                attempts=1 if old is None else old['attempts']+1
                if attempts>self.max_attempts:
                    c.execute("UPDATE continuous_actions SET state='blocked',reason='readiness_attempt_limit',updated=? WHERE action_key=?",(now,request.action_key));continue
                # The write intent survives a crash between process start and receipt.
                c.execute("INSERT OR REPLACE INTO continuous_actions VALUES (?,?,?,?,?,?,?,'running',?,?,NULL,NULL,?,?)",
                    (request.action_key,request.task_id,request.project,request.phase,canonical_json(asdict(request)),actor['token_hash'],epoch,attempts,now,old['created'] if old else now,now))
                self._event(c,request.action_key,'execution_intent',asdict(request))
            def heartbeat():
                with self.store._transaction() as c:self._fence(c,token,projects,instance_id,epoch,lease_seconds,request,stop_event,nonce=nonce)
                return True
            try:
                heartbeat();outcome=handler(request,heartbeat)
                if not isinstance(outcome,ActionReceipt) or (outcome.action_key,outcome.task_id,outcome.phase)!=(request.action_key,request.task_id,request.phase) or not 0<=self.store._now()-outcome.observed_at<=300:raise Conflict('exact fresh action receipt required')
                heartbeat()
                if outcome.state=='not_ready':
                    # Contract: no execution/mutation started; only safe readiness observation.
                    progressed,current=self._progress(token,request)
                    state,reason=('uncertain','not_ready_after_core_progress') if progressed else ('waiting','readiness_pending')
                elif outcome.state!='observed':state,reason,current='uncertain' if outcome.state=='uncertain' else 'blocked','handler_'+outcome.state,None
                else:
                    progressed,current=self._progress(token,request)
                    state,reason=('confirmed',None) if progressed else ('uncertain','receipt_without_core_progress')
                with self.store._transaction() as c:
                    self._fence(c,token,projects,instance_id,epoch,lease_seconds,request,stop_event,nonce=nonce)
                    _reject_known_tokens(c,asdict(outcome));now=self.store._now()
                    updated=c.execute('UPDATE continuous_actions SET state=?,next_due=?,reference=?,reason=?,updated=? WHERE action_key=? AND epoch=? AND state=\'running\'',
                        (state,now+min(300,2**min(attempts,8)),outcome.reference,reason,now,request.action_key,epoch))
                    if not updated.rowcount:raise Conflict('action ownership changed')
                    if state=='confirmed' and request.phase=='reconcile' and request.prior_action_key:
                        c.execute("UPDATE continuous_actions SET state='reconciled',reason=NULL,reference=?,updated=? WHERE action_key=? AND state='uncertain'",(outcome.reference,now,request.prior_action_key))
                    self._event(c,request.action_key,'receipt_'+state,asdict(outcome))
                results.append({'task_id':request.task_id,'phase':request.phase,'state':state,'observed_core_state':current,'execution_started':True})
            except BaseException as error:
                self._mark_uncertain(request.action_key,epoch,'shutdown_uncertain' if stop_event is not None and stop_event.is_set() else 'handler_uncertain')
                if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
                results.append({'task_id':request.task_id,'phase':request.phase,'state':'uncertain','execution_started':True})
                # An authority/leader loss must stop this tick, not proceed to other projects.
                try:
                    with self.store._transaction() as c:self._fence(c,token,projects,instance_id,epoch,lease_seconds,stop_event=stop_event,nonce=nonce)
                except (Conflict,PermissionDenied,NotFound):break
        with self.store._transaction() as c:
            c.execute('DELETE FROM continuous_tick_guard WHERE singleton=1 AND nonce=? AND epoch=?',(nonce,epoch))
        return {'state':'stopped' if stop_event is not None and stop_event.is_set() else 'tick_complete','actions':results,
            'always_on_verified':False,'operational_change':False,'existing_ui_chat_control_verified':False}

    def reconcile(self,token,projects,instance_id,action_key,*,lease_seconds=30):
        _hex(action_key)
        with self.store._transaction() as c:
            actor,_=self._auth(c,token,projects)
            row=c.execute('SELECT * FROM continuous_actions WHERE action_key=?',(action_key,)).fetchone()
            if row is None or row['actor_hash']!=actor['token_hash'] or row['project'] not in projects or row['state']!='uncertain':raise Conflict('exact uncertain owned action required')
        return self.tick(token,projects,instance_id,limit=1,lease_seconds=lease_seconds,_reconcile_key=action_key)

    def run(self,token,projects,instance_id,*,stop_event=None,poll_seconds=2,max_ticks=None):
        if type(poll_seconds) not in (int,float) or not 1<=poll_seconds<=60:raise ValueError('bounded polling interval required')
        if max_ticks is not None and (type(max_ticks) is not int or not 1<=max_ticks<=100000):raise ValueError('bounded rehearsal tick count required')
        stop_event=stop_event or threading.Event();ticks=0
        try:
            while not stop_event.is_set() and (max_ticks is None or ticks<max_ticks):
                self.tick(token,projects,instance_id,stop_event=stop_event);ticks+=1
                if max_ticks is None or ticks<max_ticks:stop_event.wait(poll_seconds)
        finally:
            self.release(token,projects,instance_id)
        return {'state':'stopped' if stop_event.is_set() else 'bounded_run_complete','ticks':ticks,'always_on_verified':False,'operational_change':False}

    def release(self,token,projects,instance_id):
        name(instance_id)
        with self.store._transaction() as c:
            actor,_=self._auth(c,token,projects,read_only=True);leader=c.execute('SELECT * FROM continuous_leader WHERE singleton=1').fetchone()
            if leader is not None and (leader['instance'],leader['actor_hash'],leader['projects_digest'])==(instance_id,actor['token_hash'],digest(sorted(projects))):
                c.execute("UPDATE continuous_actions SET state='uncertain',reason='shutdown_uncertain',updated=? WHERE epoch=? AND state='running'",(self.store._now(),leader['epoch']))
                c.execute('DELETE FROM continuous_tick_guard WHERE singleton=1 AND epoch=?',(leader['epoch'],))
                c.execute('UPDATE continuous_leader SET expires=0 WHERE singleton=1')
                return {'released':True,'automatic_replay':False}
            return {'released':False,'automatic_replay':False}

    def status(self,token,projects,*,limit=100):
        if type(limit) is not int or not 1<=limit<=200:raise ValueError('bounded status window required')
        with self.store._transaction() as c:
            actor,_=self._auth(c,token,projects,read_only=True);marks=','.join('?' for _ in projects)
            rows=c.execute('SELECT task_id,project,phase,state,attempts,next_due,reference,reason,updated FROM continuous_actions WHERE actor_hash=? AND project IN ('+marks+') ORDER BY updated DESC,action_key LIMIT ?',
                (actor['token_hash'],*projects,limit+1)).fetchall()
            items=[dict(row) for row in rows[:limit]]
            leader=c.execute('SELECT * FROM continuous_leader WHERE singleton=1').fetchone();now=self.store._now()
        return {'actions':items,'truncated':len(rows)>limit,'leader_observed':bool(leader and leader['expires']>now and leader['actor_hash']==actor['token_hash'] and leader['projects_digest']==digest(sorted(projects))),
            'always_on_verified':False,'operational_change':False,'existing_ui_chat_control_verified':False}
