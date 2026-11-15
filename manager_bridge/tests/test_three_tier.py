"""Fixture-only tests of server-enforced hierarchy policy and exact evidence."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from manager_bridge.store import Store,Conflict,PermissionDenied,token_hash,digest
from manager_bridge.maintenance_registry import Registry
from manager_bridge.coordination import Coordination
from manager_bridge.three_tier import ThreeTier,SourceObservation,CheckOutcome,ReviewEvidence,StopObservation,CodeAssessment


class ThreeTierTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.now=1000
        self.store=Store(self.root/'state.sqlite3',clock=lambda:self.now);self.registry=Registry(self.store);self.coord=Coordination(self.store)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.config={'company':'fixture','name':'Fixture project','source_kind':'github','source_ref':'fixture/project','branch':'main','services':[],'maintenance_enabled':True}
        for project in ('project','other'):self.registry.register(self.owner,project,{**self.config,'source_ref':'fixture/'+project})
        self.manager=self.store.add_token('taesan','manager',['project','other'])
        self.dev=self.store.add_token('developer','worker',['project','other']);self.reviewer=self.store.add_token('reviewer','worker',['project','other'])
        self.outsider=self.store.add_token('outsider','worker',['project']);self.n=0;self.source_changes={};self.proof_changes={};self.host_calls=0
        self.core=ThreeTier(self.store,self.registry,source_observers={'project':self.observe,'other':self.observe},review_hosts={'project':self.verify,'other':self.verify})
        self.core.set_limits(self.owner,None,max_active=2,max_review=2,max_reviewers=1)
        for project in ('project','other'):
            self.core.set_limits(self.owner,project,max_active=2,max_review=2,max_reviewers=1)
            for member,token,role in (('taesan',self.manager,'taesan'),('dev',self.dev,'developer'),('reviewer',self.reviewer,'reviewer')):
                self.core.register(self.owner,project,member,token_hash(token),role,agent_id=None if role=='taesan' else member+'-agent')
        self.command_hash=digest(['/bin/true'])

    def task(self,project='project',dependencies=(),approved=True,base='a'*40):
        self.n+=1
        task=self.store.submit(self.owner,'request-'+str(self.n),{'project':project,'repository':'fixture/'+project,'base_sha':base,'description':'synthetic hierarchy development','checks':['fixture-check'],'kind':'development'})
        self.coord.set_policy(self.owner,task['id'],task['version'],task['manifest_digest'],allowed_paths=['src/'],dependencies=list(dependencies))
        if approved:self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'],policy_digest=digest(self.coord.get_policy(self.owner,task['id'])))
        return self.store.get_task(self.owner,task['id'])

    def start(self,task):
        self.core.start(self.manager,task['id'],'dev','start-'+str(self.n))
        self.coord.readiness(self.owner,token_hash(self.dev),ready=True,reason='ready',ttl=300)
        self.coord.route(self.owner,task['id'],worker_hash=token_hash(self.dev))
        assignment=self.core.claim_development(self.dev,task['id'])
        self.assertIsNotNone(assignment)
        return assignment

    def reported(self,task=None):
        task=task or self.task();assignment=self.start(task)
        self.store.report(self.dev,task['id'],assignment['lease_token'],{'head_sha':'b'*40,'summary':'Fixture reported. Actual independent result is separate.','checks':[{'name':'fixture-check','passed':True}]})
        return task,self.core.submit_candidate(self.dev,task['id'])

    def observe(self,task):
        return replace(SourceObservation(task['project'],task['id'],task['manifest']['base_sha'],'b'*40,'c'*64,digest({'fixture-check':self.command_hash}),'fixture/source',self.now,('src/main.py',),'e'*64),**self.source_changes)

    def verify(self,candidate,reviewer,*,heartbeat):
        self.host_calls+=1;heartbeat()
        return replace(ReviewEvidence(candidate['digest'],reviewer['subject'],(CheckOutcome('fixture-check',self.command_hash,0,.1),),candidate['source']['artifact_digest'],'fixture/independent',self.now,True,'e'*64,CodeAssessment(candidate['digest'],reviewer['subject'],'pass','d'*64,'fixture/code-review',self.now)),**self.proof_changes)

    def reviewed(self,task=None):
        task,candidate=self.reported(task);lease=self.core.claim_review(self.reviewer,task['id'])
        return task,self.core.verify_review(self.reviewer,task['id'],lease['lease_token'])

    def accepted(self):
        task,result=self.reviewed()
        return task,self.core.decide(self.manager,task['id'],result['candidate_digest'])

    def test_full_developer_independent_review_taesan_acceptance(self):
        task,result=self.accepted()
        self.assertEqual(result['state'],'accepted');self.assertTrue(result['technical_accepted']);self.assertFalse(result['operational_approved'])
        self.assertFalse(result['connection_verified']);self.assertEqual(self.host_calls,1)
        upstream=self.store.get_task(self.manager,task['id'])
        self.assertEqual(upstream['state'],'reviewed');self.assertEqual(upstream['review']['report_digest'],digest(upstream['report']))
        self.assertEqual(upstream['review']['scope'],'three_tier_technical_only')

    def test_registration_owner_only_and_same_subject_different_tokens_cannot_review(self):
        duplicate=self.store.add_token('developer','worker',['project'])
        with self.assertRaises(PermissionDenied):self.core.register(self.manager,'project','other',token_hash(self.outsider),'developer')
        with self.assertRaises(Conflict):self.core.register(self.owner,'project','fake-reviewer',token_hash(duplicate),'reviewer')

    def test_unapproved_or_wrong_project_task_cannot_start(self):
        task=self.task(approved=False)
        with self.assertRaises(Conflict):self.core.start(self.manager,task['id'],'dev','start')
        with self.assertRaises(PermissionDenied):self.core.start(self.dev,task['id'],'dev','start')

    def test_missing_capacity_fails_closed(self):
        task=self.task()
        with self.store._transaction() as c:c.execute("DELETE FROM bridge_capacity_limits WHERE scope='*'")
        with self.assertRaises(Conflict):self.core.start(self.manager,task['id'],'dev','start')

    def test_registration_replacement_blocked_while_task_active(self):
        task=self.task();self.core.start(self.manager,task['id'],'dev','start')
        with self.assertRaises(Conflict):self.core.register(self.owner,'project','dev',token_hash(self.outsider),'developer')

    def test_wrong_worker_cannot_claim_managed_development(self):
        task=self.task();self.core.start(self.manager,task['id'],'dev','start')
        with self.assertRaises(PermissionDenied):self.core.claim_development(self.outsider,task['id'])
        with self.assertRaises(PermissionDenied):self.core.claim_development(self.reviewer,task['id'])

    def test_generic_review_record_does_not_supply_independent_proof(self):
        task,candidate=self.reported()
        with self.assertRaises(Conflict):self.core.decide(self.manager,task['id'],candidate['candidate_digest'])
        self.assertEqual(self.host_calls,0)

    def test_review_sublease_does_not_claim_developer_repo_lock(self):
        task,candidate=self.reported();lease=self.core.claim_review(self.reviewer,task['id'])
        self.assertEqual(lease['state'],'reviewing')
        self.assertIn('fixture/project',self.store._reserved_repositories(self._connection_for_reservation()))

    def _connection_for_reservation(self):
        import sqlite3
        connection=sqlite3.connect(self.store.path);connection.row_factory=sqlite3.Row;self.addCleanup(connection.close);return connection

    def test_changed_source_invalidates_previous_review(self):
        task,result=self.reviewed();self.source_changes={'artifact_digest':'d'*64}
        with self.assertRaises(Conflict):self.core.decide(self.manager,task['id'],result['candidate_digest'])
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'needs_verification')

    def test_candidate_paths_outside_approval_rejected(self):
        task=self.task();assignment=self.start(task)
        self.store.report(self.dev,task['id'],assignment['lease_token'],{'head_sha':'b'*40,'summary':'fixture','checks':[{'name':'fixture-check','passed':True}]})
        self.source_changes={'changed_paths':('secrets/private.py',)}
        with self.assertRaises(Conflict):self.core.submit_candidate(self.dev,task['id'])

    def test_actual_report_head_must_match_observed_candidate(self):
        task=self.task();assignment=self.start(task)
        self.store.report(self.dev,task['id'],assignment['lease_token'],{'head_sha':'a'*40,'summary':'fixture','checks':[{'name':'fixture-check','passed':True}]})
        with self.assertRaises(Conflict):self.core.submit_candidate(self.dev,task['id'])

    def test_untrusted_or_mismatched_evidence_cannot_pass(self):
        for change in ({'isolation_verified':False},{'reviewer_subject':'developer'},{'candidate_digest':'f'*64},{'artifact_digest':'f'*64},{'checks':(CheckOutcome('other-check',self.command_hash,0,.1),)}, {'checks':(CheckOutcome('fixture-check','f'*64,0,.1),)}):
            with self.subTest(change=change):
                task,candidate=self.reported(self.task(project='other'))
                lease=self.core.claim_review(self.reviewer,task['id']);self.proof_changes=change
                with self.assertRaises(Conflict):self.core.verify_review(self.reviewer,task['id'],lease['lease_token'])
                # Explicitly retire fixture state; no simulated recovery claim.
                with self.store._transaction() as c:
                    c.execute("UPDATE hierarchy_tasks SET state='accepted' WHERE task_id=?",(task['id'],))
                    c.execute("UPDATE bridge_tasks SET state='reviewed' WHERE id=?",(task['id'],))
                self.proof_changes={}

    def test_failed_check_is_not_technically_accepted(self):
        self.proof_changes={'checks':(CheckOutcome('fixture-check',self.command_hash,1,.1),)}
        task,result=self.reviewed();self.assertEqual(result['state'],'review_failed')
        with self.assertRaises(Conflict):self.core.decide(self.manager,task['id'],result['candidate_digest'])

    def test_revocation_during_actual_review_prevents_remaining_execution(self):
        task,candidate=self.reported();lease=self.core.claim_review(self.reviewer,task['id'])
        def revoked(candidate,reviewer,*,heartbeat):
            self.store.revoke_token(self.owner,token_hash(self.reviewer));heartbeat();raise AssertionError('must not execute')
        self.core.review_hosts['project']=revoked
        with self.assertRaises(Conflict):self.core.verify_review(self.reviewer,task['id'],lease['lease_token'])
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'needs_verification')

    def test_review_timeout_is_uncertain_and_not_reclaimable(self):
        task,candidate=self.reported();lease=self.core.claim_review(self.reviewer,task['id'],lease_seconds=1);self.now+=2
        result=self.core.claim_review(self.reviewer,task['id']);self.assertEqual(result['state'],'needs_verification')
        self.assertFalse(result['automatic_retry_safe'])
        with self.assertRaises(Conflict):self.core.claim_review(self.reviewer,task['id'])

    def test_restart_preserves_review_lease_without_replay(self):
        task,candidate=self.reported();self.core.claim_review(self.reviewer,task['id'])
        resumed=ThreeTier(self.store,self.registry,source_observers={'project':self.observe},review_hosts={'project':self.verify})
        with self.assertRaises(Conflict):resumed.claim_review(self.reviewer,task['id'])
        self.assertEqual(self.host_calls,0)

    def test_rework_bound_and_new_candidate_requires_new_review(self):
        task,result=self.reviewed()
        for attempt in range(2):
            value=self.core.decide(self.manager,task['id'],result['candidate_digest'],'rework');self.assertEqual(value['rework_count'],attempt+1)
            assignment=self.core.claim_rework(self.dev,task['id'])
            self.store.report(self.dev,task['id'],assignment['lease_token'],{'head_sha':'b'*40,'summary':'fixture rework'+str(attempt),'checks':[{'name':'fixture-check','passed':True}]})
            candidate=self.core.submit_candidate(self.dev,task['id']);lease=self.core.claim_review(self.reviewer,task['id']);result=self.core.verify_review(self.reviewer,task['id'],lease['lease_token'])
        result=self.core.decide(self.manager,task['id'],result['candidate_digest'],'rework')
        self.assertEqual(result['state'],'needs_verification');self.assertEqual(result['blocked_reason'],'rework_limit')

    def test_next_development_needs_existing_owner_approval_exact_dependency(self):
        task,parent=self.accepted();following=self.task(dependencies=[task['id']],base='b'*40)
        result=self.core.approve_next(self.manager,task['id'],following['id'],parent['candidate_digest'],'next-request')
        self.assertEqual(result['state'],'development_queued');self.assertFalse(result['execution_started']);self.assertFalse(result['connection_verified'])
        self.assertEqual(result['agent_id'],'dev-agent')
        self.assertEqual(self.core.approve_next(self.manager,task['id'],following['id'],parent['candidate_digest'],'next-request')['task_id'],following['id'])
        with self.assertRaises(Conflict):self.core.approve_next(self.manager,task['id'],following['id'],'f'*64,'next-request')

    def test_next_development_rejects_unapproved_scope_and_cross_project(self):
        task,parent=self.accepted()
        unapproved=self.task(dependencies=[task['id']],approved=False,base='b'*40)
        with self.assertRaises(Conflict):self.core.approve_next(self.manager,task['id'],unapproved['id'],parent['candidate_digest'],'next')
        no_dependency=self.task()
        with self.assertRaises(Conflict):self.core.approve_next(self.manager,task['id'],no_dependency['id'],parent['candidate_digest'],'next2')
        foreign=self.task(project='other')
        with self.assertRaises(Conflict):self.core.approve_next(self.manager,task['id'],foreign['id'],parent['candidate_digest'],'next3')

    def test_overview_is_scoped_and_never_operational_approval(self):
        task,parent=self.accepted();data=self.core.overview(self.manager,['project'])
        self.assertTrue(data['projects'][0]['tasks'][0]['technical_accepted']);self.assertFalse(data['existing_chat_control_verified'])
        with self.assertRaises(PermissionDenied):self.core.overview(self.dev,['project'])
        self.assertNotIn(self.dev,json.dumps(data))

    def test_evidence_isolation_must_be_explicit(self):
        proof=ReviewEvidence('a'*64,'reviewer',(CheckOutcome('check','b'*64,0,0),),'c'*64,'fixture/proof',1000,runtime_digest='e'*64)
        self.assertFalse(proof.isolation_verified)

    def test_test_only_reviewer_can_verify_but_cannot_claim_development(self):
        isolated=self.store.add_token('test-only-reviewer','worker',['project'],capabilities=['test'])
        self.core.register(self.owner,'project','test-reviewer',token_hash(isolated),'reviewer','test-reviewer-agent')
        task,candidate=self.reported();lease=self.core.claim_review(isolated,task['id'])
        result=self.core.verify_review(isolated,task['id'],lease['lease_token'])
        self.assertEqual(result['state'],'review_passed')
        with self.assertRaises(PermissionDenied):self.core.claim_development(isolated,task['id'])

    def test_runtime_change_invalidates_independent_review(self):
        task,result=self.reviewed();self.source_changes={'runtime_digest':'f'*64}
        with self.assertRaises(Conflict):self.core.decide(self.manager,task['id'],result['candidate_digest'])
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'needs_verification')

    def test_runtime_evidence_mismatch_cannot_pass(self):
        task,candidate=self.reported();lease=self.core.claim_review(self.reviewer,task['id'])
        self.proof_changes={'runtime_digest':'f'*64}
        with self.assertRaises(Conflict):self.core.verify_review(self.reviewer,task['id'],lease['lease_token'])

    def test_next_task_cannot_start_from_stale_base(self):
        task,parent=self.accepted();following=self.task(dependencies=[task['id']],base='a'*40)
        with self.assertRaises(Conflict):self.core.approve_next(self.manager,task['id'],following['id'],parent['candidate_digest'],'stale-base')

    def test_rework_cannot_bypass_busy_developer_or_active_capacity(self):
        task,result=self.reviewed();self.core.decide(self.manager,task['id'],result['candidate_digest'],'rework')
        following=self.task(project='other');self.start(following)
        with self.assertRaises(Conflict):self.core.claim_rework(self.dev,task['id'])
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'reported')

    def test_repeated_start_returns_existing_assignment_without_new_execution(self):
        task=self.task();self.start(task)
        result=self.core.start(self.manager,task['id'],'dev','start-'+str(self.n))
        self.assertEqual(result['state'],'developing')
        with self.assertRaises(Conflict):self.core.start(self.manager,task['id'],'dev','changed-request')

    def test_stopped_review_reconciliation_is_exact_and_never_replays(self):
        task,candidate=self.reported();self.core.claim_review(self.reviewer,task['id'],lease_seconds=1);self.now+=2
        self.core.claim_review(self.reviewer,task['id'])
        calls=[]
        def stopped(candidate,context):
            calls.append(context)
            return StopObservation(**context,stopped=True,reference='fixture/confirmed-stop',observed_at=self.now)
        self.core.stop_observers['project']=stopped
        result=self.core.reconcile_review(self.manager,task['id'])
        self.assertTrue(result['stop_confirmed']);self.assertEqual(result['state'],'candidate_ready')
        self.assertFalse(result['execution_started']);self.assertFalse(result['automatic_retry_performed']);self.assertEqual(self.host_calls,0)
        self.assertNotIn('lease_hash',json.dumps(result));self.assertEqual(len(calls),1)

    def test_unverified_stop_keeps_review_reserved(self):
        task,candidate=self.reported();self.core.claim_review(self.reviewer,task['id'])
        self.core.stop_observers['project']=lambda candidate,context:StopObservation(**context,stopped=False,reference='fixture/not-stopped',observed_at=self.now)
        with self.assertRaises(Conflict):self.core.reconcile_review(self.manager,task['id'])
        with self.assertRaises(Conflict):self.core.claim_review(self.reviewer,task['id'])
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'reviewing')

    def test_stopped_but_changed_source_releases_only_capacity(self):
        task,candidate=self.reported();self.core.claim_review(self.reviewer,task['id'])
        self.core.stop_observers['project']=lambda candidate,context:StopObservation(**context,stopped=True,reference='fixture/stopped',observed_at=self.now)
        self.source_changes={'artifact_digest':'d'*64}
        result=self.core.reconcile_review(self.manager,task['id'])
        self.assertEqual(result['state'],'needs_verification');self.assertTrue(result['stop_confirmed']);self.assertFalse(result['technical_accepted'])
        with self.store._transaction() as c:self.assertIsNone(c.execute('SELECT lease_hash FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone()[0])

    def test_stop_reconciliation_can_clean_up_revoked_prior_worker(self):
        task,candidate=self.reported();self.core.claim_review(self.reviewer,task['id'])
        self.store.revoke_token(self.owner,token_hash(self.dev))
        self.core.stop_observers['project']=lambda candidate,context:StopObservation(**context,stopped=True,reference='fixture/stopped-after-revoke',observed_at=self.now)
        result=self.core.reconcile_review(self.manager,task['id'])
        self.assertTrue(result['stop_confirmed']);self.assertEqual(result['state'],'needs_verification');self.assertFalse(result['execution_started'])

    def test_source_change_before_review_does_not_consume_all_review_capacity(self):
        first,candidate=self.reported();self.source_changes={'artifact_digest':'d'*64}
        with self.assertRaises(Conflict):self.core.claim_review(self.reviewer,first['id'])
        self.source_changes={};second,candidate=self.reported(self.task(project='other'))
        self.assertEqual(self.core.claim_review(self.reviewer,second['id'])['state'],'reviewing')

    def test_dashboard_does_not_represent_stale_accepted_source_as_current(self):
        task,result=self.accepted();self.source_changes={'runtime_digest':'f'*64}
        snapshot=self.core.overview(self.manager,['project'])
        row=snapshot['projects'][0]['tasks'][0]
        self.assertFalse(row['technical_accepted']);self.assertEqual(row['review_state'],'not_measured')

    def test_passing_commands_without_independent_code_assessment_cannot_pass_development(self):
        task,candidate=self.reported();lease=self.core.claim_review(self.reviewer,task['id'])
        self.proof_changes={'code_assessment':None}
        with self.assertRaises(Conflict):self.core.verify_review(self.reviewer,task['id'],lease['lease_token'])
        with self.assertRaises(Conflict):self.core.decide(self.manager,task['id'],candidate['candidate_digest'])

    def test_code_review_rework_recommendation_blocks_acceptance_even_when_tests_pass(self):
        task,candidate=self.reported();lease=self.core.claim_review(self.reviewer,task['id'])
        self.proof_changes={'code_assessment':CodeAssessment(candidate['candidate_digest'],'reviewer','rework','d'*64,'fixture/code-review',self.now)}
        result=self.core.verify_review(self.reviewer,task['id'],lease['lease_token'])
        self.assertEqual(result['state'],'review_failed');self.assertEqual(result['checks'][0]['exit_code'],0)
        with self.assertRaises(Conflict):self.core.decide(self.manager,task['id'],candidate['candidate_digest'])

    def test_concurrent_same_review_lease_invokes_actual_host_only_once(self):
        import threading
        task,candidate=self.reported();lease=self.core.claim_review(self.reviewer,task['id'])
        entered=threading.Event();release=threading.Event();results=[]
        def host(candidate,reviewer,*,heartbeat):
            entered.set()
            if not release.wait(3):raise RuntimeError('fixture release timed out')
            return self.verify(candidate,reviewer,heartbeat=heartbeat)
        self.core.review_hosts['project']=host
        def invoke():
            try:results.append(self.core.verify_review(self.reviewer,task['id'],lease['lease_token']))
            except Exception as error:results.append(error)
        first=threading.Thread(target=invoke);first.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(Conflict):self.core.verify_review(self.reviewer,task['id'],lease['lease_token'])
        finally:release.set();first.join(3)
        self.assertFalse(first.is_alive());self.assertEqual(self.host_calls,1)
        self.assertEqual(results[0]['state'],'review_passed')
