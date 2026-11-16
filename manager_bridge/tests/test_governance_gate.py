"""Synthetic typed-provider fixtures; no candidate Python is executed."""
from dataclasses import replace
import copy
import unittest
from manager_bridge.tests import test_three_tier as hierarchy_fixtures
from manager_bridge.tests import test_qa_admission as qa_fixtures
from manager_bridge.governance_gate import QAObservation
from manager_bridge.store import Conflict, PermissionDenied, NotFound, digest


class GovernanceGateTests(unittest.TestCase):
    def setUp(self):
        self.fx = hierarchy_fixtures.ThreeTierTests(); self.fx.setUp(); self.addCleanup(self.fx.doCleanups)
        self.gate = self.fx.core.governance_gate
        fixture = qa_fixtures.QAAdmissionTests(); self.report = fixture.fixture()
        self.contract = fixture.expected_contract
        self.report['source_revision'] = 'b'*40
        self.changes = {}; self.report_change = lambda r: None
        self.gate.providers['project'] = self.observe

    def register(self, **changes):
        values = dict(contract_version='1', expected_contract=self.contract,
                      required_coverage=['money'], provider_identity='fixture-synthetic-provider')
        values.update(changes)
        return self.gate.register_policy(self.fx.owner, 'project', **values)

    def observe(self, candidate, subject):
        report = copy.deepcopy(self.report); self.report_change(report)
        report['report_digest'] = digest({k:v for k,v in report.items() if k != 'report_digest'})
        task = candidate['task']
        return replace(QAObservation('project',task['id'],task['version'],task['manifest_digest'],
                          candidate['config_fingerprint'],candidate['digest'],candidate['source']['head_sha'],
                          '1',digest(self.contract),'fixture-synthetic-provider',subject,
                          'synthetic_only',self.fx.now,False,report), **self.changes)

    def accept(self):
        task, reviewed = self.fx.reviewed()
        return task, self.fx.core.decide(self.fx.manager,task['id'],reviewed['candidate_digest'])

    def test_registered_gate_admits_synthetic_and_records_scope(self):
        self.register(); task, result = self.accept()
        self.assertTrue(result['technical_accepted'])
        self.assertEqual('synthetic_only',result['qa_admission']['scope'])
        self.assertFalse(result['actual_project_qa_verified'])
        self.assertFalse(self.fx.core.get(self.fx.manager,task['id'])['operational_approved'])

    def test_missing_provider_blocks_real_acceptance(self):
        self.register(); self.gate.providers.clear()
        with self.assertRaisesRegex(Conflict,'provider unavailable'): self.accept()

    def test_owner_only_and_immutable_contract(self):
        with self.assertRaises(PermissionDenied):
            self.gate.register_policy(self.fx.manager,'project',contract_version='1',expected_contract=self.contract,required_coverage=['money'],provider_identity='fixture')
        self.register()
        with self.assertRaisesRegex(Conflict,'exact active policy digest'): self.register(contract_version='2')

    def test_exact_binding_staleness_revocation_and_typed_provider(self):
        self.register(); task, result = self.accept()
        for changes in ({'source_revision':'d'*40},{'candidate_digest':'a'*64},
                        {'contract_version':'2'},{'provider_identity':'forged'},
                        {'reviewer_subject':'developer'},{'version':99},
                        {'observed_at':1},{'revoked':True},{'scope':'actual_project'}):
            self.changes = changes
            with self.subTest(changes=changes):
                self.assertFalse(self.fx.core.get(self.fx.manager,task['id'])['technical_accepted'])
        self.changes = {}
        self.gate.providers['project'] = lambda *_: {'passed':True}
        self.assertFalse(self.fx.core.get(self.fx.manager,task['id'])['technical_accepted'])

    def test_missing_coverage_priority_downgrade_and_critical_nonpass_block(self):
        self.register()
        task, reviewed = self.fx.reviewed()
        for mutate in (lambda r:r['coverage'].update(observed=[]),
                       lambda r:r['scenarios'][0].update(priority='P3'),
                       lambda r:r['scenarios'][0].update(status='blocked',asserted=False),
                       lambda r:r.update(source_revision='c'*40)):
            self.report_change = mutate
            with self.assertRaisesRegex(Conflict,'QA admission failed'):
                self.fx.core.decide(self.fx.manager,task['id'],reviewed['candidate_digest'])

    def test_actual_project_policy_cannot_promote_synthetic_engine(self):
        self.register(scope='actual_project'); self.changes={'scope':'actual_project'}
        with self.assertRaisesRegex(Conflict,'actual project QA engine'): self.accept()

    def test_fresh_next_assignment_blocks_revoked_parent_qa(self):
        self.register(); task, accepted = self.accept()
        following = self.fx.task(dependencies=[task['id']],base='b'*40)
        self.changes={'revoked':True}
        with self.assertRaisesRegex(Conflict,'stale or revoked'):
            self.fx.core.approve_next(self.fx.manager,task['id'],following['id'],accepted['candidate_digest'],'qa-next')

    def test_source_configuration_change_blocks_policy_reuse(self):
        self.register(); task, accepted = self.accept()
        self.fx.registry.register(self.fx.owner,'project',{**self.fx.config,'branch':'release'},expected_revision=1)
        with self.assertRaisesRegex(Conflict,'binding changed'):
            self.fx.core.get(self.fx.manager,task['id'])

    def test_source_change_during_qa_cannot_write_acceptance(self):
        self.register(); task, reviewed = self.fx.reviewed()
        original = self.observe
        def source_changes(candidate, subject):
            observation = original(candidate, subject)
            self.fx.source_changes={'head_sha':'c'*40}
            return observation
        self.gate.providers['project'] = source_changes
        with self.assertRaisesRegex(Conflict,'frozen candidate changed|actual source differs'):
            self.fx.core.decide(self.fx.manager,task['id'],reviewed['candidate_digest'])
        persisted = self.fx.store.get_task(self.fx.owner,task['id'])
        self.assertEqual('reported',persisted['state'])
        self.assertIsNone(persisted['review'])

    def test_source_change_during_parent_qa_cannot_assign_next(self):
        self.register(); task, accepted = self.accept()
        following = self.fx.task(dependencies=[task['id']],base='b'*40)
        original = self.observe
        def source_changes(candidate, subject):
            observation = original(candidate, subject)
            self.fx.source_changes={'artifact_digest':'f'*64}
            return observation
        self.gate.providers['project'] = source_changes
        with self.assertRaisesRegex(Conflict,'frozen candidate changed|actual source differs'):
            self.fx.core.approve_next(self.fx.manager,task['id'],following['id'],accepted['candidate_digest'],'qa-source-next')
        with self.fx.store._transaction() as c:
            self.assertIsNone(c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(following['id'],)).fetchone())

    def test_proposal_qa_is_mandatory_without_policy(self):
        task, reviewed = self.fx.reviewed()
        candidate = self.fx.core._fresh_candidate(self.fx.manager,task['id'])
        candidate['task']['approval']['proposal_batch_id']='fixture-batch'
        with self.assertRaisesRegex(Conflict,'requires registered'):
            self.gate.require(self.fx.manager,candidate,'reviewer')

    def test_proposal_contract_pin_uses_registered_policy(self):
        from manager_bridge.governance_gate import validate_proposal_policy
        registered = self.register(contract_ref='fixture/qa')
        with self.fx.store._transaction() as c:
            self.assertEqual(registered['policy_digest'],validate_proposal_policy(c,'project',dict(qa_contract_ref='fixture/qa',qa_contract_digest=digest(self.contract))))
            for ref, contract_digest in [('other',digest(self.contract)),('fixture/qa','f'*64)]:
                with self.assertRaisesRegex(Conflict,'contract pin'):
                    validate_proposal_policy(c,'project',dict(qa_contract_ref=ref,qa_contract_digest=contract_digest))

    def test_owner_policy_rollover_cas_preserves_immutable_history(self):
        first=self.register()
        with self.assertRaises(PermissionDenied):
            self.gate.register_policy(self.fx.manager,'project',contract_version='2',expected_contract=self.contract,required_coverage=['money'],provider_identity='fixture-synthetic-provider',expected_policy_digest=first['policy_digest'])
        with self.assertRaisesRegex(Conflict,'exact active policy digest'):
            self.register(contract_version='2',expected_policy_digest='0'*64)
        with self.assertRaisesRegex(Conflict,'version is immutable'):
            self.register(provider_identity='other-provider',expected_policy_digest=first['policy_digest'])
        second=self.register(contract_version='2',expected_policy_digest=first['policy_digest'])
        self.assertNotEqual(first['policy_digest'],second['policy_digest'])
        with self.fx.store._transaction() as c:
            rows=c.execute('SELECT contract_version,policy_digest FROM governance_qa_policy_history WHERE project=? ORDER BY contract_version',('project',)).fetchall()
            self.assertEqual([('1',first['policy_digest']),('2',second['policy_digest'])],[tuple(row) for row in rows])
        with self.assertRaisesRegex(Conflict,'new contract version'):
            self.register(expected_policy_digest=second['policy_digest'])

    def test_policy_rollover_requires_new_qa_for_historical_acceptance(self):
        first=self.register(); task, accepted=self.accept()
        self.register(contract_version='2',expected_policy_digest=first['policy_digest'])
        current=self.fx.core.get(self.fx.manager,task['id'])
        self.assertFalse(current['technical_accepted'])
        persisted=self.fx.store.get_task(self.fx.owner,task['id'])
        self.assertEqual(first['policy_digest'],persisted['review']['qa_admission']['policy_digest'])
        self.changes={'contract_version':'2'}
        self.assertTrue(self.fx.core.get(self.fx.manager,task['id'])['technical_accepted'])

    def test_qa_expiration_during_source_fence_cannot_write_acceptance(self):
        self.register(max_age_seconds=1)
        task, reviewed=self.fx.reviewed()
        original_qa=self.observe
        original_source=self.fx.observe
        expire_after_qa=[False]
        def qa(candidate,subject):
            result=original_qa(candidate,subject)
            expire_after_qa[0]=True
            return result
        def source(task):
            if expire_after_qa[0]:
                self.fx.now+=2
            return original_source(task)
        self.gate.providers['project']=qa
        self.fx.core.source_observers['project']=source
        with self.assertRaisesRegex(Conflict,'expired before acceptance'):
            self.fx.core.decide(self.fx.manager,task['id'],reviewed['candidate_digest'])
        persisted=self.fx.store.get_task(self.fx.owner,task['id'])
        self.assertEqual('reported',persisted['state'])
        self.assertIsNone(persisted['review'])

    def test_enrolled_legacy_acceptance_requires_qa_policy(self):
        task,reviewed=self.fx.reviewed()
        self.gate.enroll(self.fx.manager,['project'])
        with self.assertRaisesRegex(Conflict,'enrolled development requires'):
            self.fx.core.decide(self.fx.manager,task['id'],reviewed['candidate_digest'])
        persisted=self.fx.store.get_task(self.fx.owner,task['id'])
        self.assertEqual('reported',persisted['state'])
        self.assertIsNone(persisted['review'])

    def test_enrollment_survives_core_reconstruction_without_gate_configuration(self):
        from manager_bridge.three_tier import ThreeTier
        task,accepted=self.accept()
        self.gate.enroll(self.fx.manager,['project'])
        restarted=ThreeTier(self.fx.store,self.fx.registry,source_observers={'project':self.fx.observe})
        historical=self.fx.store.get_task(self.fx.owner,task['id'])
        self.assertEqual(accepted['candidate_digest'],historical['review']['candidate_digest'])
        with self.assertRaisesRegex(Conflict,'enrolled development requires'):
            restarted.get(self.fx.manager,task['id'])
        with self.assertRaisesRegex(Conflict,'enrolled development requires'):
            restarted.decide(self.fx.manager,task['id'],accepted['candidate_digest'])

    def test_enrollment_is_scoped_and_atomic(self):
        with self.assertRaises(PermissionDenied):
            self.gate.enroll(self.fx.dev,['project'])
        narrow=self.fx.store.add_token('narrow-manager','manager',['project'])
        with self.assertRaises((PermissionDenied,NotFound)):
            self.gate.enroll(narrow,['project','other'])
        with self.fx.store._transaction() as c:
            self.assertEqual(0,c.execute('SELECT COUNT(*) FROM governance_enrolled_projects').fetchone()[0])
