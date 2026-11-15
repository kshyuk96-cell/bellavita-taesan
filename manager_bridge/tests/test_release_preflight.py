import tempfile
import unittest
from pathlib import Path
from manager_bridge.store import Store,digest,PermissionDenied
from manager_bridge.release_preflight import ReleasePreflight


class PreflightTests(unittest.TestCase):
    def setUp(self):
        root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.now=1000
        self.store=Store(root/'state.db',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*']);self.store.register_project(self.owner,'a','fixture/a')
        self.manager=self.store.add_token('manager','manager',['a']);self.worker=self.store.add_token('worker','worker',['a'])
        self.task=self.store.submit(self.manager,'one',{'project':'a','repository':'fixture/a','base_sha':'b'*40,'description':'fixture','checks':['fixture'],'kind':'test'})
        self.repo=dict(repository='fixture/a',main_sha='a'*40,candidate_sha='b'*40,rollback_sha='a'*40,
            candidate_contains_main=True,rollback_exists=True,receipt='fixture:git',observed_at=1000)
        self.live=dict(service_id='srv-fixture',repository='fixture/a',branch='main',live_sha='a'*40,status='live',deploy_id='dep-fixture',
            health_ready=True,auto_deploy=True,disk_attached=True,receipt='fixture:render',observed_at=1000)

    def reviewed(self,passed=True):
        task=self.task;self.store.approve(self.owner,task['id'],1,task['manifest_digest']);claim=self.store.claim(self.worker,'a')
        report={'head_sha':'b'*40,'summary':'fixture','checks':[{'name':'fixture','passed':passed}]}
        self.store.report(self.worker,task['id'],claim['lease_token'],report)
        self.store.review(self.manager,task['id'],1,task['manifest_digest'],'fixture:independent')
        with self.store._transaction() as c:
            c.execute('INSERT INTO bridge_verified_candidates VALUES (?,?,?,?,?,?)',(task['id'],digest(report),'b'*40,'f'*64,0,self.now))

    def run_check(self):
        return ReleasePreflight(self.store,repository_observer=lambda *_:self.repo,service_observer=lambda *_:self.live,service_id='srv-fixture').run(
            self.manager,self.task['id'],rollback_sha='a'*40)

    def test_matching_actual_bound_observations_only_prepare_owner_review(self):
        self.reviewed();result=self.run_check();self.assertEqual(result['status'],'ready_for_owner_review')
        self.assertFalse(result['operational_approval_recorded']);self.assertFalse(result['deployment_executed'])
        self.assertTrue(result['disk_downtime_review_required']);self.assertFalse(result['external_data_rollback_verified'])

    def test_missing_integration_stale_observation_and_failed_report_block(self):
        self.reviewed();self.repo['candidate_contains_main']=False
        self.assertIn('latest_main_not_integrated',self.run_check()['issues'])
        self.now+=301;self.assertIn('service_observation_unverified',self.run_check()['issues'])

    def test_unreviewed_or_unconnected_or_wrong_source_is_not_ready(self):
        result=self.run_check();self.assertEqual(result['status'],'blocked')
        self.reviewed();self.live['repository']='other/repo'
        self.assertIn('service_observation_unverified',self.run_check()['issues'])
        empty=ReleasePreflight(self.store).run(self.manager,self.task['id'],rollback_sha='a'*40)
        self.assertIn('repository_connection_required',empty['issues'])
        with self.assertRaises(PermissionDenied):ReleasePreflight(self.store).run(self.worker,self.task['id'],rollback_sha='a'*40)

    def test_wrong_service_id_or_missing_binding_cannot_use_other_main_preview(self):
        self.reviewed();self.live['service_id']='srv-other-preview'
        self.assertIn('service_observation_unverified',self.run_check()['issues'])
        result=ReleasePreflight(self.store,repository_observer=lambda *_:self.repo,service_observer=lambda *_:self.live).run(
            self.manager,self.task['id'],rollback_sha='a'*40)
        self.assertIn('service_identity_not_bound',result['issues'])

    def test_manual_review_of_failed_checks_does_not_become_ready(self):
        self.reviewed(False);self.assertIn('candidate_independent_verification_required',self.run_check()['issues'])


if __name__=='__main__':unittest.main()
