"""Opt-in consumer for one registered agent inbox; no daemon or CLI startup.

The execution callback is trusted host configuration and must provide actual
sandbox isolation for repository code. It receives a public assignment and a
heartbeat function, never a bearer credential or lease. Callback results remain
worker claims until the manager independently verifies them.
"""
import json
import threading
from .maintenance_registry import name
from .store import Conflict, PermissionDenied, canonical_json


class RepresentativeWorker:
    def __init__(self,store,directory,*,execute=None,lease_seconds=300):
        if directory.store is not store:
            raise ValueError('shared registered agent store required')
        if execute is not None and not callable(execute):
            raise ValueError('trusted sandbox callback required')
        if type(lease_seconds) is not int or not 1<=lease_seconds<=3600:
            raise ValueError('bounded execution lease required')
        self.store,self.directory,self.execute=store,directory,execute
        self.lease_seconds=lease_seconds

    def _scope(self,c,token,project,agent_id):
        return self.directory._agent(c,token,project,agent_id,('worker',))

    def _active(self,c,token,project,agent_id):
        actor,_=self._scope(c,token,project,agent_id)
        row=c.execute('''SELECT i.task_id,t.state FROM manager_agent_inbox i
            JOIN bridge_tasks t ON t.id=i.task_id WHERE i.project=? AND i.agent_id=?
            AND i.state IN ('acknowledged','cancelled') AND t.worker_token_hash=?
            AND (t.state IN ('running','needs_verification') OR (t.state='cancelled'
            AND NOT EXISTS (SELECT 1 FROM bridge_stop_confirmations s WHERE s.task=t.id)))
            ORDER BY i.created,i.id LIMIT 1''',(project,agent_id,actor['token_hash'])).fetchone()
        return row

    def _fence(self,token,project,agent_id,task_id,lease):
        with self.store._transaction() as c:
            actor,agent=self._scope(c,token,project,agent_id)
            if not agent['enabled']:raise PermissionDenied('agent disabled')
            row=self.store._get(c,actor,task_id)
            self.store._lease(row,actor,lease,c=c)
            if row['state']!='running' or row['lease_expires']<=self.store._now():
                raise Conflict('execution stopped or expired')

    def _uncertain(self,task_id):
        # Internal receipt reconciliation after an already-authorized claim. No
        # exception string or callback payload is retained, and no work is replayed.
        with self.store._transaction() as c:
            c.execute("UPDATE bridge_tasks SET state='needs_verification',updated=? WHERE id=? AND state='running'",
                      (self.store._now(),task_id))

    def run_once(self,worker_token,project,agent_id):
        name(project);name(agent_id)
        with self.store._transaction() as c:
            self._scope(c,worker_token,project,agent_id)
            active=self._active(c,worker_token,project,agent_id)
            if active:
                return {'project':project,'state':'needs_verification','task_id':active['task_id'],
                        'execution_started':False,'automatically_replayed':False}
        if self.execute is None:
            return {'project':project,'state':'not_configured','execution_started':False}
        deliveries=self.directory.poll(worker_token,project,agent_id,limit=1)
        if not deliveries:
            return {'project':project,'state':'idle','execution_started':False}
        delivery=deliveries[0]
        if not delivery['claim_required']:
            return {'project':project,'state':'needs_verification','task_id':delivery['task_id'],
                    'execution_started':False,'automatically_replayed':False}
        assignment=self.store.claim(worker_token,project,task_id=delivery['task_id'],lease_seconds=self.lease_seconds)
        if assignment is None:
            return {'project':project,'state':'blocked','task_id':delivery['task_id'],'execution_started':False}
        task_id=assignment['id'];lease=assignment['lease_token']
        try:
            self.directory.acknowledge(worker_token,delivery['id'],lease)
            self._fence(worker_token,project,agent_id,task_id,lease)

            def heartbeat():
                self._fence(worker_token,project,agent_id,task_id,lease)
                pulse=self.store.heartbeat(worker_token,task_id,lease,lease_seconds=self.lease_seconds)
                if pulse.get('cancel_requested') is not False:
                    raise Conflict('execution stop requested')
                return {'state':pulse['state'],'cancel_requested':False,
                        'lease_expires_at':pulse['lease_expires_at']}

            public={key:value for key,value in assignment.items() if key!='lease_token'}
            public=json.loads(canonical_json(public))
            report=self.execute(public,heartbeat)
            self._fence(worker_token,project,agent_id,task_id,lease)
            outcome=self.directory.report(worker_token,delivery['id'],lease,report)
            return {'project':project,'state':'reported','task_id':task_id,'delivery_id':delivery['id'],
                    'task_state':outcome['task_state'],'execution_started':True,
                    'independently_verified':False,'operational_change':False}
        except Exception:
            self._uncertain(task_id)
            return {'project':project,'state':'needs_verification','task_id':task_id,
                    'execution_started':True,'automatically_replayed':False,
                    'independently_verified':False,'operational_change':False}

    def run_loop(self,worker_token,project,agent_id,*,max_cycles=1,stop_event=None):
        if type(max_cycles) is not int or not 1<=max_cycles<=100:
            raise ValueError('bounded run count required')
        if stop_event is not None and not isinstance(stop_event,threading.Event):
            raise ValueError('host stop event required')
        results=[]
        for _ in range(max_cycles):
            if stop_event is not None and stop_event.is_set():break
            result=self.run_once(worker_token,project,agent_id);results.append(result)
            if result['state']!='reported':break
        return {'cycles':len(results),'results':results,'background_process_started':False,
                'schedule_created':False,'stop_requested':bool(stop_event and stop_event.is_set())}
