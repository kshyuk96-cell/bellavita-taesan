"""Business requirement pinning through actual proposal/task/QA boundaries."""
import copy
import unittest
from manager_bridge.tests import test_worker_quality_handoff as fixtures
from manager_bridge.tests import test_qa_admission as qa_fixtures
from manager_bridge.business_baselines import validate_baseline_qa
from manager_bridge.governance_gate import QAObservation
from manager_bridge.store import Conflict,PermissionDenied,digest,token_hash


class BusinessBaselineTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.WorkerQualityHandoffTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.baselines,self.core,self.store,self.owner,self.manager=self.fx.baselines,self.fx.core,self.fx.store,self.fx.owner,self.fx.manager

    def next_draft(self,key='next',version='business-v2'):
        contract=copy.deepcopy(self.fx.contract);contract['version']=version
        return self.baselines.draft(self.manager,'project',key,contract)

    def test_owner_only_cas_immutable_version_and_draft_idempotence(self):
        draft=self.next_draft();self.assertEqual(draft,self.next_draft())
        with self.assertRaises(PermissionDenied):self.baselines.approve(self.manager,draft['id'],draft['digest'],expected_revision=1)
        with self.assertRaises(Conflict):self.baselines.approve(self.owner,draft['id'],draft['digest'])
        result=self.baselines.approve(self.owner,draft['id'],draft['digest'],expected_revision=1)
        self.assertEqual(result['revision'],2);self.assertFalse(result['reference_materialized'])
        old=self.next_draft('old','business-v1')
        with self.assertRaises(Conflict):self.baselines.approve(self.owner,old['id'],old['digest'],expected_revision=2)

    def test_active_work_blocks_baseline_changes(self):
        self.fx.task();draft=self.next_draft()
        with self.assertRaisesRegex(Conflict,'active work'):self.baselines.approve(self.owner,draft['id'],draft['digest'],expected_revision=1)

    def test_old_proposal_cannot_approve_after_new_baseline(self):
        from manager_bridge.proposal_board import ProposalBoard
        board=ProposalBoard(self.store,self.fx.registry)
        spec={'project':'project','repository':'fixture/project','base_sha':'a'*40,'description':'Fixture pending proposal',
              'checks':['fixture-check'],'kind':'development','allowed_paths':['src/'],'dependencies':[],
              'qa_contract_ref':'qa-v1','qa_contract_digest':digest(self.fx.expected),'priority':'P1','risk_rationale':'Synthetic'}
        batch=board.propose_bundle(self.manager,'before-change',[spec]);draft=self.next_draft()
        self.baselines.approve(self.owner,draft['id'],draft['digest'],expected_revision=1)
        with self.assertRaises(Conflict):board.approve_batch(self.owner,batch['id'],batch['digest'])
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM bridge_tasks').fetchone()[0],0)

    def test_legacy_owner_approval_without_pin_cannot_start_or_claim(self):
        task=self.fx.fx.task()
        with self.assertRaises(Conflict):self.core.start(self.manager,task['id'],'dev','legacy')
        self.assertIsNone(self.store.claim(self.fx.fx.dev,'project',task_id=task['id']))

    def test_missing_mapping_and_design_reference_do_not_approve(self):
        contract=copy.deepcopy(self.fx.contract);contract['version']='bad';contract['requirements'][0]['scenario_ids']=['not-registered']
        with self.assertRaises(ValueError):self.baselines.draft(self.manager,'project','bad-map',contract)
        contract=copy.deepcopy(self.fx.contract);contract['version']='no-reference';contract['references']=[]
        draft=self.baselines.draft(self.manager,'project','no-ref',contract)
        with self.assertRaises(Conflict):self.baselines.approve(self.owner,draft['id'],draft['digest'],expected_revision=1)

    def test_expired_or_changed_reference_is_stale(self):
        with self.store._transaction() as c:c.execute("UPDATE artifact_content_records SET sha256=?",('f'*64,))
        self.assertEqual(self.baselines.current(self.manager,'project')['state'],'stale')
        with self.assertRaises(Conflict):self.fx.task()

    def test_revoked_owner_invalidates_baseline_not_manager_scope(self):
        self.store.revoke_token(self.owner,token_hash(self.owner))
        self.assertEqual(self.baselines.current(self.manager,'project')['state'],'stale')

    def test_secret_criteria_rejected_before_draft_is_saved(self):
        contract=copy.deepcopy(self.fx.contract);contract['version']='bad-secret';contract['preserve']=['sk-proj-syntheticsecret123456']
        with self.assertRaises(ValueError):self.baselines.draft(self.manager,'project','secret',contract)
        with self.store._transaction() as c:self.assertIsNone(c.execute("SELECT 1 FROM business_baseline_drafts WHERE request_key='secret'").fetchone())

    def test_mapped_critical_qa_missing_or_unmeasured_cannot_pass(self):
        task=self.fx.task();qa=qa_fixtures.QAAdmissionTests();report=qa.fixture()
        with self.store._transaction() as c:
            row=c.execute('SELECT * FROM bridge_tasks WHERE id=?',(task['id'],)).fetchone()
            result=validate_baseline_qa(c,self.store,row,report)
            self.assertTrue(all(r['mapped_scenarios_observed_pass'] for r in result['requirements']))
            self.assertFalse(result['business_acceptance_verified'])
            report['scenarios']=[]
            with self.assertRaises(Conflict):validate_baseline_qa(c,self.store,row,report)

    def test_actual_technical_acceptance_records_baseline_qa_without_business_claim(self):
        task=self.fx.task();qa=qa_fixtures.QAAdmissionTests();original=qa.fixture()
        def observer(candidate,subject):
            report=copy.deepcopy(original);report['source_revision']=candidate['source']['head_sha'];report['report_digest']=digest(report)
            t=candidate['task']
            return QAObservation('project',t['id'],t['version'],t['manifest_digest'],candidate['config_fingerprint'],candidate['digest'],candidate['source']['head_sha'],
                'qa-v1',digest(self.fx.expected),'fixture',subject,'synthetic_only',self.fx.fx.now,False,report)
        self.core.governance_gate.providers['project']=observer
        task,reviewed=self.fx.fx.reviewed(task)
        result=self.core.decide(self.manager,task['id'],reviewed['candidate_digest'])
        self.assertTrue(result['technical_accepted'])
        recorded=result['qa_admission']['business_baseline_qa']
        self.assertEqual(recorded['baseline_digest'],self.baselines.current(self.manager,'project')['digest'])
        self.assertFalse(recorded['business_acceptance_verified'])

    def test_oversized_criteria_rejected_before_owner_approval(self):
        contract=copy.deepcopy(self.fx.contract);contract['version']='too-large'
        contract['requirements']=[{'id':'r'+str(i),'category':'copy','priority':'P1','criterion':'x'*1012,'scenario_ids':['s1']} for i in range(40)]
        with self.assertRaisesRegex(ValueError,'native handoff'):self.baselines.draft(self.manager,'project','too-large',contract)
        self.assertEqual(self.baselines.current(self.manager,'project')['version'],'business-v1')
