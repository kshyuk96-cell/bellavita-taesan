from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from manager_bridge.agent_directory import AgentDirectory, AgentObservation, Connector, DeliveryReceipt
from manager_bridge.store import Store, Conflict, PermissionDenied, NotFound, token_hash, digest
from manager_bridge.coordination import Coordination


class AgentDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory());self.now=1000
        self.store=Store(Path(self.tmp)/'manager.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.store.register_project(self.owner,'a','fixture/a');self.store.register_project(self.owner,'b','fixture/b')
        self.manager=self.store.add_token('manager','manager',['a'])
        self.worker=self.store.add_token('worker','worker',['a'])
        self.worker2=self.store.add_token('worker2','worker',['a'])
        self.foreign=self.store.add_token('foreign','worker',['b'])
        self.worker_hash=token_hash(self.worker);self.calls=[];self.sequence=0
        self.observation=AgentObservation('a','agent','session-1',self.worker_hash,True,'fixture/probe',self.now,self.now+100)
        self.directory=AgentDirectory(self.store,{('a','agent'):Connector(self.observe,self.sender)})
        self.directory.register(self.manager,'a','agent',self.worker_hash,'session-1',enabled=True)
        self.directory.probe(self.manager,'a','agent')
        self.report={'head_sha':'b'*40,'summary':'fixture worker claim','checks':[{'name':'fixture','passed':True}]}

    def observe(self,*args):return self.observation

    def sender(self,envelope):
        self.calls.append(envelope)
        return DeliveryReceipt(envelope['id'],envelope['task_id'],envelope['version'],envelope['manifest_digest'],'fixture/notify')

    def task(self,approve=True,policy=False):
        self.sequence+=1
        task=self.store.submit(self.manager,f'task-{self.sequence}',{'project':'a','repository':'fixture/a','base_sha':'a'*40,'description':'synthetic work, material is not authority','kind':'test','checks':['fixture']})
        policy_digest=None
        if policy:
            value=Coordination(self.store).set_policy(self.owner,task['id'],task['version'],task['manifest_digest'],allowed_paths=['src/'],dependencies=[])
            policy_digest=digest(value)
        if approve:task=self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'],policy_digest=policy_digest)
        return task

    def enqueue(self,task=None,request=None):
        task=task or self.task()
        return self.directory.enqueue(self.manager,'a','agent',request or 'delivery-'+task['id'],task['id'],task['version'],task['manifest_digest'])

    def claimed(self):
        delivery=self.enqueue()
        claimed=self.store.claim(self.worker,'a',task_id=delivery['task_id'])
        self.assertIsNotNone(claimed)
        return delivery,claimed['lease_token']

    def test_registered_bearer_cannot_be_used_as_other_session_identifier(self):
        with self.assertRaises(ValueError):
            self.directory.register(self.manager,'a','bad',self.worker_hash,self.worker,enabled=True)
        task=self.task()
        with self.assertRaises(ValueError):self.enqueue(task,self.worker)

    def test_other_worker_bearer_cannot_be_persisted_as_delivery_receipt(self):
        self.directory.connectors[('a','agent')]=Connector(self.observe,
            lambda e:DeliveryReceipt(e['id'],e['task_id'],e['version'],e['manifest_digest'],'fixture/'+self.worker2))
        result=self.directory.send(self.manager,self.enqueue()['id'])
        self.assertEqual(result['state'],'uncertain')
        self.assertIsNone(result['reference'])
        self.assertNotIn(self.worker2,json.dumps(result))

    def test_registration_default_disabled(self):
        self.directory.register(self.manager,'a','off',self.worker_hash,'off-session')
        row=next(r for r in self.directory.list(self.manager,'a') if r['agent_id']=='off')
        self.assertEqual(row['state'],'disabled')
        self.assertFalse(row['existing_chat_control_verified'])

    def test_probe_missing_provider_not_connected(self):
        directory=AgentDirectory(self.store)
        result=directory.probe(self.manager,'a','agent')
        self.assertEqual(result['state'],'disconnected')

    def test_probe_freshness_expires(self):
        self.now+=101
        self.assertEqual(self.directory.list(self.manager,'a')[0]['state'],'stale')
        with self.assertRaises(Conflict):self.enqueue()

    def test_probe_wrong_worker_or_session_never_ready(self):
        for observation in (replace(self.observation,worker_hash=token_hash(self.worker2)),replace(self.observation,session_id='different'),replace(self.observation,project='b')):
            with self.subTest(observation=observation):
                self.observation=observation
                self.assertEqual(self.directory.probe(self.manager,'a','agent')['state'],'disconnected')

    def test_probe_wrong_reference_secret_not_saved(self):
        self.observation=replace(self.observation,reference=self.manager)
        self.assertEqual(self.directory.probe(self.manager,'a','agent')['state'],'disconnected')
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM manager_agent_observations').fetchone()[0],0)

    def test_probe_revoked_caller_cannot_publish_ready(self):
        def revoke(*_):
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return self.observation
        self.directory.connectors[('a','agent')]=Connector(revoke)
        with self.assertRaises(PermissionDenied):self.directory.probe(self.manager,'a','agent')

    def test_probe_registration_change_is_fenced(self):
        def change(*_):
            self.directory.register(self.owner,'a','agent',self.worker_hash,'session-1',enabled=False,expected_revision=1)
            return self.observation
        self.directory.connectors[('a','agent')]=Connector(change)
        with self.assertRaises(Conflict):self.directory.probe(self.manager,'a','agent')
        self.assertEqual(self.directory.list(self.manager,'a')[0]['state'],'disabled')

    def test_manager_cannot_register_foreign_or_broad_scope_worker(self):
        for worker in (self.foreign,self.store.add_token('broad','worker',['a','b'])):
            with self.subTest(worker_hash=token_hash(worker)),self.assertRaises(PermissionDenied):
                self.directory.register(self.manager,'a','foreign',token_hash(worker),'foreign-session')

    def test_only_own_project_visible(self):
        with self.assertRaises(NotFound):self.directory.list(self.manager,'b')
        with self.assertRaises(PermissionDenied):self.directory.list(self.worker,'a')

    def test_worker_revoked_or_expired_is_disabled(self):
        self.store.revoke_token(self.owner,self.worker_hash)
        self.assertEqual(self.directory.list(self.manager,'a')[0]['state'],'disabled')
        with self.assertRaises(PermissionDenied):self.enqueue()

    def test_registration_revision_and_identity_immutable(self):
        with self.assertRaises(Conflict):self.directory.register(self.manager,'a','agent',self.worker_hash,'session-1',enabled=True)
        with self.assertRaises(Conflict):self.directory.register(self.manager,'a','agent',token_hash(self.worker2),'session-2',enabled=True,expected_revision=1)

    def test_only_approved_exact_task_can_enter_inbox(self):
        task=self.task(approve=False)
        with self.assertRaises(PermissionDenied):self.enqueue(task)
        task=self.task()
        with self.assertRaises(Conflict):self.directory.enqueue(self.manager,'a','agent','bad-version',task['id'],2,task['manifest_digest'])

    def test_enqueue_does_not_claim_execute_or_verify(self):
        delivery=self.enqueue()
        task=self.store.get_task(self.manager,delivery['task_id'])
        self.assertEqual(delivery['state'],'pending')
        self.assertEqual(task['state'],'queued')
        self.assertFalse(delivery['execution_verified'])
        self.assertFalse(delivery['independently_verified'])
        self.assertEqual(self.calls,[])

    def test_duplicate_enqueue_same_content_is_idempotent(self):
        task=self.task()
        first=self.enqueue(task,'same');second=self.enqueue(task,'same')
        self.assertEqual(first,second)
        with self.assertRaises(Conflict):self.enqueue(self.task(),'same')

    def test_policy_route_is_exact_registered_worker(self):
        task=self.task(policy=True)
        self.enqueue(task)
        with self.store._transaction() as c:
            route=c.execute('SELECT worker_hash FROM bridge_task_route WHERE task=?',(task['id'],)).fetchone()
            self.assertEqual(route['worker_hash'],self.worker_hash)
        self.assertIsNotNone(self.store.claim(self.worker,'a',task_id=task['id']))

    def test_task_assigned_other_worker_not_delivered(self):
        task=self.task()
        self.store.claim(self.worker2,'a',task_id=task['id'])
        with self.assertRaises(Conflict):self.enqueue(task)

    def test_worker_poll_requires_exact_registered_identity(self):
        delivery=self.enqueue()
        rows=self.directory.poll(self.worker,'a','agent')
        self.assertEqual(rows[0]['id'],delivery['id'])
        self.assertTrue(rows[0]['claim_required'])
        self.assertFalse(rows[0]['delivery_is_execution_approval'])
        with self.assertRaises(PermissionDenied):self.directory.poll(self.worker2,'a','agent')
        with self.assertRaises(NotFound):self.directory.poll(self.foreign,'a','agent')

    def test_cancelled_task_not_returned_to_worker(self):
        delivery=self.enqueue()
        self.store.cancel(self.manager,delivery['task_id'])
        self.assertEqual(self.directory.poll(self.worker,'a','agent'),[])

    def test_sender_receipt_is_notification_only(self):
        delivery=self.enqueue()
        result=self.directory.send(self.manager,delivery['id'])
        self.assertEqual(result['state'],'notified')
        self.assertFalse(result['execution_verified'])
        self.assertEqual(self.directory.send(self.manager,delivery['id']),result)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.store.get_task(self.manager,delivery['task_id'])['state'],'queued')

    def test_sender_failure_uncertain_no_retry_even_after_restart(self):
        def failed(envelope):self.calls.append(envelope);raise TimeoutError('private service body')
        self.directory.connectors[('a','agent')]=Connector(self.observe,failed)
        delivery=self.enqueue();result=self.directory.send(self.manager,delivery['id'])
        self.assertEqual(result['state'],'uncertain')
        self.assertNotIn('private',json.dumps(result))
        reopened=AgentDirectory(self.store,self.directory.connectors)
        self.assertEqual(reopened.send(self.manager,delivery['id'])['state'],'uncertain')
        self.assertEqual(len(self.calls),1)

    def test_sender_wrong_receipt_uncertain(self):
        self.directory.connectors[('a','agent')]=Connector(self.observe,lambda e:DeliveryReceipt('a'*64,e['task_id'],e['version'],e['manifest_digest'],'fixture/wrong'))
        self.assertEqual(self.directory.send(self.manager,self.enqueue()['id'])['state'],'uncertain')

    def test_concurrent_send_invokes_host_once(self):
        delivery=self.enqueue();entered=threading.Event();finish=threading.Event()
        original=self.sender
        def waiting(envelope):entered.set();finish.wait(3);return original(envelope)
        self.directory.connectors[('a','agent')]=Connector(self.observe,waiting)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.directory.send,self.manager,delivery['id']);self.assertTrue(entered.wait(3))
            second=self.directory.send(self.manager,delivery['id']);finish.set();one=first.result()
        self.assertEqual(second['state'],'sending');self.assertEqual(one['state'],'notified');self.assertEqual(len(self.calls),1)

    def test_disabled_agent_cannot_ack(self):
        delivery,lease=self.claimed()
        self.directory.register(self.manager,'a','agent',self.worker_hash,'session-1',enabled=False,expected_revision=1)
        with self.assertRaises(Conflict):self.directory.acknowledge(self.worker,delivery['id'],lease)

    def test_ack_requires_claim_exact_worker_and_lease(self):
        delivery=self.enqueue()
        with self.assertRaises(Conflict):self.directory.acknowledge(self.worker,delivery['id'],'a'*64)
        task=self.store.claim(self.worker,'a',task_id=delivery['task_id'])
        with self.assertRaises(PermissionDenied):self.directory.acknowledge(self.worker2,delivery['id'],task['lease_token'])
        with self.assertRaises(Conflict):self.directory.acknowledge(self.worker,delivery['id'],'a'*64)
        result=self.directory.acknowledge(self.worker,delivery['id'],task['lease_token'])
        self.assertEqual(result['state'],'acknowledged')
        self.assertFalse(result['execution_verified'])
        self.assertEqual(self.directory.acknowledge(self.worker,delivery['id'],task['lease_token']),result)

    def test_ack_expired_lease_cannot_receive(self):
        delivery,lease=self.claimed();self.now+=301
        with self.assertRaises(Conflict):self.directory.acknowledge(self.worker,delivery['id'],lease)

    def test_manager_cannot_forge_worker_ack_or_report(self):
        delivery,lease=self.claimed()
        with self.assertRaises(PermissionDenied):self.directory.acknowledge(self.manager,delivery['id'],lease)
        with self.assertRaises(PermissionDenied):self.directory.report(self.manager,delivery['id'],lease,self.report)

    def test_report_requires_prior_ack(self):
        delivery,lease=self.claimed()
        with self.assertRaises(PermissionDenied):self.directory.report(self.worker,delivery['id'],lease,self.report)

    def test_report_is_worker_claim_and_duplicate_exact_only(self):
        delivery,lease=self.claimed();self.directory.acknowledge(self.worker,delivery['id'],lease)
        result=self.directory.report(self.worker,delivery['id'],lease,self.report)
        self.assertEqual(result['state'],'reported');self.assertFalse(result['independently_verified'])
        self.assertEqual(self.directory.report(self.worker,delivery['id'],lease,self.report),result)
        changed={**self.report,'summary':'different'}
        with self.assertRaises(Conflict):self.directory.report(self.worker,delivery['id'],lease,changed)

    def test_report_lost_inbox_receipt_reconciles_store_exact_result(self):
        delivery,lease=self.claimed();self.directory.acknowledge(self.worker,delivery['id'],lease)
        self.store.report(self.worker,delivery['task_id'],lease,self.report)
        self.assertEqual(self.directory.report(self.worker,delivery['id'],lease,self.report)['state'],'reported')

    def test_manager_can_inspect_and_stop_after_worker_revocation(self):
        delivery,lease=self.claimed()
        self.store.revoke_token(self.owner,self.worker_hash)
        self.assertIn(self.directory.get(self.manager,delivery['id'])['task_state'],('running','needs_verification'))
        result=self.directory.control(self.manager,delivery['id'],action='cancel')
        self.assertFalse(result['execution_stop_confirmed'])
        self.assertEqual(self.store.get_task(self.manager,delivery['task_id'])['state'],'cancelled')

    def test_manager_can_stop_old_instruction_after_agent_disabled(self):
        delivery,lease=self.claimed()
        self.directory.register(self.manager,'a','agent',self.worker_hash,'session-1',enabled=False,expected_revision=1)
        result=self.directory.control(self.manager,delivery['id'],action='pause')
        self.assertFalse(result['execution_stop_confirmed'])
        self.assertEqual(self.directory.get(self.manager,delivery['id'])['state'],'cancelled')

    def test_control_cancel_before_start_no_execution(self):
        delivery=self.enqueue();result=self.directory.control(self.manager,delivery['id'],action='cancel')
        self.assertTrue(result['execution_stop_confirmed']);self.assertFalse(result['rollback_performed'])
        self.assertIsNone(self.store.claim(self.worker,'a',task_id=delivery['task_id']))

    def test_pause_running_is_stop_request_not_confirmed_rollback(self):
        delivery,lease=self.claimed();result=self.directory.control(self.manager,delivery['id'],action='pause')
        self.assertFalse(result['execution_stop_confirmed']);self.assertFalse(result['automatic_resume'])
        with self.assertRaises(Conflict):self.directory.acknowledge(self.worker,delivery['id'],lease)

    def test_change_creates_new_pending_scope_no_approval_copy(self):
        delivery=self.enqueue();task=self.store.get_task(self.manager,delivery['task_id'])
        manifest={**task['manifest'],'description':'changed synthetic requirement'}
        result=self.directory.control(self.manager,delivery['id'],action='change',request_key='change-1',manifest=manifest,reason='scope update')
        self.assertEqual(result['successor']['state'],'pending_approval')
        self.assertIsNone(result['successor']['approval'])
        self.assertFalse(result['automatic_resume'])
        self.assertEqual(self.store.get_task(self.manager,delivery['task_id'])['state'],'cancelled')

    def test_other_worker_cannot_control_and_credentials_not_saved(self):
        delivery,lease=self.claimed()
        with self.assertRaises(PermissionDenied):self.directory.control(self.worker,delivery['id'],action='cancel')
        with self.assertRaises(ValueError):self.directory.enqueue(self.manager,'a','agent',self.manager,delivery['task_id'],delivery['version'],delivery['manifest_digest'])
        with self.store._transaction() as c:
            content=' '.join(str(tuple(row)) for row in c.execute('SELECT * FROM manager_agent_inbox'))
        for secret in (self.manager,self.worker,self.owner,lease):self.assertNotIn(secret,content)
