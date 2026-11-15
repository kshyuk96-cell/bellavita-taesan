from pathlib import Path
import json
import tempfile
import threading
import unittest
from manager_bridge.agent_directory import AgentDirectory,AgentObservation,Connector
from manager_bridge.representative_worker import RepresentativeWorker
from manager_bridge.store import Store,PermissionDenied,NotFound,token_hash


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory());self.now=1000
        self.store=Store(Path(self.tmp)/'queue.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.store.register_project(self.owner,'a','fixture/a');self.store.register_project(self.owner,'b','fixture/b')
        self.manager=self.store.add_token('manager','manager',['a'])
        self.worker=self.store.add_token('worker','worker',['a'])
        self.other=self.store.add_token('other','worker',['b'])
        self.directory=AgentDirectory(self.store,{('a','agent'):Connector(lambda *_:
            AgentObservation('a','agent','session',token_hash(self.worker),True,'fixture/agent',self.now,self.now+100))})
        self.directory.register(self.manager,'a','agent',token_hash(self.worker),'session',enabled=True)
        self.directory.probe(self.manager,'a','agent');self.calls=[];self.seq=0

    def enqueue(self):
        self.seq+=1
        task=self.store.submit(self.manager,'task-'+str(self.seq),{'project':'a','repository':'fixture/a','base_sha':'a'*40,
            'description':'synthetic test only','checks':['fixture'],'kind':'test'})
        task=self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'])
        delivery=self.directory.enqueue(self.manager,'a','agent','delivery-'+str(self.seq),task['id'],task['version'],task['manifest_digest'])
        return task,delivery

    def execute(self,assignment,heartbeat):
        self.calls.append(assignment)
        self.assertNotIn('lease_token',assignment)
        self.assertNotIn(self.worker,json.dumps(assignment))
        self.assertFalse(heartbeat()['cancel_requested'])
        return {'head_sha':'a'*40,'summary':'synthetic executor result','checks':[{'name':'fixture','passed':True}]}

    def runner(self,execute=None):
        return RepresentativeWorker(self.store,self.directory,execute=execute or self.execute)

    def test_disconnected_executor_never_claims(self):
        task,_=self.enqueue()
        result=RepresentativeWorker(self.store,self.directory).run_once(self.worker,'a','agent')
        self.assertEqual(result['state'],'not_configured')
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'queued')

    def test_exact_execution_reports_without_independent_verification(self):
        task,delivery=self.enqueue()
        result=self.runner().run_once(self.worker,'a','agent')
        self.assertEqual(result['state'],'reported')
        self.assertIs(result['independently_verified'],False)
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'reported')
        self.assertEqual(self.directory.get(self.manager,delivery['id'])['state'],'reported')
        self.assertEqual(len(self.calls),1)

    def test_no_executor_called_with_foreign_or_manager_credential(self):
        self.enqueue()
        for token in (self.manager,self.other):
            with self.subTest(token=token[:3]),self.assertRaises((PermissionDenied,NotFound)):
                self.runner().run_once(token,'a','agent')
        self.assertEqual(self.calls,[])

    def test_started_exception_reserves_task_and_does_not_replay(self):
        task,_=self.enqueue()
        calls=[]
        def fail(*_):calls.append(True);raise RuntimeError('private callback content')
        first=self.runner(fail).run_once(self.worker,'a','agent')
        second=self.runner(fail).run_once(self.worker,'a','agent')
        self.assertEqual(first['state'],'needs_verification')
        self.assertEqual(second['state'],'needs_verification')
        self.assertEqual(calls,[True])
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'needs_verification')
        self.assertNotIn('private',json.dumps(first))

    def test_restart_acknowledged_running_task_not_executed(self):
        task,delivery=self.enqueue()
        claimed=self.store.claim(self.worker,'a',task_id=task['id'])
        self.directory.acknowledge(self.worker,delivery['id'],claimed['lease_token'])
        result=self.runner().run_once(self.worker,'a','agent')
        self.assertEqual(result['state'],'needs_verification')
        self.assertEqual(self.calls,[])

    def test_mid_execution_cancel_heartbeat_stops_report(self):
        task,delivery=self.enqueue()
        def cancelled(assignment,heartbeat):
            self.directory.control(self.manager,delivery['id'],action='cancel')
            heartbeat()
            self.fail('heartbeat allowed cancelled task')
        result=self.runner(cancelled).run_once(self.worker,'a','agent')
        self.assertEqual(result['state'],'needs_verification')
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'cancelled')

    def test_cancelled_running_task_stays_uncertain_until_stop_proof(self):
        task,delivery=self.enqueue()
        claimed=self.store.claim(self.worker,'a',task_id=task['id'])
        self.directory.acknowledge(self.worker,delivery['id'],claimed['lease_token'])
        self.directory.control(self.manager,delivery['id'],action='cancel')
        result=self.runner().run_once(self.worker,'a','agent')
        self.assertEqual(result['state'],'needs_verification')
        self.assertEqual(self.calls,[])

    def test_lease_expiry_before_completion_does_not_report(self):
        task,_=self.enqueue()
        def expire(assignment,heartbeat):
            self.now+=301
            return {'head_sha':'a'*40,'summary':'late','checks':[{'name':'fixture','passed':True}]}
        result=self.runner(expire).run_once(self.worker,'a','agent')
        self.assertEqual(result['state'],'needs_verification')
        self.assertIsNone(self.store.get_task(self.manager,task['id'])['report'])

    def test_revocation_during_execution_does_not_report(self):
        self.enqueue()
        def revoke(assignment,heartbeat):
            self.store.revoke_token(self.owner,token_hash(self.worker))
            heartbeat()
        result=self.runner(revoke).run_once(self.worker,'a','agent')
        self.assertEqual(result['state'],'needs_verification')
        self.assertIs(result['automatically_replayed'],False)

    def test_success_then_second_run_does_not_replay(self):
        self.enqueue()
        runner=self.runner();self.assertEqual(runner.run_once(self.worker,'a','agent')['state'],'reported')
        self.assertEqual(runner.run_once(self.worker,'a','agent')['state'],'idle')
        self.assertEqual(len(self.calls),1)

    def test_stopped_loop_and_invalid_bounds(self):
        task,_=self.enqueue();stop=threading.Event();stop.set()
        result=self.runner().run_loop(self.worker,'a','agent',max_cycles=2,stop_event=stop)
        self.assertEqual(result['cycles'],0)
        self.assertFalse(result['schedule_created'])
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'queued')
        with self.assertRaises(ValueError):self.runner().run_loop(self.worker,'a','agent',max_cycles=0)
