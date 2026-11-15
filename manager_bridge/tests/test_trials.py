import tempfile
import unittest
from pathlib import Path

from manager_bridge.coordination import Coordination
from manager_bridge.store import Store, Conflict, PermissionDenied
from manager_bridge.trials import TrialRunner, Observer


class TrialTests(unittest.TestCase):
    def setUp(self):
        root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store=Store(root/'manager.sqlite3',clock=lambda:1000)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.store.register_project(self.owner,'a','fixture/a')
        self.manager=self.store.add_token('manager','manager',['a'])
        self.worker=self.store.add_token('worker','worker',['a'])
        task=self.store.submit(self.manager,'one',{'project':'a','repository':'fixture/a','base_sha':'a'*40,
            'description':'fixture trial only','checks':['fixture'],'kind':'test'})
        self.store.approve(self.owner,task['id'],1,task['manifest_digest'])
        claim=self.store.claim(self.worker,'a')
        self.store.report(self.worker,task['id'],claim['lease_token'],{'head_sha':'a'*40,
            'summary':'fixture','checks':[{'name':'fixture','passed':True}]})
        self.task=self.store.review(self.manager,task['id'],1,task['manifest_digest'],'fixture:independent')

    def observer(self,actual=None,candidate=None):
        def observe(case,task):
            return {'task_id':task['id'],'candidate_sha':candidate or task['manifest']['base_sha'],
                    'receipt':'fixture:instrumentation','actual':dict(case.expected) if actual is None else actual}
        return observe

    def test_unconnected_business_case_does_not_create_real_score(self):
        result=TrialRunner(self.store).run(self.manager,self.task['id'],'photo_status')
        self.assertFalse(result['score_recorded'])
        score=Coordination(self.store).scorecard(self.manager,'a')
        self.assertIsNone(score['real']['dimensions']['understanding']['sample_score'])

    def test_synthetic_observer_never_promotes_to_real(self):
        runner=TrialRunner(self.store,{'kakao_dev':Observer('synthetic','isolated_copy',self.observer())})
        result=runner.run(self.manager,self.task['id'],'photo_status')
        self.assertTrue(result['passed'])
        self.assertEqual(result['source'],'synthetic')
        score=Coordination(self.store).scorecard(self.manager,'a')
        self.assertIsNone(score['real']['dimensions']['understanding']['sample_score'])
        self.assertEqual(score['synthetic']['dimensions']['understanding']['sample_score'],100)

    def test_boolean_type_and_critical_permission_failure_are_not_hidden(self):
        runner=TrialRunner(self.store,{'kakao_dev':Observer('synthetic','isolated_copy',
            self.observer({'write_executed':0,'permission_denied':True}))})
        result=runner.run(self.manager,self.task['id'],'permission_stock')
        self.assertFalse(result['passed'])
        score=Coordination(self.store).scorecard(self.manager,'a')
        self.assertEqual(score['synthetic']['critical_failures'],1)

    def test_wrong_commit_and_worker_cannot_record_trial(self):
        runner=TrialRunner(self.store,{'kakao_dev':Observer('synthetic','isolated_copy',self.observer(candidate='b'*40))})
        with self.assertRaises(ValueError):runner.run(self.manager,self.task['id'],'photo_status')
        with self.assertRaises(PermissionDenied):runner.run(self.worker,self.task['id'],'photo_status')

    def test_production_observer_configuration_is_rejected(self):
        with self.assertRaises(ValueError):Observer('real','production',self.observer())


if __name__=='__main__':unittest.main()
