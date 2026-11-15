import tempfile
import unittest
from pathlib import Path

from manager_bridge.store import Store,PermissionDenied,NotFound
from manager_bridge.operations import Operations


class OperationTests(unittest.TestCase):
    def setUp(self):
        root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.now=1000
        self.store=Store(root/'state.db',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        for project in ('a','b'):self.store.register_project(self.owner,project,'fixture/'+project)
        self.manager=self.store.add_token('manager','manager',['a'])
        self.worker=self.store.add_token('worker','worker',['a'])
        self.other=self.store.add_token('other','worker',['b'])
        self.ops=Operations(self.store)

    def task(self,project='a'):
        task=self.store.submit(self.owner,'one',{'project':project,'repository':'fixture/'+project,'base_sha':'a'*40,
            'description':'fixture','checks':['fixture'],'kind':'test'})
        self.now+=10;self.store.approve(self.owner,task['id'],1,task['manifest_digest'])
        return task

    def test_timings_are_observed_wall_intervals_and_do_not_invent_cpu_or_prices(self):
        task=self.task();self.now+=20;claim=self.store.claim(self.worker,'a');self.now+=30
        self.store.report(self.worker,task['id'],claim['lease_token'],{'head_sha':'a'*40,'summary':'fixture','checks':[{'name':'fixture','passed':True}]})
        self.now+=40;self.store.review(self.manager,task['id'],1,task['manifest_digest'],'fixture:independent')
        result=self.ops.timings(self.manager,task['id'])
        self.assertEqual(result['approval_wait_seconds'],10);self.assertEqual(result['queue_wait_seconds'],20)
        self.assertEqual(result['observed_execution_wall_seconds'],30);self.assertEqual(result['review_wait_seconds'],40)
        self.assertFalse(result['cpu_or_model_usage_measured']);self.assertFalse(result['billing_estimate_provided'])
        with self.assertRaises(PermissionDenied):self.ops.timings(self.worker,task['id'])

    def test_global_review_backpressure_blocks_other_project_until_review(self):
        first=self.task('a');second=self.task('b')
        self.ops.set_capacity(self.owner,None,max_active=2,max_review=1)
        claim=self.store.claim(self.worker,'a')
        self.store.report(self.worker,first['id'],claim['lease_token'],{'head_sha':'a'*40,'summary':'fixture','checks':[{'name':'fixture','passed':True}]})
        self.assertIsNone(self.store.claim(self.other,'b'))
        self.store.review(self.owner,first['id'],1,first['manifest_digest'],'fixture:reviewed')
        self.assertEqual(self.store.claim(self.other,'b')['id'],second['id'])

    def test_unconfirmed_cancel_occupies_global_capacity_and_does_not_interrupt(self):
        first=self.task('a');self.task('b')
        self.ops.set_capacity(self.owner,None,max_active=1,max_review=10)
        self.store.claim(self.worker,'a');self.store.cancel(self.owner,first['id'])
        self.assertIsNone(self.store.claim(self.other,'b'))
        self.assertTrue(self.ops.timings(self.manager,first['id'])['unknown_execution_end'])
        from manager_bridge.coordination import Coordination
        Coordination(self.store).confirm_stopped(self.owner,first['id'],1,first['manifest_digest'],'fixture:process-exit')
        self.assertIsNotNone(self.store.claim(self.other,'b'))
        self.assertFalse(self.ops.timings(self.manager,first['id'])['unknown_execution_end'])

    def test_project_manager_cannot_set_global_limit_or_other_project(self):
        with self.assertRaises(PermissionDenied):self.ops.set_capacity(self.manager,None,max_active=1,max_review=1)
        with self.assertRaises(NotFound):self.ops.set_capacity(self.manager,'b',max_active=1,max_review=1)
        with self.assertRaises(ValueError):self.ops.set_capacity(self.owner,None,max_active=True,max_review=1)


if __name__=='__main__':unittest.main()
