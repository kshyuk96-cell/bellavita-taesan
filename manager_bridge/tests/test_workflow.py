import tempfile
import unittest
from pathlib import Path

from manager_bridge.workflow import Workflow
from manager_bridge.store import Store,Conflict,PermissionDenied


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.now=1000
        self.store=Store(self.root/'state.db',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*']);self.store.register_project(self.owner,'a','fixture/a')
        self.manager=self.store.add_token('manager','manager',['a']);self.worker=self.store.add_token('worker','worker',['a'])
        self.workflow=Workflow(self.store)
        self.manifest={'project':'a','repository':'fixture/a','base_sha':'a'*40,'description':'fixture','checks':['fixture'],'kind':'test'}

    def approved(self,key):
        task=self.store.submit(self.manager,key,self.manifest)
        return self.store.approve(self.owner,task['id'],1,task['manifest_digest'])

    def test_priority_then_deadline_orders_queue_without_preempting_running_work(self):
        normal=self.approved('normal');urgent=self.approved('urgent');early=self.approved('early')
        self.workflow.plan(self.manager,urgent['id'],priority=2,due_at=2000)
        self.workflow.plan(self.manager,early['id'],priority=2,due_at=1500)
        claim=self.store.claim(self.worker,'a');self.assertEqual(claim['id'],early['id'])
        self.workflow.plan(self.manager,normal['id'],priority=2,due_at=1100)
        self.assertIsNone(self.store.claim(self.worker,'a'))
        self.assertEqual(self.store.get_task(self.manager,early['id'])['state'],'running')

    def test_mid_execution_change_requires_fresh_approval_and_old_stop_confirmation(self):
        source=self.approved('source');self.store.claim(self.worker,'a')
        changed=self.workflow.propose_change(self.manager,source['id'],request_key='change-1',
            manifest=self.manifest|{'description':'new fixture scope'},reason='fixture steering')
        self.assertEqual(changed['state'],'pending_approval');self.assertIsNone(changed['approval'])
        self.assertEqual(self.store.get_task(self.manager,source['id'])['state'],'cancelled')
        self.assertEqual(self.workflow.propose_change(self.manager,source['id'],request_key='change-1',
            manifest=self.manifest|{'description':'new fixture scope'},reason='fixture steering')['id'],changed['id'])
        self.store.approve(self.owner,changed['id'],1,changed['manifest_digest'])
        self.assertIsNone(self.store.claim(self.worker,'a'))
        from manager_bridge.coordination import Coordination
        Coordination(self.store).confirm_stopped(self.manager,source['id'],1,source['manifest_digest'],'fixture:independent-stop')
        self.assertEqual(self.store.claim(self.worker,'a')['id'],changed['id'])

    def test_reviewed_candidate_stays_frozen_when_followup_is_proposed(self):
        source=self.approved('source');claim=self.store.claim(self.worker,'a')
        report={'head_sha':'a'*40,'summary':'fixture','checks':[{'name':'fixture','passed':True}]}
        self.store.report(self.worker,source['id'],claim['lease_token'],report)
        self.store.review(self.manager,source['id'],1,source['manifest_digest'],'fixture:checked')
        followup=self.workflow.propose_change(self.manager,source['id'],request_key='followup',manifest=self.manifest,reason='next review')
        self.assertEqual(self.store.get_task(self.manager,source['id'])['state'],'reviewed')
        self.assertEqual(followup['state'],'pending_approval')

    def test_digest_is_previewed_then_exactly_acknowledged_and_suppresses_heartbeat_noise(self):
        task=self.approved('one');claim=self.store.claim(self.worker,'a')
        first=self.workflow.preview_digest(self.manager,'a');self.assertTrue(first['notify'])
        self.workflow.acknowledge_digest(self.manager,'a',first['fingerprint'])
        self.now+=1;self.store.heartbeat(self.worker,task['id'],claim['lease_token'],progress='fixture heartbeat')
        self.assertFalse(self.workflow.preview_digest(self.manager,'a')['notify'])
        self.workflow.plan(self.manager,task['id'],priority=2,due_at=1010)
        changed=self.workflow.preview_digest(self.manager,'a');self.assertTrue(changed['notify'])
        with self.assertRaises(Conflict):self.workflow.acknowledge_digest(self.manager,'a',first['fingerprint'])
        self.workflow.acknowledge_digest(self.manager,'a',changed['fingerprint'])
        self.now=1011;late=self.workflow.preview_digest(self.manager,'a')
        self.assertTrue(late['notify']);self.assertTrue(late['changed'][0]['overdue'])
        self.assertFalse(late['external_message_sent'])

    def test_same_parent_web_session_reuses_receipt_without_repeating_report(self):
        self.approved('one')
        first=self.workflow.preview_digest(self.manager,'a')
        self.workflow.acknowledge_digest(self.manager,'a',first['fingerprint'])
        from manager_bridge.sessions import Sessions
        sessions=Sessions(self.store)
        web=sessions.redeem(sessions.issue(self.manager)['code'],'fixture')['token']
        self.assertFalse(self.workflow.preview_digest(web,'a')['notify'])

    def test_revised_review_card_requires_new_report_even_if_task_state_is_unchanged(self):
        from manager_bridge.workbench import Workbench
        from manager_bridge.store import digest
        task=self.approved('one');claim=self.store.claim(self.worker,'a')
        report={'head_sha':'a'*40,'summary':'fixture','checks':[{'name':'fixture','passed':True}]}
        self.store.report(self.worker,task['id'],claim['lease_token'],report)
        self.store.review(self.manager,task['id'],1,task['manifest_digest'],'fixture:checked')
        with self.store._transaction() as c:
            c.execute('INSERT INTO bridge_verified_candidates VALUES (?,?,?,?,?,?)',
                      (task['id'],digest(report),'a'*40,'f'*64,0,self.now))
        workbench=Workbench(self.store)
        package={'summary':'fixture','impact':'fixture','risks':'first','rollback_sha':'a'*40,
            'candidate_sha':'a'*40,'pr_url':'https://github.com/fixture/a/pull/1','evidence_references':['fixture:checked']}
        workbench.put_package(self.manager,task['id'],package)
        first=self.workflow.preview_digest(self.manager,'a')
        self.workflow.acknowledge_digest(self.manager,'a',first['fingerprint'])
        workbench.put_package(self.manager,task['id'],package|{'risks':'changed'})
        revised=self.workflow.preview_digest(self.manager,'a')
        self.assertTrue(revised['notify']);self.assertEqual(revised['changed'][0]['review_card']['revision'],2)

    def test_workflow_role_scope_and_validation_are_enforced(self):
        task=self.approved('one')
        with self.assertRaises(PermissionDenied):self.workflow.plan(self.worker,task['id'],priority=2,due_at=None)
        with self.assertRaises(ValueError):self.workflow.plan(self.manager,task['id'],priority=True,due_at=None)
        with self.assertRaises(ValueError):self.workflow.plan(self.manager,task['id'],priority=0,due_at=float('nan'))
        with self.assertRaises(Conflict):self.workflow.propose_change(self.manager,task['id'],request_key='bad',
            manifest=self.manifest|{'repository':'fixture/other'},reason='fixture')


if __name__=='__main__':unittest.main()
