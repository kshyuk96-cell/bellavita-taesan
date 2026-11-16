"""Owner standards and evidence-gap reports; no new tester/model execution."""
from copy import deepcopy
import json
from unittest.mock import patch
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.tests import test_qa_admission as qa_fixtures
from manager_bridge.quality_delivery_report import QualityDeliveryReport
from manager_bridge.business_baselines import BusinessBaselines
from manager_bridge.store import Conflict,PermissionDenied,NotFound,token_hash


class QualityDeliveryReportTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    task=fixtures.ThreeTierTests.task
    start=fixtures.ThreeTierTests.start
    reported=fixtures.ThreeTierTests.reported
    reviewed=fixtures.ThreeTierTests.reviewed
    accepted=fixtures.ThreeTierTests.accepted

    def baseline(self,*,reference=False,count=1):
        fixture=qa_fixtures.QAAdmissionTests();fixture.fixture()
        self.core.governance_gate.register_policy(self.owner,'project',contract_version='fixture-v1',expected_contract=fixture.expected_contract,
            required_coverage=['money'],provider_identity='fixture-provider')
        refs=[]
        if reference:
            from manager_bridge.artifact_content import ArtifactContent
            from manager_bridge.operations_reporting import ArtifactObservation
            root=self.root/'reference-content';root.mkdir(mode=0o700)
            content=ArtifactContent(self.core,root,sources={('project','candidate'):lambda project,key:ArtifactObservation(project,'candidate',key,b'\x89PNG\r\n\x1a\nfixture reference',self.now)})
            retained=content.retain(self.manager,'project','candidate','approved-image')
            refs=[{'reference':retained['reference'],'sha256':retained['sha256'],'description':'Approved sample layout'}]
        contract={'version':'baseline-v1','requirements':[{'id':'r'+str(index),'category':'design' if reference else 'completion','priority':'P1',
            'criterion':'Owner-approved required outcome '+str(index),'scenario_ids':['s1']} for index in range(count)],
            'references':refs,'preserve':['Preserve the existing customer workflow']}
        baselines=BusinessBaselines(self.core);draft=baselines.draft(self.manager,'project','requirements-draft',contract)
        baselines.approve(self.owner,draft['id'],draft['digest'])
        return baselines,contract

    def test_unconfigured_standard_and_queued_work_are_reported_without_scores(self):
        self.task();result=QualityDeliveryReport(self.core).report(self.manager,['project'])
        project=result['projects'][0]
        self.assertEqual(project['baseline']['state'],'not_configured')
        self.assertEqual(project['unfinished_recorded'],1);self.assertEqual(project['unassigned_recorded'],1)
        self.assertIsNone(result['quality_score']);self.assertFalse(result['worker_quality_equivalence_verified'])
        self.assertFalse(result['business_acceptance_verified']);self.assertFalse(result['tester_engine_started'])
        self.assertFalse(result['messages_sent'])

    def test_owner_criteria_and_reference_body_pending_are_explicit(self):
        baselines,contract=self.baseline(reference=True)
        result=QualityDeliveryReport(self.core,baselines).report(self.owner,['project'])
        baseline=result['projects'][0]['baseline']
        self.assertEqual(baseline['state'],'approved');self.assertEqual(baseline['version'],'baseline-v1')
        self.assertEqual(baseline['requirements'][0]['criterion'],contract['requirements'][0]['criterion'])
        self.assertTrue(baseline['references'][0]['reference_materialization_pending'])
        self.assertFalse(baseline['references'][0]['body_delivered_to_workers'])
        self.assertFalse(baseline['requirements'][0]['business_delivery_verified'])
        self.assertFalse(result['reference_bodies_delivered_to_workers'])
        self.assertEqual(baseline['preserve'],contract['preserve'])

    def test_review_omission_is_not_accepted_quality_and_history_is_distinct(self):
        task,_=self.accepted()
        with self.store._transaction() as c:c.execute('UPDATE hierarchy_tasks SET evidence=NULL WHERE task_id=?',(task['id'],))
        result=QualityDeliveryReport(self.core).report(self.manager,['project'])
        task_report=result['projects'][0]['tasks'][0]
        self.assertTrue(task_report['acceptance_recorded']);self.assertFalse(task_report['core_technical_acceptance_current'])
        self.assertIn('independent_review_missing',task_report['gaps'])
        self.assertEqual(result['summary']['review_gaps_in_window'],1)
        self.assertEqual(result['summary']['acceptance_history_unverified_in_window'],1)

    def test_wrong_candidate_review_binding_is_not_current_acceptance(self):
        task,_=self.accepted()
        with self.store._transaction() as c:
            evidence=json.loads(c.execute('SELECT evidence FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone()[0])
            evidence['code_assessment']['candidate_digest']='f'*64
            c.execute('UPDATE hierarchy_tasks SET evidence=? WHERE task_id=?',(json.dumps(evidence),task['id']))
        result=QualityDeliveryReport(self.core).report(self.manager,['project'])
        task_report=result['projects'][0]['tasks'][0]
        self.assertTrue(task_report['independent_review_recorded'])
        self.assertFalse(task_report['independent_review_binding_current'])
        self.assertFalse(task_report['core_technical_acceptance_current'])
        self.assertIn('independent_review_invalid_or_authority_changed',task_report['gaps'])

    def test_old_task_missing_current_baseline_pin_is_visible_without_mutation(self):
        task,_=self.accepted();baselines,_=self.baseline()
        result=QualityDeliveryReport(self.core,baselines).report(self.manager,['project'])
        task_report=result['projects'][0]['tasks'][0]
        self.assertFalse(task_report['approved_baseline_pin_current'])
        self.assertIn('business_baseline_pin_missing_or_changed',task_report['gaps'])
        with self.store._transaction() as c:
            approval=json.loads(c.execute('SELECT approval FROM bridge_tasks WHERE id=?',(task['id'],)).fetchone()[0])
        self.assertNotIn('business_baseline',approval)
        self.assertFalse(task_report['business_acceptance_verified'])

    def test_collection_baseline_change_invalidates_current_acceptance(self):
        self.accepted();baselines,contract=self.baseline();original=self.core.overview
        def change(*args,**kwargs):
            value=original(*args,**kwargs);next_contract=deepcopy(contract);next_contract['version']='baseline-v2'
            draft=baselines.draft(self.manager,'project','requirements-v2',next_contract)
            baselines.approve(self.owner,draft['id'],draft['digest'],expected_revision=1)
            return value
        with patch.object(self.core,'overview',side_effect=change):
            result=QualityDeliveryReport(self.core,baselines).report(self.manager,['project'])
        project=result['projects'][0]
        self.assertEqual(project['baseline']['version'],'baseline-v2')
        self.assertTrue(project['baseline_changed_during_collection'])
        self.assertFalse(project['tasks'][0]['core_technical_acceptance_current'])
        self.assertIn('baseline_changed_during_collection',project['tasks'][0]['gaps'])

    def test_scope_before_callbacks_and_revocation_after_collection(self):
        report=QualityDeliveryReport(self.core);limited=self.store.add_token('limited-quality','manager',['other'])
        with patch.object(self.core,'overview',side_effect=AssertionError('unauthorized callback')):
            with self.assertRaises((PermissionDenied,NotFound)):report.report(limited,['project'])
        original=self.core.overview
        def revoked(*args,**kwargs):
            value=original(*args,**kwargs);self.store.revoke_token(self.owner,token_hash(self.manager));return value
        with patch.object(self.core,'overview',side_effect=revoked):
            with self.assertRaises(PermissionDenied):report.report(self.manager,['project'])

    def test_criteria_and_task_windows_are_partial_not_percent_complete(self):
        baselines,_=self.baseline(count=21)
        result=QualityDeliveryReport(self.core,baselines).report(self.manager,['project'])
        self.assertTrue(result['projects'][0]['baseline']['requirements_window']['partial'])
        self.assertFalse(result['summary_complete']);self.assertEqual(len(result['projects'][0]['baseline']['requirements']),20)
        self.assertNotIn('100%',json.dumps(result))

    def test_stale_baseline_and_role_binding_are_not_reinterpreted_as_ready(self):
        baselines,_=self.baseline()
        self.registry.register(self.owner,'project',self.config,expected_revision=1)
        result=QualityDeliveryReport(self.core,baselines).report(self.owner,['project'])
        self.assertEqual(result['projects'][0]['baseline']['state'],'stale')
        self.assertEqual(result['projects'][0]['active_role_counts'],{'taesan':0,'developer':0,'reviewer':0})
        self.assertFalse(result['projects'][0]['qa']['policy_current'])

    def test_global_freshness_budget_and_one_overview_call(self):
        task,_=self.accepted();report=QualityDeliveryReport(self.core)
        with patch.object(self.core,'overview',wraps=self.core.overview) as overview:
            result=report.report(self.manager,['project'],freshness_limit=0)
        self.assertEqual(overview.call_count,1);self.assertEqual(result['task_window']['freshness_checks'],0)
        self.assertFalse(result['projects'][0]['tasks'][0]['core_technical_acceptance_current'])
        self.assertEqual(result['projects'][0]['tasks'][0]['source_freshness'],'not_checked')
        with self.assertRaises(ValueError):report.report(self.manager,['project'],freshness_limit=4)


if __name__=='__main__':unittest.main()
