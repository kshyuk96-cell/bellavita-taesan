import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_three_tier as hierarchy_fixtures
from manager_bridge.tests import test_qa_admission as qa_fixtures
from manager_bridge.worker_quality_handoff import WorkerQualityHandoff
from manager_bridge.business_baselines import BusinessBaselines
from manager_bridge.proposal_board import ProposalBoard
from manager_bridge.artifact_content import ArtifactContent
from manager_bridge.operations_reporting import ArtifactObservation
from manager_bridge.source_watch import SourceWatch
from manager_bridge.store import Conflict,PermissionDenied,token_hash,digest


class WorkerQualityHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fx=hierarchy_fixtures.ThreeTierTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.core,self.store,self.registry=self.fx.core,self.fx.store,self.fx.registry
        self.owner,self.manager=self.fx.owner,self.fx.manager
        qa=qa_fixtures.QAAdmissionTests();qa.fixture();self.expected=qa.expected_contract
        self.core.governance_gate.register_policy(self.owner,'project',contract_version='qa-v1',expected_contract=self.expected,required_coverage=['money'],provider_identity='fixture')
        SourceWatch(self.store,self.registry);cfg=self.registry.get(self.owner,'project')
        with self.store._transaction() as c:
            c.execute('INSERT INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',('project',cfg['fingerprint'],'a'*40,'v1','fixture/source',1000,1))
            c.execute('INSERT INTO manager_source_observation_status VALUES (?,?,?,?)',('project',cfg['fingerprint'],'unchanged',1000))
        root=self.fx.root/'quality-artifacts';root.mkdir(mode=0o700)
        self.artifacts=ArtifactContent(self.core,root,sources={('project','diagnostic'):lambda p,key:ArtifactObservation(p,'diagnostic',key,b'<svg>synthetic approved layout</svg>',self.fx.now)})
        reference=self.artifacts.retain(self.manager,'project','diagnostic','approved-design')
        self.baselines=BusinessBaselines(self.core)
        self.contract={'version':'business-v1','requirements':[{'id':'requirement-'+category,'category':category,'priority':'P1','criterion':criterion,'scenario_ids':['s1']} for category,criterion in (
            ('design','Preserve approved layout and spacing.'),('copy','Keep approved confirmation wording.'),
            ('price_behavior','Preserve quoted amount and rounding rule.'),('completion','Report actual changed files and incomplete checks.'))],
            'references':[{'reference':reference['reference'],'sha256':reference['sha256'],'description':'Synthetic design reference'}],
            'preserve':['Keep authorization boundaries and original customer data unchanged.']}
        draft=self.baselines.draft(self.manager,'project','draft-business',self.contract)
        self.baseline=self.baselines.approve(self.owner,draft['id'],draft['digest'])
        self.handoff=WorkerQualityHandoff(self.core)

    def task(self):
        self.fx.n+=1
        with self.store._transaction() as c:policy=json.loads(c.execute("SELECT policy FROM governance_qa_policies WHERE project='project'").fetchone()[0])
        spec={'project':'project','repository':'fixture/project','base_sha':'a'*40,
              'description':'Preserve owner-approved synthetic business criteria.','checks':['fixture-check'],'kind':'development',
              'allowed_paths':['src/'],'dependencies':[],'qa_contract_ref':policy['contract_ref'],'qa_contract_digest':digest(self.expected),
              'priority':'P1','risk_rationale':'Synthetic scoped handoff fixture.'}
        board=ProposalBoard(self.store,self.registry)
        batch=board.propose_bundle(self.manager,'quality-task-'+str(self.fx.n),[spec])
        return board.approve_batch(self.owner,batch['id'],batch['digest'])['tasks'][0]

    def test_developer_and_reviewer_receive_same_approved_criteria_and_expected_contract(self):
        task=self.task();developer=self.handoff.generate(self.manager,task['id'],'developer')
        task,reviewed=self.fx.reviewed(task)
        reviewer=self.handoff.generate(self.manager,task['id'],'reviewer',reviewed['candidate_digest'])
        self.assertEqual(developer['criteria_digest'],reviewer['criteria_digest'])
        for key in ('requirements','preserve','references','expected_contract','business_baseline'):
            self.assertEqual(developer[key],reviewer[key])
        self.assertNotEqual(developer['binding_digest'],reviewer['binding_digest'])
        self.assertEqual(developer['expected_contract'],self.expected)
        self.assertFalse(developer['reference_materialized']);self.assertTrue(developer['reference_delivery_pending'])
        self.assertEqual(reviewer['visual_validation'],'not_measured')
        self.assertFalse(reviewer['technical_acceptance']);self.assertFalse(reviewer['quality_equivalence_guaranteed'])
        self.assertFalse(reviewer['binding_digest_is_signature'])
        self.assertNotIn(self.manager,json.dumps(reviewer));self.assertNotIn('synthetic approved layout',json.dumps(reviewer))

    def test_no_artifact_body_provider_or_live_observer_is_invoked(self):
        task=self.task()
        with patch.object(self.artifacts,'read_body',side_effect=AssertionError('body transfer forbidden')),patch.object(self.core,'_observe',side_effect=AssertionError('no callback')):
            packet=self.handoff.generate(self.owner,task['id'],'developer')
        self.assertEqual(packet['state'],'bound');self.assertFalse(packet['references'][0]['body_transferred'])

    def test_changed_task_pin_policy_source_and_reviewer_candidate_fail_closed(self):
        task=self.task()
        with self.assertRaises(ValueError):self.handoff.generate(self.manager,task['id'],'reviewer','bad')
        with self.assertRaises(ValueError):self.handoff.generate(self.manager,task['id'],'developer','a'*64)
        with self.store._transaction() as c:c.execute("UPDATE manager_source_observation_status SET state='permission_denied' WHERE project='project'")
        with self.assertRaises(Conflict):self.handoff.generate(self.manager,task['id'],'developer')
        with self.store._transaction() as c:c.execute("UPDATE manager_source_observation_status SET state='unchanged' WHERE project='project'")
        task,reviewed=self.fx.reviewed(task)
        with self.assertRaises(Conflict):self.handoff.generate(self.manager,task['id'],'reviewer','f'*64)
        with self.store._transaction() as c:
            row=c.execute('SELECT approval FROM bridge_tasks WHERE id=?',(task['id'],)).fetchone();approval=json.loads(row['approval']);approval['business_baseline']['digest']='f'*64
            c.execute('UPDATE bridge_tasks SET approval=? WHERE id=?',(json.dumps(approval),task['id']))
        with self.assertRaises(Conflict):self.handoff.generate(self.manager,task['id'],'developer')

    def test_scope_role_revocation_and_reference_expiry_fail_closed(self):
        task=self.task();limited=self.store.add_token('limited-quality','manager',['other'])
        with self.assertRaises((PermissionDenied,LookupError)):self.handoff.generate(limited,task['id'],'developer')
        with self.assertRaises(PermissionDenied):self.handoff.generate(self.fx.dev,task['id'],'developer')
        with self.store._transaction() as c:c.execute("UPDATE artifact_content_records SET expires=999 WHERE project='project'")
        with self.assertRaises(Conflict):self.handoff.generate(self.manager,task['id'],'developer')

    def test_return_boundary_reauth_and_baseline_drift_prevent_packet(self):
        task=self.task();original=self.handoff._recipient
        def revoked(c,*args):
            result=original(c,*args);c.execute('UPDATE bridge_tokens SET revoked=1000 WHERE token_hash=?',(token_hash(self.manager),));return result
        with patch.object(self.handoff,'_recipient',side_effect=revoked):
            with self.assertRaises(PermissionDenied):self.handoff.generate(self.manager,task['id'],'developer')
        self.assertEqual(self.handoff.generate(self.manager,task['id'],'developer')['state'],'bound')

    def test_legacy_project_without_baseline_is_explicitly_not_bound(self):
        task=self.fx.task(project='other')
        packet=self.handoff.generate(self.manager,task['id'],'developer')
        self.assertEqual(packet['state'],'not_bound');self.assertIsNone(packet['business_baseline'])
        self.assertFalse(packet['quality_equivalence_guaranteed']);self.assertFalse(packet['execution_authorized'])

    def test_qa_policy_drift_and_credential_like_text_are_not_delivered(self):
        task=self.task()
        with self.store._transaction() as c:
            row=c.execute("SELECT policy FROM governance_qa_policies WHERE project='project'").fetchone();policy=json.loads(row['policy']);policy['provider_identity']='changed-provider'
            c.execute("UPDATE governance_qa_policies SET policy=?,policy_digest=? WHERE project='project'",(json.dumps(policy),digest(policy)))
        with self.assertRaises(Conflict):self.handoff.generate(self.manager,task['id'],'developer')
        with self.store._transaction() as c:
            with self.assertRaises(ValueError):self.handoff._safe(c,self.manager,{'criterion':'sk-proj-syntheticcredentialonly12345'})
            with self.assertRaises(ValueError):self.handoff._safe(c,self.manager,{'criterion':'prefix'+self.fx.dev+'suffix'})
        with self.assertRaises(TypeError):self.handoff.generate(self.manager,task['id'],'developer',passed=True)

    def test_frozen_candidate_body_tampering_cannot_change_handoff_source(self):
        task=self.task();task,reviewed=self.fx.reviewed(task)
        with self.store._transaction() as c:
            row=c.execute('SELECT candidate FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone();candidate=json.loads(row['candidate'])
            candidate['source']['head_sha']='f'*40
            c.execute('UPDATE hierarchy_tasks SET candidate=? WHERE task_id=?',(json.dumps(candidate),task['id']))
        with self.assertRaises(Conflict):self.handoff.generate(self.manager,task['id'],'reviewer',reviewed['candidate_digest'])

    def test_unrelated_qa_rows_are_projected_without_dropping_any_approved_scenario(self):
        expanded=copy.deepcopy(self.expected)
        for index in range(1000):
            row=copy.deepcopy(self.expected['scenarios']['s1']);row['id']='unrelated-'+str(index)
            row['inputs']={'quantity':index+2};row['state_key']=digest([row['adapter'],row['inputs']])
            expanded['scenarios'][row['id']]=row
        with self.store._transaction() as c:prior=c.execute("SELECT policy_digest FROM governance_qa_policies WHERE project='project'").fetchone()[0]
        self.core.governance_gate.register_policy(self.owner,'project',contract_version='qa-v2',expected_contract=expanded,required_coverage=['money'],provider_identity='fixture',expected_policy_digest=prior)
        contract=copy.deepcopy(self.contract);contract['version']='business-v2'
        draft=self.baselines.draft(self.manager,'project','projected-baseline',contract)
        self.baseline=self.baselines.approve(self.owner,draft['id'],draft['digest'],expected_revision=1)
        self.expected=expanded
        task=self.task();developer=self.handoff.generate(self.manager,task['id'],'developer')
        task,reviewed=self.fx.reviewed(task);reviewer=self.handoff.generate(self.manager,task['id'],'reviewer',reviewed['candidate_digest'])
        self.assertEqual(set(developer['expected_contract']['scenarios']),{'s1'})
        self.assertEqual(developer['qa_contract']['mapped_ids'],['s1'])
        self.assertFalse(developer['qa_contract']['is_full'])
        self.assertEqual(developer['qa_contract']['digest'],digest(expanded))
        self.assertEqual(developer['qa_contract']['projection_digest'],digest(developer['expected_contract']))
        self.assertEqual(developer['criteria_digest'],reviewer['criteria_digest'])
        self.assertLess(len(json.dumps(developer).encode()),16384)
