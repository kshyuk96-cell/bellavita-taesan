"""Scoped agent directory and durable instruction inbox, not control of arbitrary chats.

Trusted host connectors only observe/notify registered sessions. Notification,
worker acknowledgement, execution and independent verification are separate facts.
Workers must claim the exact Store task before executing and present its lease.
"""
from dataclasses import dataclass
import json
import hashlib
import math
import re
from .maintenance_registry import name, receipt, scoped
from .store import (Conflict, NotFound, PermissionDenied, _project, _task_id,
                    _reject_credentials, _report, canonical_json, digest)


def _hash(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):
        raise ValueError('exact credential or manifest fingerprint required')
    return value


def _reject_known_tokens(c,value):
    """Do not mistake an accidentally supplied registered bearer for an opaque ID."""
    def strings(item):
        if isinstance(item,str):yield item
        elif isinstance(item,dict):
            for k,v in item.items():yield from strings(k);yield from strings(v)
        elif isinstance(item,(tuple,list)):
            for v in item:yield from strings(v)
    for text in strings(value):
        candidates={text,*re.findall(r'[A-Za-z0-9_-]{32,1024}',text)}
        for candidate in candidates:
            if 32<=len(candidate)<=1024 and c.execute('SELECT 1 FROM bridge_tokens WHERE token_hash=?',
                    (hashlib.sha256(candidate.encode()).hexdigest(),)).fetchone():
                raise ValueError('credential cannot be a session, request, or receipt identifier')


@dataclass(frozen=True)
class AgentObservation:
    project: str
    agent_id: str
    session_id: str
    worker_hash: str
    ready: bool
    reference: str
    observed_at: float
    expires_at: float

    def __post_init__(self):
        _project(self.project);name(self.agent_id);name(self.session_id);_hash(self.worker_hash);receipt(self.reference)
        if type(self.ready) is not bool:
            raise ValueError('actual readiness boolean required')
        if any(type(x) not in (int,float) or not math.isfinite(x) or x<=0 for x in (self.observed_at,self.expires_at)) or not self.observed_at<self.expires_at<=self.observed_at+300:
            raise ValueError('bounded readiness validity required')


@dataclass(frozen=True)
class DeliveryReceipt:
    delivery_id: str
    task_id: str
    version: int
    manifest_digest: str
    reference: str

    def __post_init__(self):
        _hash(self.delivery_id);_task_id(self.task_id);_hash(self.manifest_digest);receipt(self.reference)
        if type(self.version) is not int or self.version<1:
            raise ValueError('exact task version required')


@dataclass(frozen=True)
class Connector:
    observe: object
    sender: object = None
    read_only_probe: bool = True

    def __post_init__(self):
        if not callable(self.observe) or self.read_only_probe is not True or (self.sender is not None and not callable(self.sender)):
            raise ValueError('trusted registered session connector required')


class AgentDirectory:
    def __init__(self,store,connectors=None):
        self.store=store;self.connectors=dict(connectors or {})
        for key,value in self.connectors.items():
            if type(key) is not tuple or len(key)!=2 or not isinstance(value,Connector):
                raise ValueError('project/session connector registry required')
            _project(key[0]);name(key[1])
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS manager_agents (
                project TEXT,agent_id TEXT,worker_hash TEXT,session_id TEXT,
                enabled INTEGER,revision INTEGER,PRIMARY KEY(project,agent_id));
            CREATE TABLE IF NOT EXISTS manager_agent_observations (
                project TEXT,agent_id TEXT,revision INTEGER,ready INTEGER,
                reference TEXT,observed_at REAL,expires_at REAL,
                PRIMARY KEY(project,agent_id));
            CREATE TABLE IF NOT EXISTS manager_agent_inbox (
                id TEXT PRIMARY KEY,project TEXT,agent_id TEXT,request_key TEXT,
                task_id TEXT,version INTEGER,manifest_digest TEXT,agent_revision INTEGER,
                state TEXT,reference TEXT,ack_worker TEXT,created REAL,
                UNIQUE(project,request_key));
            ''')

    def _worker(self,c,actor,project,worker_hash):
        row=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(worker_hash,)).fetchone()
        if row is None or row['role']!='worker' or row['revoked'] is not None or (row['expires'] is not None and row['expires']<=self.store._now()):
            raise PermissionDenied('worker connection unavailable')
        projects=json.loads(row['projects'])
        if project not in projects:
            raise PermissionDenied('worker outside project')
        if actor['role'] in ('manager','owner') and actor['projects']!=['*'] and not set(projects)<=set(actor['projects']):
            raise PermissionDenied('worker spans projects outside manager authority')
        return row

    def _agent(self,c,token,project,agent_id,roles=('owner','manager'),*,live_worker=True):
        actor=scoped(self.store,c,token,project,roles)
        row=c.execute('SELECT * FROM manager_agents WHERE project=? AND agent_id=?',(project,agent_id)).fetchone()
        if row is None:raise NotFound('agent not registered')
        if live_worker:self._worker(c,actor,project,row['worker_hash'])
        if actor['role']=='worker' and actor['token_hash']!=row['worker_hash']:
            raise PermissionDenied('inbox belongs to another worker')
        return actor,row

    def _ready(self,c,row):
        if not row['enabled']:return 'disabled'
        observed=c.execute('SELECT * FROM manager_agent_observations WHERE project=? AND agent_id=?',(row['project'],row['agent_id'])).fetchone()
        if observed is None or observed['revision']!=row['revision']:return 'disconnected'
        now=self.store._now()
        if not observed['observed_at']<=now<observed['expires_at']:return 'stale'
        return 'ready' if observed['ready'] else 'unavailable'

    @staticmethod
    def _public(row,state):
        return {'project':row['project'],'agent_id':row['agent_id'],'session_id':row['session_id'],
                'revision':row['revision'],'state':state,'existing_chat_control_verified':False}

    def register(self,token,project,agent_id,worker_hash,session_id,*,enabled=False,expected_revision=0):
        _project(project);name(agent_id);name(session_id);_hash(worker_hash)
        if type(enabled) is not bool or type(expected_revision) is not int or expected_revision<0:
            raise ValueError('explicit activation and revision required')
        _reject_credentials({'project':project,'agent_id':agent_id,'session_id':session_id},token)
        with self.store._transaction() as c:
            actor=scoped(self.store,c,token,project)
            _reject_known_tokens(c,{'agent_id':agent_id,'session_id':session_id})
            self._worker(c,actor,project,worker_hash)
            old=c.execute('SELECT * FROM manager_agents WHERE project=? AND agent_id=?',(project,agent_id)).fetchone()
            if expected_revision!=(old['revision'] if old else 0):raise Conflict('agent registration changed')
            if old and (old['worker_hash'],old['session_id'])!=(worker_hash,session_id):
                raise Conflict('register a new agent identity for a different worker/session')
            c.execute('INSERT OR REPLACE INTO manager_agents VALUES (?,?,?,?,?,?)',
                      (project,agent_id,worker_hash,session_id,int(enabled),expected_revision+1))
            row=c.execute('SELECT * FROM manager_agents WHERE project=? AND agent_id=?',(project,agent_id)).fetchone()
            return self._public(row,self._ready(c,row))

    def probe(self,token,project,agent_id):
        with self.store._transaction() as c:
            actor,row=self._agent(c,token,project,agent_id)
            revision=row['revision'];worker_hash=row['worker_hash'];session=row['session_id']
            if not row['enabled']:return self._public(row,'disabled')
        connector=self.connectors.get((project,agent_id))
        observed=None
        if connector:
            try:
                value=connector.observe(project,agent_id,session)
                if not isinstance(value,AgentObservation) or (value.project,value.agent_id,value.session_id,value.worker_hash)!=(project,agent_id,session,worker_hash):
                    raise ValueError('session observation mismatch')
                _reject_credentials(value.reference,token)
                now=self.store._now()
                if not value.observed_at<=now<value.expires_at:raise ValueError('readiness not current')
                observed=value
            except Exception:pass
        if observed:
            with self.store._transaction() as c:
                self._agent(c,token,project,agent_id)
                try:_reject_known_tokens(c,observed.reference)
                except ValueError:observed=None
        from .coordination import Coordination
        ttl=int(observed.expires_at-self.store._now()) if observed else 1
        if ttl<1:observed=None;ttl=1
        Coordination(self.store).readiness(token,worker_hash,ready=bool(observed and observed.ready),
            reason='ready' if observed and observed.ready else 'environment_unverified',ttl=min(ttl,300))
        with self.store._transaction() as c:
            _,row=self._agent(c,token,project,agent_id)
            if row['revision']!=revision:raise Conflict('agent registration changed during probe')
            if observed:
                c.execute('INSERT OR REPLACE INTO manager_agent_observations VALUES (?,?,?,?,?,?,?)',
                          (project,agent_id,revision,int(observed.ready),observed.reference,observed.observed_at,observed.expires_at))
            else:
                c.execute('DELETE FROM manager_agent_observations WHERE project=? AND agent_id=?',(project,agent_id))
            return self._public(row,self._ready(c,row))

    def list(self,token,project):
        with self.store._transaction() as c:
            actor=scoped(self.store,c,token,project)
            rows=c.execute('SELECT * FROM manager_agents WHERE project=? ORDER BY agent_id',(project,)).fetchall()
            values=[]
            for row in rows:
                try:self._worker(c,actor,project,row['worker_hash']);state=self._ready(c,row)
                except PermissionDenied:state='disabled'
                values.append(self._public(row,state))
            return values

    def _task(self,c,actor,inbox,*,allowed=('queued','running')):
        task=self.store._get(c,actor,inbox['task_id'])
        _reject_known_tokens(c,json.loads(task['manifest']))
        if (task['project'],task['version'],task['manifest_digest'])!=(inbox['project'],inbox['version'],inbox['manifest_digest']):
            raise Conflict('instruction task version or scope changed')
        approval=json.loads(task['approval']) if task['approval'] else {}
        policy=c.execute('SELECT policy_digest FROM bridge_task_policy WHERE task=?',(task['id'],)).fetchone()
        if approval.get('version')!=task['version'] or approval.get('manifest_digest')!=task['manifest_digest'] or approval.get('policy_digest')!=(policy['policy_digest'] if policy else None):
            raise PermissionDenied('exact task approval required')
        if task['state'] not in allowed:raise Conflict('task is not executable in this state')
        self.store._validate_execution(c,task,check_source_age=task['state']=='queued')
        return task

    def enqueue(self,token,project,agent_id,request_key,task_id,version,manifest_digest):
        name(request_key);_task_id(task_id);_hash(manifest_digest)
        if type(version) is not int or version<1:raise ValueError('task version required')
        _reject_credentials({'request_key':request_key,'agent_id':agent_id},token)
        with self.store._transaction() as c:
            actor,agent=self._agent(c,token,project,agent_id)
            _reject_known_tokens(c,{'request_key':request_key,'agent_id':agent_id})
            if self._ready(c,agent)!='ready':raise Conflict('agent connection not ready')
            task=self._task(c,actor,{'project':project,'task_id':task_id,'version':version,'manifest_digest':manifest_digest})
            routing=task['state']=='queued' and c.execute('SELECT 1 FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone() is not None
            worker_hash=agent['worker_hash']
            old=c.execute('SELECT 1 FROM manager_agent_inbox WHERE project=? AND request_key=?',(project,request_key)).fetchone()
        if routing and not old:
            from .coordination import Coordination
            routed=Coordination(self.store).route(token,task_id,worker_hash=worker_hash)
            if routed['status']!='routed':raise Conflict('registered worker not currently routable')
        with self.store._transaction() as c:
            actor,agent=self._agent(c,token,project,agent_id)
            _reject_known_tokens(c,{'request_key':request_key,'agent_id':agent_id})
            if self._ready(c,agent)!='ready':raise Conflict('agent connection not ready')
            inbox={'project':project,'task_id':task_id,'version':version,'manifest_digest':manifest_digest}
            task=self._task(c,actor,inbox)
            worker=self._worker(c,actor,project,agent['worker_hash'])
            if json.loads(task['manifest'])['kind'] not in json.loads(worker['capabilities']):
                raise PermissionDenied('worker lacks task capability')
            if task['state']=='running' and task['worker_token_hash']!=agent['worker_hash']:
                raise Conflict('task already assigned to another worker')
            old=c.execute('SELECT * FROM manager_agent_inbox WHERE project=? AND request_key=?',(project,request_key)).fetchone()
            key=digest({'project':project,'request_key':request_key})
            if old:
                if (old['agent_id'],old['task_id'],old['version'],old['manifest_digest'],old['agent_revision'])!=(agent_id,task_id,version,manifest_digest,agent['revision']):
                    raise Conflict('instruction key already identifies different work')
                return self._delivery(old)
            # Queue routing is metadata. Store.claim still checks policy, dependencies,
            # worker health, capabilities and global repository reservations.
            if task['state']=='queued':
                route=c.execute('SELECT worker_hash FROM bridge_task_route WHERE task=?',(task_id,)).fetchone()
                if route and route['worker_hash']!=agent['worker_hash']:
                    raise Conflict('existing route points to another worker')
                c.execute('INSERT OR REPLACE INTO bridge_task_route VALUES (?,?,?)',(task_id,agent['worker_hash'],self.store._now()))
            c.execute('INSERT INTO manager_agent_inbox VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)',
                      (key,project,agent_id,request_key,task_id,version,manifest_digest,agent['revision'],'pending',None,self.store._now()))
            return self._delivery(c.execute('SELECT * FROM manager_agent_inbox WHERE id=?',(key,)).fetchone())

    @staticmethod
    def _delivery(row):
        return {'id':row['id'],'project':row['project'],'agent_id':row['agent_id'],'task_id':row['task_id'],
                'version':row['version'],'manifest_digest':row['manifest_digest'],'state':row['state'],
                'reference':row['reference'],'execution_verified':False,'independently_verified':False}

    def _inbox(self,c,token,delivery_id,roles=('owner','manager'),*,live_worker=True):
        _hash(delivery_id)
        row=c.execute('SELECT * FROM manager_agent_inbox WHERE id=?',(delivery_id,)).fetchone()
        if row is None:raise NotFound('instruction not found')
        actor,agent=self._agent(c,token,row['project'],row['agent_id'],roles,live_worker=live_worker)
        if live_worker and row['agent_revision']!=agent['revision']:raise Conflict('agent registration changed')
        return actor,agent,row

    def get(self,token,delivery_id):
        with self.store._transaction() as c:
            actor,_,row=self._inbox(c,token,delivery_id,live_worker=False)
            task=self.store._get(c,actor,row['task_id'])
            return {**self._delivery(row),'task_state':task['state']}

    def send(self,token,delivery_id):
        with self.store._transaction() as c:
            actor,agent,row=self._inbox(c,token,delivery_id)
            if self._ready(c,agent)!='ready':raise Conflict('agent not ready')
            self._task(c,actor,row)
            if row['state']!='pending':return self._delivery(row)
            connector=self.connectors.get((row['project'],row['agent_id']))
            if connector is None or connector.sender is None:
                return {**self._delivery(row),'notification':'not_configured'}
            c.execute("UPDATE manager_agent_inbox SET state='sending' WHERE id=?",(delivery_id,))
            envelope=self._delivery(row);envelope['state']='sending'
        observed=None
        try:
            value=connector.sender(json.loads(canonical_json(envelope)))
            if not isinstance(value,DeliveryReceipt) or (value.delivery_id,value.task_id,value.version,value.manifest_digest)!=(delivery_id,row['task_id'],row['version'],row['manifest_digest']):
                raise ValueError('delivery receipt mismatch')
            _reject_credentials(value.reference,token)
            with self.store._transaction() as c:_reject_known_tokens(c,value.reference)
            observed=value.reference
        except Exception:pass
        with self.store._transaction() as c:
            # Store a conservative outcome even if caller is now revoked. Never invoke
            # a sender again after an ambiguous transport outcome or process restart.
            current=c.execute('SELECT * FROM manager_agent_inbox WHERE id=?',(delivery_id,)).fetchone()
            if current and current['state']=='sending':
                c.execute('UPDATE manager_agent_inbox SET state=?,reference=? WHERE id=?',
                          ('notified' if observed else 'uncertain',observed,delivery_id))
        with self.store._transaction() as c:
            actor,agent,current=self._inbox(c,token,delivery_id)
            self._task(c,actor,current)
            return self._delivery(current)

    def poll(self,worker_token,project,agent_id,*,limit=20):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('bounded inbox page required')
        with self.store._transaction() as c:
            actor,agent=self._agent(c,worker_token,project,agent_id,('worker',))
            if self._ready(c,agent)!='ready':return []
            rows=c.execute("SELECT * FROM manager_agent_inbox WHERE project=? AND agent_id=? AND state IN ('pending','notified','sending','uncertain') ORDER BY created,id LIMIT ?",(project,agent_id,limit)).fetchall()
            result=[]
            for row in rows:
                if row['agent_revision']!=agent['revision']:continue
                try:task=self._task(c,actor,row)
                except (Conflict,PermissionDenied):continue
                if task['state']=='running' and task['worker_token_hash']!=actor['token_hash']:continue
                result.append({**self._delivery(row),'manifest':json.loads(task['manifest']),
                               'claim_required':task['state']=='queued','delivery_is_execution_approval':False})
            return result

    def acknowledge(self,worker_token,delivery_id,lease_token):
        with self.store._transaction() as c:
            actor,agent,row=self._inbox(c,worker_token,delivery_id,('worker',))
            if not agent['enabled']:raise PermissionDenied('agent disabled')
            task=self._task(c,actor,row,allowed=('running',))
            self.store._lease(task,actor,lease_token,c=c)
            if task['lease_expires']<=self.store._now():raise Conflict('execution lease expired')
            if row['state']=='reported':raise Conflict('instruction already reported')
            if row['ack_worker'] and row['ack_worker']!=actor['token_hash']:
                raise PermissionDenied('another worker acknowledged')
            c.execute("UPDATE manager_agent_inbox SET state='acknowledged',ack_worker=? WHERE id=?",(actor['token_hash'],delivery_id))
            return {'id':delivery_id,'state':'acknowledged','execution_verified':False}

    def report(self,worker_token,delivery_id,lease_token,report):
        report=_report(report);_reject_credentials(report,worker_token,lease_token)
        with self.store._transaction() as c:
            actor,agent,row=self._inbox(c,worker_token,delivery_id,('worker',))
            if not agent['enabled']:raise PermissionDenied('agent disabled')
            _reject_known_tokens(c,report)
            if row['state'] not in ('acknowledged','reported') or row['ack_worker']!=actor['token_hash']:
                raise PermissionDenied('worker acknowledgement required')
            task=self._task(c,actor,row,allowed=('running','reported'))
            self.store._lease(task,actor,lease_token,c=c)
            if task['state']=='reported':
                if digest(json.loads(task['report']))!=digest(report):raise Conflict('reported instruction result changed')
                c.execute("UPDATE manager_agent_inbox SET state='reported' WHERE id=?",(delivery_id,))
                return {'id':delivery_id,'state':'reported','task_state':'reported','independently_verified':False}
            task_id=row['task_id']
        result=self.store.report(worker_token,task_id,lease_token,report)
        with self.store._transaction() as c:
            _,_,row=self._inbox(c,worker_token,delivery_id,('worker',))
            if row['state']!='acknowledged':raise Conflict('instruction changed during report')
            c.execute("UPDATE manager_agent_inbox SET state='reported' WHERE id=?",(delivery_id,))
        return {'id':delivery_id,'state':'reported','task_state':result['state'],'independently_verified':False}

    def control(self,token,delivery_id,*,action,request_key=None,manifest=None,reason='manager_control'):
        if action not in ('pause','cancel','change'):raise ValueError('registered control action required')
        _reject_credentials({'reason':reason,'request_key':request_key,'manifest':manifest},token)
        with self.store._transaction() as c:
            actor,_,row=self._inbox(c,token,delivery_id,live_worker=False)
            task=self.store._get(c,actor,row['task_id'])
            if (task['version'],task['manifest_digest'])!=(row['version'],row['manifest_digest']):raise Conflict('instruction source changed')
            task_id=row['task_id']
        if action=='change':
            from .workflow import Workflow
            successor=Workflow(self.store).propose_change(token,task_id,request_key=request_key,manifest=manifest,reason=reason)
            result={'successor':successor,'state':'pending_approval','execution_stop_confirmed':task['worker_subject'] is None}
        else:
            cancelled=self.store.cancel(token,task_id,reason=reason)
            result={'state':'cancelled','execution_stop_confirmed':task['worker_subject'] is None,'task_state':cancelled['state']}
        with self.store._transaction() as c:
            self._inbox(c,token,delivery_id,live_worker=False)
            c.execute("UPDATE manager_agent_inbox SET state='cancelled' WHERE id=?",(delivery_id,))
        return {**result,'rollback_performed':False,'automatic_resume':False}
