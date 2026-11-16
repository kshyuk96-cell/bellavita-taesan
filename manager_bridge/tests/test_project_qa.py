"""Synthetic protocol fixtures for persisted actual-check evidence admission.

These tests do not claim host isolation or real customer coverage. Independent
execution itself remains owned by FixedReviewRunner and its namespace tests.
"""
import copy
import json
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.project_qa import freeze_project_qa_contract, PersistedReviewQAProvider
from manager_bridge.store import Conflict, canonical_json, digest
from manager_bridge.qa_admission import admit_qa_report


class ProjectQATests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.ThreeTierTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.contract=self.freeze()
        self.provider=PersistedReviewQAProvider(self.fx.store,contracts={'project':self.contract})
        self.fx.core.governance_gate.providers['project']=self.provider
        self.fx.core.governance_gate.register_policy(self.fx.owner,'project',**self.contract.policy_arguments())

    def freeze(self,project='project',**changes):
        values=dict(project=project,config_fingerprint=self.fx.registry.get(self.fx.owner,project)['fingerprint'],
                    approved_base_sha='a'*40,checks={'fixture-check':('/bin/true',)},
                    contract_version='mechanical-v1',provider_identity='fixture-durable-mechanical',contract_ref='fixture/mechanical')
        values.update(changes)
        return freeze_project_qa_contract(**values)

    def reviewed(self,project='project'):
        task,result=self.fx.reviewed(self.fx.task(project=project))
        return task,self.fx.core._fresh_candidate(self.fx.manager,task['id'])

    def mutate_evidence(self,task_id,mutate):
        with self.fx.store._transaction() as c:
            row=c.execute('SELECT evidence FROM hierarchy_tasks WHERE task_id=?',(task_id,)).fetchone()
            evidence=json.loads(row['evidence']);mutate(evidence)
            c.execute('UPDATE hierarchy_tasks SET evidence=? WHERE task_id=?',(canonical_json(evidence),task_id))

    def test_predeclared_contract_integrates_durable_review_with_gate_acceptance(self):
        task,candidate=self.reviewed()
        observation=self.provider(candidate,'reviewer')
        admitted=admit_qa_report(observation.report,expected_source_revision='b'*40,
                   required_coverage=self.contract.required_coverage,expected_contract=self.contract.expected_contract)
        self.assertTrue(admitted['admitted'])
        self.assertEqual('synthetic_only',observation.scope)
        self.assertEqual(1000,observation.observed_at)
        self.assertEqual(0,observation.report['scenarios'][0]['actual'])
        result=self.fx.core.decide(self.fx.manager,task['id'],candidate['digest'])
        self.assertTrue(result['technical_accepted'])
        self.assertFalse(result['actual_project_qa_verified'])
        self.assertFalse(result['operational_approved'])

    def test_expected_contract_is_detached_and_source_and_commands_change_its_digest(self):
        original=self.contract.expected_contract
        changed=self.contract.expected_contract;changed['scenarios'].clear()
        self.assertEqual(original,self.contract.expected_contract)
        self.assertNotEqual(digest(original),digest(self.freeze(approved_base_sha='c'*40).expected_contract))
        self.assertNotEqual(digest(original),digest(self.freeze(checks={'fixture-check':('/bin/false',)}).expected_contract))

    def test_freeze_registration_cannot_be_created_after_execution(self):
        task,candidate=self.reviewed()
        late=PersistedReviewQAProvider(self.fx.store,contracts={'project':self.freeze(contract_version='late-v2')})
        with self.assertRaisesRegex(Conflict,'before independent execution'):
            late(candidate,'reviewer')
        self.fx.core.decide(self.fx.manager,task['id'],candidate['digest'])
        next_task,next_candidate=self.reviewed()
        self.assertEqual('late-v2',late(next_candidate,'reviewer').contract_version)
        # Reconstructing an already registered immutable provider is safe.
        PersistedReviewQAProvider(self.fx.store,contracts={'project':self.contract})

    def test_failed_skipped_duplicate_or_altered_commands_never_generate_passing_qa(self):
        task,candidate=self.reviewed()
        with self.fx.store._transaction() as c:
            saved=c.execute('SELECT evidence FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone()['evidence']
        mutations=(lambda e:e['checks'][0].update(exit_code=1),
                   lambda e:e.update(checks=[]),
                   lambda e:e['checks'][0].update(skipped=True),
                   lambda e:e['checks'].append(copy.deepcopy(e['checks'][0])),
                   lambda e:e['checks'][0].update(argv_digest='0'*64),
                   lambda e:e['checks'][0].update(name='unapproved-check'),
                   lambda e:e.update(isolation_verified=False),
                   lambda e:e.update(runtime_digest='0'*64),
                   lambda e:e.update(candidate_digest='0'*64),
                   lambda e:e.update(reviewer_subject='developer'),
                   lambda e:e.update(observed_at=999))
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.fx.store._transaction() as c:
                    c.execute('UPDATE hierarchy_tasks SET evidence=? WHERE task_id=?',(saved,task['id']))
                self.mutate_evidence(task['id'],mutation)
                with self.assertRaises(Conflict):self.provider(candidate,'reviewer')

    def test_task_version_source_runtime_and_manifest_checks_are_exact(self):
        task,candidate=self.reviewed()
        mutations=(lambda v:v['task'].update(version=2),
                   lambda v:v['task'].update(manifest_digest='0'*64),
                   lambda v:v['source'].update(head_sha='c'*40),
                   lambda v:v['source'].update(runtime_digest='0'*64),
                   lambda v:v['task']['manifest'].update(checks=[]),
                   lambda v:v['task']['manifest'].update(checks=['fixture-check','fixture-check']),
                   lambda v:v.update(config_fingerprint='0'*64))
        for mutation in mutations:
            value=copy.deepcopy(candidate);mutation(value)
            with self.subTest(mutation=mutation),self.assertRaises(Conflict):self.provider(value,'reviewer')
        with self.assertRaises(Conflict):self.provider(candidate,'developer')

    def test_completed_durable_execution_is_required(self):
        task,candidate=self.reviewed()
        with self.fx.store._transaction() as c:
            c.execute("UPDATE hierarchy_review_runs SET state='uncertain' WHERE task_id=?",(task['id'],))
        with self.assertRaisesRegex(Conflict,'completed durable'):self.provider(candidate,'reviewer')

    def test_each_project_uses_its_own_frozen_contract(self):
        other=self.freeze('other')
        provider=PersistedReviewQAProvider(self.fx.store,contracts={'project':self.contract,'other':other})
        task,candidate=self.reviewed('other')
        observation=provider(candidate,'reviewer')
        self.assertEqual('other',observation.project)
        self.assertEqual(digest(other.expected_contract),observation.contract_digest)
        self.assertNotEqual(digest(self.contract.expected_contract),observation.contract_digest)
        with self.assertRaisesRegex(Conflict,'not registered'):self.provider(candidate,'reviewer')

    def test_registered_contract_version_cannot_change(self):
        with self.assertRaisesRegex(Conflict,'version is immutable'):
            PersistedReviewQAProvider(self.fx.store,contracts={'project':self.freeze(checks={'fixture-check':('/bin/false',)})})

    def test_revoked_reviewer_and_changed_project_configuration_block(self):
        task,candidate=self.reviewed()
        with self.fx.store._transaction() as c:
            member=c.execute("SELECT credential_hash FROM hierarchy_members WHERE project='project' AND member_id='reviewer'").fetchone()
            c.execute('UPDATE bridge_tokens SET revoked=? WHERE token_hash=?',(self.fx.now,member['credential_hash']))
        with self.assertRaisesRegex(Conflict,'authority no longer current'):
            self.provider(candidate,'reviewer')
        with self.fx.store._transaction() as c:
            c.execute('UPDATE bridge_tokens SET revoked=NULL WHERE token_hash=?',(member['credential_hash'],))
        self.fx.registry.register(self.fx.owner,'project',{**self.fx.config,'branch':'feature'},expected_revision=1)
        with self.assertRaisesRegex(Conflict,'completed exact independent review'):
            self.provider(candidate,'reviewer')

    def test_board_approval_freezes_base_before_development_then_admits_changed_head(self):
        from manager_bridge.source_watch import SourceWatch
        from manager_bridge.proposal_board import ProposalBoard
        SourceWatch(self.fx.store,self.fx.registry)
        self.fx.core.governance_gate.enroll(self.fx.manager,['project'])
        fingerprint=self.fx.registry.get(self.fx.owner,'project')['fingerprint']
        with self.fx.store._transaction() as c:
            c.execute('INSERT INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',('project',fingerprint,'a'*40,'base-revision','fixture/base',self.fx.now,1))
        board=ProposalBoard(self.fx.store,self.fx.registry)
        spec=dict(project='project',repository='fixture/project',base_sha='a'*40,
                  description='Synthetic Board-to-provider mechanical review',checks=['fixture-check'],kind='development',
                  allowed_paths=['src/'],dependencies=[],qa_contract_ref=self.contract.contract_ref,
                  qa_contract_digest=digest(self.contract.expected_contract),priority='P1',risk_rationale='Synthetic mechanical contract')
        proposal=board.propose(self.fx.manager,'mechanical-proposal',spec)
        batch=board.create_batch(self.fx.manager,'mechanical-batch',[proposal['id']])
        approved=board.approve_batch(self.fx.owner,batch['id'],batch['digest'])
        task=self.fx.store.get_task(self.fx.owner,approved['tasks'][0]['id'])
        task,result=self.fx.reviewed(task)
        candidate=self.fx.core._fresh_candidate(self.fx.manager,task['id'])
        self.assertEqual('a'*40,self.contract.payload['approved_base_sha'])
        self.assertEqual('b'*40,candidate['source']['head_sha'])
        observation=self.provider(candidate,'reviewer')
        self.assertEqual('b'*40,observation.source_revision)
        self.assertEqual('b'*40,observation.report['source_revision'])
        self.assertTrue(self.fx.core.decide(self.fx.manager,task['id'],candidate['digest'])['technical_accepted'])

    def board_task(self,base='a'*40,dependencies=(),key='board-task'):
        from manager_bridge.source_watch import SourceWatch
        from manager_bridge.proposal_board import ProposalBoard
        SourceWatch(self.fx.store,self.fx.registry)
        self.fx.core.governance_gate.enroll(self.fx.manager,['project'])
        fingerprint=self.fx.registry.get(self.fx.owner,'project')['fingerprint']
        with self.fx.store._transaction() as c:
            c.execute('INSERT OR IGNORE INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',('project',fingerprint,'a'*40,'base-revision','fixture/base',self.fx.now,1))
        board=ProposalBoard(self.fx.store,self.fx.registry)
        spec=dict(project='project',repository='fixture/project',base_sha=base,
                  description='Synthetic derived mechanical review',checks=['fixture-check'],kind='development',
                  allowed_paths=['src/'],dependencies=list(dependencies),qa_contract_ref=self.contract.contract_ref,
                  qa_contract_digest=digest(self.contract.expected_contract),priority='P1',risk_rationale='Synthetic mechanical contract')
        proposal=board.propose(self.fx.manager,key,spec)
        batch=board.create_batch(self.fx.manager,key,[proposal['id']])
        approved=board.approve_batch(self.fx.owner,batch['id'],batch['digest'])
        return self.fx.store.get_task(self.fx.owner,approved['tasks'][0]['id'])

    def test_same_frozen_anchor_accepts_board_derived_next_task(self):
        from dataclasses import replace
        from manager_bridge.store import token_hash
        parent=self.board_task(key='parent')
        parent,result=self.fx.reviewed(parent)
        parent_candidate=self.fx.core._fresh_candidate(self.fx.manager,parent['id'])
        self.fx.core.decide(self.fx.manager,parent['id'],parent_candidate['digest'])
        child=self.board_task(base='b'*40,dependencies=[parent['id']],key='child')
        self.fx.core.approve_next(self.fx.manager,parent['id'],child['id'],parent_candidate['digest'],'derived-next')
        self.fx.coord.readiness(self.fx.owner,token_hash(self.fx.dev),ready=True,reason='ready',ttl=300)
        self.fx.coord.route(self.fx.owner,child['id'],worker_hash=token_hash(self.fx.dev))
        assignment=self.fx.core.claim_development(self.fx.dev,child['id'])
        self.assertIsNotNone(assignment)
        original=self.fx.observe
        self.fx.core.source_observers['project']=lambda task: replace(original(task),head_sha='c'*40) if task['id']==child['id'] else original(task)
        self.fx.store.report(self.fx.dev,child['id'],assignment['lease_token'],dict(head_sha='c'*40,
                             summary='Synthetic derived candidate',checks=[dict(name='fixture-check',passed=True)]))
        self.fx.core.submit_candidate(self.fx.dev,child['id'])
        lease=self.fx.core.claim_review(self.fx.reviewer,child['id'])
        self.fx.core.verify_review(self.fx.reviewer,child['id'],lease['lease_token'])
        candidate=self.fx.core._fresh_candidate(self.fx.manager,child['id'])
        observation=self.provider(candidate,'reviewer')
        self.assertEqual('a'*40,self.contract.payload['approved_base_sha'])
        self.assertEqual('b'*40,candidate['source']['base_sha'])
        self.assertEqual('c'*40,observation.source_revision)
        self.assertTrue(self.fx.core.decide(self.fx.manager,child['id'],candidate['digest'])['technical_accepted'])

    def test_arbitrary_derived_base_cannot_be_authorized_by_candidate_parent_fields(self):
        parent,result=self.fx.reviewed()
        parent_candidate=self.fx.core._fresh_candidate(self.fx.manager,parent['id'])
        self.fx.core.decide(self.fx.manager,parent['id'],parent_candidate['digest'])
        child=self.fx.task(base='b'*40,dependencies=[parent['id']])
        child,result=self.fx.reviewed(child)
        candidate=self.fx.core._fresh_candidate(self.fx.manager,child['id'])
        candidate['task']['approval']['parent_authority']={'id':parent['id'],'head_sha':'b'*40}
        with self.assertRaisesRegex(Conflict,'approved Board parent authority'):
            self.provider(candidate,'reviewer')

    def test_changed_durable_derived_parent_authority_is_rejected(self):
        parent=self.board_task(key='parent-proof')
        parent,result=self.fx.reviewed(parent)
        parent_candidate=self.fx.core._fresh_candidate(self.fx.manager,parent['id'])
        self.fx.core.decide(self.fx.manager,parent['id'],parent_candidate['digest'])
        child=self.board_task(base='b'*40,dependencies=[parent['id']],key='child-proof')
        child,result=self.fx.reviewed(child)
        candidate=self.fx.core._fresh_candidate(self.fx.manager,child['id'])
        self.assertEqual('b'*40,self.provider(candidate,'reviewer').source_revision)
        proposal_id=candidate['task']['approval']['proposal_id']
        with self.fx.store._transaction() as c:
            snapshot=json.loads(c.execute('SELECT snapshot FROM manager_proposals WHERE id=?',(proposal_id,)).fetchone()['snapshot'])
            snapshot['parent_authority']['head_sha']='d'*40
            c.execute('UPDATE manager_proposals SET snapshot=? WHERE id=?',(canonical_json(snapshot),proposal_id))
        with self.assertRaisesRegex(Conflict,'parent authority changed'):
            self.provider(candidate,'reviewer')
