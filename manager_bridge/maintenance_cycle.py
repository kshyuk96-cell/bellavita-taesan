"""Bounded, opt-in maintenance dispatch and scoped, acknowledgement-based reports.

The host must register trusted sandbox actions. No daemon, timer, child agent,
network, deployment or operational mutation is started by this module. Existing
Store approvals, worker leases, repository reservations and independent review
remain authoritative. Action results are worker claims, never release approval.
"""
from dataclasses import dataclass
import json
import hashlib
import re
import threading
from .maintenance_registry import name, receipt, scoped
from .store import Conflict, NotFound, PermissionDenied, _project, _sha, _task_id, _lease_seconds, _reject_credentials, canonical_json, digest


def _hash(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('exact fingerprint required')
    return value


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    status: str

    def __post_init__(self):
        name(self.name)
        if self.status not in ('pass', 'fail', 'skip'):
            raise ValueError('explicit test outcome required')


@dataclass(frozen=True)
class ActionResult:
    head_sha: str
    checks: tuple
    reference: str

    def __post_init__(self):
        _sha(self.head_sha); receipt(self.reference)
        if type(self.checks) is not tuple or not 1 <= len(self.checks) <= 100 or not all(isinstance(x, CheckOutcome) for x in self.checks):
            raise ValueError('bounded registered check results required')
        if len({x.name for x in self.checks}) != len(self.checks):
            raise ValueError('duplicate result names')


@dataclass(frozen=True)
class Action:
    kind: str
    execute: object
    observe_source: object
    enabled: bool = False
    version: str = 'v1'

    def __post_init__(self):
        if self.kind not in ('read_probe', 'test', 'develop'):
            raise ValueError('maintenance action kind required')
        if not callable(self.execute) or not callable(self.observe_source) or type(self.enabled) is not bool:
            raise ValueError('registered sandbox executor and source observer required')
        name(self.version)

    @property
    def task_kind(self):
        return 'development' if self.kind == 'develop' else 'test'


@dataclass(frozen=True)
class ActionContext:
    project: str
    company: str
    task_id: str
    base_sha: str
    config_fingerprint: str
    cancelled: object


class Cycle:
    def __init__(self, store, registry, *, actions=None, enabled=False):
        self.store, self.registry = store, registry
        if type(enabled) is not bool:
            raise ValueError('explicit host enable flag required')
        self.enabled = enabled
        self.actions = dict(actions or {})
        for key, action in self.actions.items():
            if type(key) is not tuple or len(key) != 2 or not isinstance(action, Action):
                raise ValueError('project-bound action registry required')
            _project(key[0]); name(key[1])
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS maintenance_jobs (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, request_key TEXT NOT NULL,
                task TEXT NOT NULL UNIQUE, action TEXT NOT NULL, binding TEXT NOT NULL,
                fingerprint TEXT NOT NULL, state TEXT NOT NULL, approval TEXT,
                worker_hash TEXT, result TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(project,request_key));
            CREATE TABLE IF NOT EXISTS maintenance_digest_ack (
                actor TEXT NOT NULL,scope TEXT NOT NULL,fingerprint TEXT NOT NULL,
                acknowledged REAL NOT NULL,PRIMARY KEY(actor,scope));
            ''')

    def _get(self, c, token, job_id, *, roles=('owner', 'manager', 'worker')):
        _hash(job_id)
        row = c.execute('SELECT * FROM maintenance_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise NotFound('maintenance job not found')
        scoped(self.store, c, token, row['project'], roles)
        return row

    @staticmethod
    def _public(row):
        return {'id': row['id'], 'project': row['project'], 'task_id': row['task'],
                'action': row['action'], 'fingerprint': row['fingerprint'], 'state': row['state'],
                'binding': json.loads(row['binding']),
                'result': json.loads(row['result']) if row['result'] else None,
                'independently_verified': False, 'operational_change': False}

    def get(self, token, job_id):
        with self.store._transaction() as c:
            return self._public(self._get(c, token, job_id))

    def _binding(self, c, token, project, task_id, action_name, *, roles=('owner','manager')):
        config = self.registry._get(c, token, project, roles=roles)
        actor = scoped(self.store,c,token,project,roles)
        task = self.store._get(c,actor,task_id)
        if task['project'] != project:
            raise Conflict('task belongs to another project')
        manifest = json.loads(task['manifest'])
        action = self.actions.get((project, action_name))
        if action is None:
            raise NotFound('maintenance action not registered')
        if manifest['kind'] != action.task_kind:
            raise Conflict('task kind differs from registered action')
        approval = json.loads(task['approval']) if task['approval'] else {}
        policy = c.execute('SELECT policy_digest FROM bridge_task_policy WHERE task=?',(task_id,)).fetchone()
        policy_digest = policy['policy_digest'] if policy else None
        if (approval.get('version') != task['version'] or approval.get('manifest_digest') != task['manifest_digest']
                or approval.get('policy_digest') != policy_digest):
            raise Conflict('existing exact task approval required')
        return {'project': project, 'company': config['config']['company'],
                'config_fingerprint': config['fingerprint'], 'task_id': task_id,
                'manifest_digest': task['manifest_digest'], 'manifest_version': task['version'],
                'policy_digest': policy_digest, 'repository': manifest['repository'],
                'base_sha': manifest['base_sha'], 'action': action_name,
                'action_kind': action.kind, 'action_version': action.version}, config, task

    def enqueue(self, token, project, request_key, task_id, action_name):
        _project(project); name(request_key); _task_id(task_id); name(action_name)
        with self.store._transaction() as c:
            binding, config, task = self._binding(c,token,project,task_id,action_name)
            _reject_credentials(binding,token)
            fingerprint=digest(binding)
            old=c.execute('SELECT * FROM maintenance_jobs WHERE project=? AND request_key=?',(project,request_key)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('request key reused with changed maintenance scope')
                return self._public(old)
            if task['state'] != 'queued':
                raise Conflict('only unstarted approved tasks can be enqueued')
            if c.execute('SELECT 1 FROM maintenance_jobs WHERE task=?',(task_id,)).fetchone():
                raise Conflict('task already belongs to a maintenance job')
            key=digest({'project':project,'request_key':request_key})
            now=self.store._now()
            c.execute('INSERT INTO maintenance_jobs VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)',
                      (key,project,request_key,task_id,action_name,canonical_json(binding),fingerprint,'pending_approval',now,now))
            return self._public(c.execute('SELECT * FROM maintenance_jobs WHERE id=?',(key,)).fetchone())

    def approve(self, owner_token, job_id, fingerprint):
        _hash(fingerprint)
        with self.store._transaction() as c:
            row=self._get(c,owner_token,job_id,roles=('owner',))
            if row['fingerprint'] != fingerprint:
                raise Conflict('maintenance approval fingerprint changed')
            binding,config,task=self._binding(c,owner_token,row['project'],row['task'],row['action'],roles=('owner',))
            if digest(binding)!=fingerprint or task['state']!='queued':
                raise Conflict('project or task changed after maintenance proposal')
            if row['state']=='queued' and row['approval']:
                return self._public(row)
            if row['state']!='pending_approval':
                raise Conflict('maintenance job cannot be reapproved')
            actor=self.store._authenticate(c,owner_token)
            approval={'actor':actor['subject'],'fingerprint':fingerprint,'approved_at':self.store._now()}
            c.execute("UPDATE maintenance_jobs SET state='queued',approval=?,updated=? WHERE id=?",
                      (canonical_json(approval),self.store._now(),job_id))
            return self._public(c.execute('SELECT * FROM maintenance_jobs WHERE id=?',(job_id,)).fetchone())

    def _uncertain(self, job_id):
        # Internal outcome marking, not permission to retry or claim that a process stopped.
        with self.store._transaction() as c:
            row=c.execute('SELECT task FROM maintenance_jobs WHERE id=?',(job_id,)).fetchone()
            if row is None:return
            c.execute("UPDATE maintenance_jobs SET state='uncertain',updated=? WHERE id=? AND state='running'",
                      (self.store._now(),job_id))
            c.execute("UPDATE bridge_tasks SET state='needs_verification',updated=? WHERE id=? AND state='running'",
                      (self.store._now(),row['task']))

    def _current(self,c,worker_token,row):
        binding,config,task=self._binding(c,worker_token,row['project'],row['task'],row['action'],roles=('worker',))
        if digest(binding)!=row['fingerprint']:
            raise Conflict('maintenance scope changed')
        if not config['config']['maintenance_enabled']:
            raise Conflict('project maintenance execution disabled')
        approval=json.loads(row['approval']) if row['approval'] else {}
        if approval.get('fingerprint')!=row['fingerprint']:
            raise Conflict('exact maintenance approval missing')
        return binding,config,task

    def run_once(self, worker_token, project, *, lease_seconds=300):
        _project(project); _lease_seconds(lease_seconds)
        # Authenticate even while disabled; disabled execution must not become an information oracle.
        with self.store._transaction() as c:
            config=self.registry._get(c,worker_token,project,roles=('worker',))
            if not self.enabled or not config['config']['maintenance_enabled']:
                return {'project':project,'state':'disabled','execution_started':False}
            # Running/uncertain jobs are never automatically reclaimed after restart.
            active=c.execute("SELECT * FROM maintenance_jobs WHERE project=? AND state IN ('running','uncertain') ORDER BY created,id LIMIT 1",(project,)).fetchone()
            if active:
                return {'project':project,'state':'needs_verification' if active['state']=='uncertain' else 'busy',
                        'job_id':active['id'],'execution_started':False}
            row=c.execute("SELECT * FROM maintenance_jobs WHERE project=? AND state='queued' ORDER BY created,id LIMIT 1",(project,)).fetchone()
            if row is None:return {'project':project,'state':'idle','execution_started':False}
            binding,config,task=self._current(c,worker_token,row)
            action=self.actions.get((project,row['action']))
            if action is None or not action.enabled:
                return {'project':project,'state':'not_configured','execution_started':False}
            if task['state']!='queued':
                raise Conflict('core task has already started or stopped')
            actor=self.store._authenticate(c,worker_token)
            c.execute("UPDATE maintenance_jobs SET state='running',worker_hash=?,updated=? WHERE id=? AND state='queued'",
                      (actor['token_hash'],self.store._now(),row['id']))
            job_id=row['id']; stored=dict(row)
        try:
            # Core exact-task claim reuses global repository reservation, policy/dependency
            # checks, worker capability/health and lease verification.
            assignment=self.store.claim(worker_token,project,lease_seconds=lease_seconds,task_id=stored['task'])
            if assignment is None:
                with self.store._transaction() as c:
                    current=self._get(c,worker_token,job_id,roles=('worker',))
                    self._current(c,worker_token,current)
                    actor=self.store._authenticate(c,worker_token)
                    task=self.store._get(c,actor,stored['task'])
                    safe=task['state']=='queued'
                    c.execute('UPDATE maintenance_jobs SET state=?,worker_hash=NULL,updated=? WHERE id=? AND state=?',
                              ('queued' if safe else 'uncertain',self.store._now(),job_id,'running'))
                return {'project':project,'state':'capacity_wait' if safe else 'needs_verification','execution_started':False}
            lease=assignment['lease_token']
            with self.store._transaction() as c:
                current=self._get(c,worker_token,job_id,roles=('worker',))
                binding,config,task=self._current(c,worker_token,current)
                if current['state']!='running' or task['state']!='running':raise Conflict('job cancelled before execution')
                actor=self.store._authenticate(c,worker_token);self.store._lease(task,actor,lease)
            observed=action.observe_source(project)
            if observed!=binding['base_sha']:
                raise Conflict('source changed before maintenance execution')
            with self.store._transaction() as c:
                current=self._get(c,worker_token,job_id,roles=('worker',))
                _,_,task=self._current(c,worker_token,current)
                actor=self.store._authenticate(c,worker_token);self.store._lease(task,actor,lease)
                if current['state']!='running' or task['state']!='running' or task['lease_expires']<=self.store._now():
                    raise Conflict('execution approval changed during source observation')

            def cancelled():
                try:
                    with self.store._transaction() as c:
                        current=self._get(c,worker_token,job_id,roles=('worker',))
                        _,_,task=self._current(c,worker_token,current)
                        return current['state']!='running' or task['state']!='running' or task['lease_expires']<=self.store._now()
                except Exception:return True

            context=ActionContext(project,binding['company'],stored['task'],binding['base_sha'],binding['config_fingerprint'],cancelled)
            value=action.execute(context)
            if not isinstance(value,ActionResult):raise ValueError('registered execution facts required')
            _reject_credentials({'reference':value.reference,'checks':[x.name for x in value.checks]},worker_token,lease)
            if action.observe_source(project)!=value.head_sha:
                raise Conflict('reported head does not match source after execution')
            with self.store._transaction() as c:
                current=self._get(c,worker_token,job_id,roles=('worker',))
                _,_,task=self._current(c,worker_token,current)
                if current['state']!='running' or task['state']!='running' or task['lease_expires']<=self.store._now():
                    raise Conflict('job expired, cancelled, or changed during execution')
                if action.task_kind=='test' and value.head_sha!=binding['base_sha']:
                    raise Conflict('read/test action changed the pinned source')
            self.store.report(worker_token,stored['task'],lease,{
                'head_sha':value.head_sha,'summary':'등록된 유지보수 작업의 결과 주장입니다. 독립 검토와 운영 승인은 별도입니다.',
                'checks':[{'name':x.name,'passed':x.status=='pass'} for x in value.checks]})
            result={'head_sha':value.head_sha,'reference':value.reference,
                    'checks':[{'name':x.name,'status':x.status} for x in value.checks],
                    'counts':{status:sum(x.status==status for x in value.checks) for status in ('pass','fail','skip')},
                    'evidence_kind':'worker_claim','independently_verified':False,'operational_change':False}
            with self.store._transaction() as c:
                current=self._get(c,worker_token,job_id,roles=('worker',))
                _,config,_=self._current(c,worker_token,current)
                if current['state']!='running':raise Conflict('job outcome needs cancellation review')
                c.execute("UPDATE maintenance_jobs SET state='done',result=?,updated=? WHERE id=? AND state='running'",
                          (canonical_json(result),self.store._now(),job_id))
            return self.get(worker_token,job_id)
        except PermissionDenied:
            self._uncertain(job_id)
            raise
        except Exception:
            self._uncertain(job_id)
            return {'project':project,'state':'needs_verification','job_id':job_id,
                    'execution_started':True,'automatically_replayed':False}

    def run_loop(self, worker_token, project, *, max_cycles=1, stop_event=None):
        if type(max_cycles) is not int or not 1<=max_cycles<=100:
            raise ValueError('bounded cycle count required')
        if stop_event is not None and not isinstance(stop_event,threading.Event):
            raise ValueError('host stop event required')
        results=[]
        for _ in range(max_cycles):
            if stop_event is not None and stop_event.is_set():break
            result=self.run_once(worker_token,project)
            results.append(result)
            if result['state']!='done':break
        return {'cycles':len(results),'results':results,'background_process_started':False,
                'schedule_created':False,'stop_requested':bool(stop_event and stop_event.is_set())}

    def cancel(self, token, job_id):
        with self.store._transaction() as c:
            row=dict(self._get(c,token,job_id,roles=('owner','manager')))
        if row['state'] in ('done','cancelled'):
            raise Conflict('completed job cannot be rolled back by cancellation')
        self.store.cancel(token,row['task'],reason='유지보수 작업 취소')
        with self.store._transaction() as c:
            current=self._get(c,token,job_id,roles=('owner','manager'))
            state='uncertain' if current['state'] in ('running','uncertain') else 'cancelled'
            c.execute('UPDATE maintenance_jobs SET state=?,updated=? WHERE id=?',(state,self.store._now(),job_id))
            return {'job_id':job_id,'state':state,'execution_stop_confirmed':state=='cancelled','rollback_performed':False}

    def confirm_stopped(self, token, job_id, reference):
        receipt(reference)
        with self.store._transaction() as c:
            row=dict(self._get(c,token,job_id,roles=('owner','manager')))
            actor=self.store._authenticate(c,token);task=self.store._get(c,actor,row['task'])
            if row['state']!='uncertain' or task['state']!='cancelled':raise Conflict('cancelled uncertain job required')
        from .coordination import Coordination
        proof=Coordination(self.store).confirm_stopped(token,row['task'],task['version'],task['manifest_digest'],reference)
        with self.store._transaction() as c:
            self._get(c,token,job_id,roles=('owner','manager'))
            c.execute("UPDATE maintenance_jobs SET state='cancelled',updated=? WHERE id=? AND state='uncertain'",(self.store._now(),job_id))
        return {**proof,'job_id':job_id,'rollback_performed':False}


class Digest:
    def __init__(self,store,registry):
        self.store,self.registry=store,registry
        # Summary works even when no execution hosts are registered or enabled.
        Cycle(store,registry)

    @staticmethod
    def _validation(c,project,config_fingerprint):
        sections={}
        specifications=(('checks','maintenance_check_plans','plan','fingerprint'),
                        ('data','maintenance_data_plans','body','digest'))
        for label,table,body_column,fingerprint_column in specifications:
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            if not exists:
                sections[label]={'state':'not_measured','plans':[],'truncated':False,'all_records_fingerprint':digest([])}
                continue
            # Names above are fixed local schema constants, never request fields.
            rows=c.execute(f'SELECT id,{body_column}, {fingerprint_column},state,result FROM {table} WHERE project=? ORDER BY id LIMIT 101',(project,)).fetchall()
            all_records=hashlib.sha256()
            for entry in c.execute(f'SELECT id,{fingerprint_column},state,result FROM {table} WHERE project=? ORDER BY id',(project,)):
                all_records.update(canonical_json([entry['id'],entry[fingerprint_column],entry['state'],
                    hashlib.sha256((entry['result'] or '').encode()).hexdigest()]).encode())
            plans=[]
            for row in rows[:100]:
                try:
                    body=json.loads(row[body_column])
                    result=json.loads(row['result']) if row['result'] else None
                    state=row['state'] if body.get('config_fingerprint')==config_fingerprint else 'stale'
                    counts={'passed':None,'failed':None,'skipped':None,'unknown':None}
                    if result is not None and label=='checks':
                        checks=result.get('checks',[])
                        counts={'passed':sum(x.get('passed') is True and x.get('timed_out') is not True and x.get('skipped') is not True for x in checks),
                                'failed':sum((x.get('passed') is False or x.get('timed_out') is True) and x.get('skipped') is not True for x in checks),
                                'skipped':sum(x.get('skipped') is True for x in checks),
                                'unknown':sum(type(x.get('passed')) is not bool and x.get('skipped') is not True and x.get('timed_out') is not True for x in checks)}
                    elif result is not None and label=='data':
                        facts=[result[k] for k in ('schema_matches','data_preserved','existing_column_contract_preserved',
                               'foreign_keys_ok','integrity_ok','rollback_verified') if type(result.get(k)) is bool]
                        if facts:counts={'passed':sum(facts),'failed':len(facts)-sum(facts),'skipped':0,'unknown':0}
                    plans.append({'id':row['id'],'state':state,
                                  'fingerprint':row[fingerprint_column],
                                  'config_fingerprint':body.get('config_fingerprint'),
                                  'source_sha':body.get('source_sha'),
                                  'source_snapshot':body.get('source_snapshot'),
                                  'counts':counts,'current_source_verified':False,
                                  'operational_change':False})
                except (TypeError,ValueError,KeyError):
                    plans.append({'id':row['id'],'state':'unknown','counts':{'passed':None,'failed':None,'skipped':None,'unknown':None},'operational_change':False})
            sections[label]={'state':'observed' if plans else 'not_measured','plans':plans,'truncated':len(rows)>100,
                             'all_records_fingerprint':all_records.hexdigest() if plans else digest([])}
        return sections

    def _observations(self,c,project,config_fingerprint,max_age):
        sections={}
        for label,table in (('backups','maintenance_backup_reports'),('dependencies','maintenance_dependency_reports')):
            empty={'state':'not_measured','facts':[],'operational_change':False}
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is None:
                sections[label]=empty;continue
            row=c.execute(f'SELECT config_fingerprint,report,recorded FROM {table} WHERE project=?',(project,)).fetchone()
            if row is None:
                sections[label]=empty;continue
            try:
                report=json.loads(row['report'])
                stale=(row['config_fingerprint']!=config_fingerprint or not 0<=self.store._now()-row['recorded']<=max_age)
                state='stale' if stale else report['state']
                if label=='backups':
                    facts=[{key:item.get(key) for key in ('backup_id','state','encrypted','readable','checksum_recorded','restore_verified')}
                           for item in report.get('backups',[])[:100]]
                    sections[label]={'state':state,'facts':facts,'truncated':len(report.get('backups',[]))>100,
                                     'production_restore_performed':False,'operational_change':False}
                else:
                    advisories=report.get('advisories',[])
                    sections[label]={'state':state,'source_sha':report.get('source_sha'),
                                     'manifest_digest':report.get('manifest_digest'),'lock_digest':report.get('lock_digest'),
                                     'advisory_fingerprint':digest(advisories),
                                     'counts':{severity:sum(item.get('severity')==severity for item in advisories)
                                               for severity in ('critical','high','normal','unknown')},
                                     'packages_changed':False,'tests_passed':False,'operational_change':False}
            except (TypeError,ValueError,KeyError):
                sections[label]={**empty,'state':'unknown'}
        return sections

    def _snapshot(self,c,token,projects,max_age):
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        if projects is None:
            all_projects=[r['project'] for r in c.execute('SELECT project FROM maintenance_projects ORDER BY project')]
            projects=[p for p in all_projects if self.store._scope(actor,p)]
        elif type(projects) not in (list,tuple) or len(projects)>100 or len(set(projects))!=len(projects):
            raise ValueError('bounded explicit project scope required')
        projects=sorted(projects)
        items=[]
        semantic=[]
        for project in projects:
            config=self.registry._get(c,token,project)
            health=c.execute('SELECT report,recorded FROM maintenance_health WHERE project=?',(project,)).fetchone()
            if not health:
                health_value={'state':'not_checked','observations':[]}
            else:
                health_value=json.loads(health['report'])
                stale=(self.store._now()-health['recorded']>max_age or health['recorded']>self.store._now()
                       or health_value.get('config_fingerprint')!=config['fingerprint'])
                if stale:health_value={**health_value,'state':'stale'}
                health_value={'state':health_value['state'],'observations':[
                    {'name':x['name'],'state':x['state']} for x in health_value['observations']]}
            jobs=[self._job_view(r) for r in c.execute('SELECT * FROM maintenance_jobs WHERE project=? ORDER BY id',(project,))]
            tasks=[{'id':r['id'],'state':r['state']} for r in c.execute('SELECT id,state FROM bridge_tasks WHERE project=? ORDER BY id',(project,))]
            incidents=[dict(r) for r in c.execute('SELECT id,code,target,severity,revision,state,occurrences FROM maintenance_incidents WHERE project=? ORDER BY id',(project,))]
            item={'project':project,'company':config['config']['company'],'name':config['config']['name'],
                  'maintenance_enabled':config['config']['maintenance_enabled'],
                  'config_fingerprint':config['fingerprint'],'health':health_value,'jobs':jobs,'tasks':tasks,'incidents':incidents,
                  'validation':self._validation(c,project,config['fingerprint']),
                  'observations':self._observations(c,project,config['fingerprint'],max_age)}
            items.append(item)
            # Heartbeats, repeated identical errors, receipt rotations and timestamps do
            # not produce new owner messages. Severity/state/revision changes do.
            semantic.append({**item,'incidents':[{k:v for k,v in incident.items() if k!='occurrences'} for incident in incidents]})
        fingerprint=digest({'scope':projects,'projects':semantic})
        actor_key=digest({'subject':actor['subject'],'role':actor['role'],'projects':actor['projects']})
        scope=digest(projects)
        old=c.execute('SELECT fingerprint FROM maintenance_digest_ack WHERE actor=? AND scope=?',(actor_key,scope)).fetchone()
        report={'projects':items,'fingerprint':fingerprint,'changed':old is None or old['fingerprint']!=fingerprint,
                'scope':projects,'live_ai_score':None,'live_ai_measured':False,
                'operational_deployment_verified':False,'notification_delivered':False}
        return report,actor_key,scope

    @staticmethod
    def _job_view(row):
        result=json.loads(row['result']) if row['result'] else None
        return {'id':row['id'],'task_id':row['task'],'state':row['state'],'action':row['action'],
                'counts':result.get('counts') if result else None,'independently_verified':False}

    def report(self,token,projects=None,*,max_age=300):
        if type(max_age) is not int or not 1<=max_age<=3600:raise ValueError('freshness bound required')
        with self.store._transaction() as c:
            return self._snapshot(c,token,projects,max_age)[0]

    def acknowledge(self,token,fingerprint,projects=None,*,max_age=300):
        _hash(fingerprint)
        if type(max_age) is not int or not 1<=max_age<=3600:raise ValueError('freshness bound required')
        with self.store._transaction() as c:
            report,actor,scope=self._snapshot(c,token,projects,max_age)
            if report['fingerprint']!=fingerprint:raise Conflict('report changed before acknowledgement')
            c.execute('INSERT OR REPLACE INTO maintenance_digest_ack VALUES (?,?,?,?)',(actor,scope,fingerprint,self.store._now()))
        return {'fingerprint':fingerprint,'acknowledged':True,'notification_delivered':False}
