"""Owner-budget fixtures: durable local admission only, no paid calls/models."""
from dataclasses import asdict
import json
from concurrent.futures import ThreadPoolExecutor
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.operating_budget import OperatingBudget,BudgetPolicy,BudgetAuthObservation
from manager_bridge.continuous_manager import ContinuousManager,ActionRequest,ActionReceipt
from manager_bridge.store import Conflict,PermissionDenied,NotFound,digest,canonical_json,token_hash
from manager_bridge.maintenance_registry import Registry
from manager_bridge.store import Store
from manager_bridge.three_tier import ThreeTier


class OperatingBudgetTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    task=fixtures.ThreeTierTests.task
    start=fixtures.ThreeTierTests.start
    reported=fixtures.ThreeTierTests.reported
    reviewed=fixtures.ThreeTierTests.reviewed

    def service(self,policy=None,observer='ready',require_policy=False):
        def read(project):
            return BudgetAuthObservation(project,self.registry.get(self.manager,project)['fingerprint'],'ready',self.now,self.now+300,'fixture/current-auth')
        service=OperatingBudget(self.core,auth_observer=read if observer=='ready' else observer,require_policy=require_policy)
        if policy is not None:
            service.install(self.owner,'project',policy)
            service.refresh_auth(self.manager,'project')
        return service

    def reserve(self,service,task,key=None):
        key=key or digest({'task':task['id'],'phase':'start'})
        with self.store._transaction() as c:return service.reserve_in_transaction(c,self.manager,task['project'],key,task['id'])

    def action(self,task,key,state,reason=None,phase='start',parent=None):
        ContinuousManager(self.core)
        config=self.registry.get(self.manager,task['project'])
        request=ActionRequest(key,(parent or task)['id'],task['project'],phase,(parent or task)['version'],
            (parent or task)['manifest_digest'],config['fingerprint'],'unassigned',
            next_task_id=task['id'] if parent else None,next_version=task['version'] if parent else None,
            next_manifest_digest=task['manifest_digest'] if parent else None)
        with self.store._transaction() as c:
            c.execute('INSERT OR REPLACE INTO continuous_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key,request.task_id,task['project'],phase,canonical_json(asdict(request)),token_hash(self.manager),1,state,1,self.now,None,reason,self.now,self.now))

    def finish(self,service,key):
        with self.store._transaction() as c:return service.finish_in_transaction(c,self.manager,key)

    def admitted(self,service,task):
        def start(action,heartbeat):
            heartbeat()
            self.core.start(self.manager,action.task_id,'dev',action.action_key)
            return ActionReceipt(action.action_key,action.task_id,action.phase,'observed','fixture/admitted',self.now)
        controller=ContinuousManager(self.core,handlers={'start':start},admission_budget=service)
        result=controller.tick(self.manager,['project'],'budget-fixture')
        self.assertEqual(result['actions'][0]['state'],'confirmed')
        with self.store._transaction() as c:
            return dict(c.execute('SELECT * FROM operating_budget_reservations WHERE task_id=?',(task['id'],)).fetchone())

    def reviewed_admitted(self,task):
        self.coord.readiness(self.owner,token_hash(self.dev),ready=True,reason='ready',ttl=300)
        self.coord.route(self.owner,task['id'],worker_hash=token_hash(self.dev))
        assignment=self.core.claim_development(self.dev,task['id'])
        self.store.report(self.dev,task['id'],assignment['lease_token'],{'head_sha':'b'*40,'summary':'Fixture actual independently reviewed candidate.','checks':[{'name':'fixture-check','passed':True}]})
        self.core.submit_candidate(self.dev,task['id'])
        lease=self.core.claim_review(self.reviewer,task['id'])
        return self.core.verify_review(self.reviewer,task['id'],lease['lease_token'])

    def test_default_no_policy_is_explicit_and_owner_only_policy_cas(self):
        service=self.service()
        self.assertTrue(service.check(self.manager,'project')['allowed'])
        self.assertEqual(service.check(self.manager,'project')['state'],'not_configured')
        required=OperatingBudget(self.core,require_policy=True)
        self.assertFalse(required.check(self.owner,'project')['allowed'])
        for token in (self.manager,self.dev):
            with self.assertRaises(PermissionDenied):service.install(token,'project',BudgetPolicy(1,2,60))
        first=service.install(self.owner,'project',BudgetPolicy(1,2,60))
        with self.assertRaises(Conflict):service.install(self.owner,'project',BudgetPolicy(2,3,60))
        second=service.install(self.owner,'project',BudgetPolicy(2,3,60),expected_revision=1)
        self.assertEqual((first['revision'],second['revision']),(1,2))
        self.assertFalse(second['execution_authorized'])

    def test_missing_stale_expired_and_negative_auth_pause_only_new_work(self):
        service=self.service(BudgetPolicy(2,5,60),observer=None)
        self.assertEqual(service.check(self.manager,'project')['reason'],'authentication_unknown')
        service.auth_observer=lambda p:BudgetAuthObservation(p,self.registry.get(self.manager,p)['fingerprint'],'ready',self.now,self.now+100,'fixture/auth')
        service.refresh_auth(self.manager,'project');self.assertTrue(service.check(self.manager,'project')['allowed'])
        self.now+=101;self.assertEqual(service.check(self.manager,'project')['reason'],'authentication_expired')
        service.auth_observer=lambda p:BudgetAuthObservation(p,self.registry.get(self.manager,p)['fingerprint'],'unavailable',self.now,None,'fixture/outage')
        service.refresh_auth(self.manager,'project');self.assertEqual(service.check(self.manager,'project')['reason'],'authentication_unavailable')
        # Read/report and pending reconciliation stay available; no old healthy fallback.
        self.assertFalse(service.report(self.manager,['project'])['subscription_usage_measured'])

    def test_atomic_reservation_max_inflight_and_repeat_is_not_readmission(self):
        service=self.service(BudgetPolicy(1,10,60));first=self.task();second=self.task()
        decision=self.reserve(service,first);self.assertTrue(decision['allowed'])
        repeat=self.reserve(service,first);self.assertFalse(repeat['allowed']);self.assertEqual(repeat['reservation_id'],decision['reservation_id'])
        blocked=self.reserve(service,second);self.assertEqual(blocked['reason'],'max_inflight')
        self.assertEqual(service.check(self.manager,'project')['counts']['total'],1)

    def test_parallel_reservation_serializes_one_admission(self):
        service=self.service(BudgetPolicy(1,10,60));tasks=[self.task(),self.task()]
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda task:self.reserve(service,task),tasks))
        self.assertEqual(sum(r['allowed'] for r in results),1)
        self.assertEqual(service.check(self.owner,'project')['counts']['inflight'],1)

    def test_reservation_or_uncertainty_does_not_expire_out_of_budget(self):
        service=self.service(BudgetPolicy(5,1,60));task=self.task();key=digest({'task':task['id'],'phase':'start'})
        reservation=self.reserve(service,task,key);self.action(task,key,'uncertain','handler_uncertain')
        self.assertEqual(self.finish(service,reservation['reservation_id'])['state'],'uncertain')
        self.now+=86400;service.refresh_auth(self.manager,'project')
        check=service.check(self.manager,'project');self.assertEqual(check['reason'],'max_starts_in_window')
        self.assertEqual(check['counts']['held_unknown'],1)
        self.assertFalse(service.reconcile(self.manager,reservation['reservation_id'])['released'])
        reopened=OperatingBudget(self.core,auth_observer=service.auth_observer)
        self.assertEqual(reopened.check(self.manager,'project')['counts']['inflight'],1)

    def test_safe_not_started_requires_durable_action_and_retry_does_not_double_count(self):
        service=self.service(BudgetPolicy(1,1,60));task=self.task();key=digest({'task':task['id'],'phase':'start'})
        reservation=self.reserve(service,task,key)
        self.action(task,key,'waiting','readiness_pending')
        self.assertEqual(self.finish(service,reservation['reservation_id'])['state'],'confirmed_not_started')
        self.assertEqual(service.check(self.owner,'project')['counts']['inflight'],0)
        retry=self.reserve(service,task,key)
        self.assertTrue(retry['allowed']);self.assertEqual(retry['reservation_id'],reservation['reservation_id'])
        self.assertEqual(service.check(self.owner,'project')['counts']['total'],1)

    def test_no_action_proof_is_uncertain_and_finish_cannot_clear_it(self):
        service=self.service(BudgetPolicy(1,2,60));task=self.task();key=digest({'task':task['id'],'phase':'start'})
        reservation=self.reserve(service,task,key)
        self.assertEqual(self.finish(service,reservation['reservation_id'])['state'],'uncertain')
        self.action(task,key,'waiting','readiness_pending')
        self.assertEqual(self.finish(service,reservation['reservation_id'])['state'],'uncertain')
        self.assertEqual(service.check(self.owner,'project')['counts']['inflight'],1)

    def test_only_exact_accepted_completion_releases_inflight_not_window_start(self):
        service=self.service(BudgetPolicy(1,1,60));task=self.task();key=digest({'task':task['id'],'phase':'start'})
        reservation=self.admitted(service,task)
        reservation['reservation_id']=reservation['id']
        self.assertEqual(reservation['state'],'started')
        self.assertFalse(service.reconcile(self.manager,reservation['reservation_id'])['released'])
        reviewed=self.reviewed_admitted(task);self.core.decide(self.manager,task['id'],reviewed['candidate_digest'])
        self.assertTrue(service.reconcile(self.manager,reservation['reservation_id'])['released'])
        check=service.check(self.manager,'project');self.assertEqual(check['counts']['inflight'],0);self.assertEqual(check['reason'],'max_starts_in_window')
        self.now+=61;service.refresh_auth(self.manager,'project');self.assertTrue(service.check(self.manager,'project')['allowed'])

    def test_real_accepted_decision_automatically_frees_inflight_for_next_admission(self):
        service=self.service(BudgetPolicy(1,3,60));first=self.task()
        reservation=self.admitted(service,first)
        reviewed=self.reviewed_admitted(first)
        self.core.decide(self.manager,first['id'],reviewed['candidate_digest'])
        self.assertEqual(service.check(self.manager,'project')['counts']['inflight'],0)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT state FROM operating_budget_reservations WHERE id=?',(reservation['id'],)).fetchone()[0],'completed')
        second=self.task();self.admitted(service,second)
        self.assertEqual(service.check(self.manager,'project')['counts']['inflight'],1)

    def test_auth_observer_reauthorization_revocation_and_fingerprint_binding(self):
        service=self.service(BudgetPolicy(1,2,60))
        def revoke(project):
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return BudgetAuthObservation(project,self.registry.get(self.owner,project)['fingerprint'],'ready',self.now,self.now+100,'fixture/revoked')
        service.auth_observer=revoke
        with self.assertRaises(PermissionDenied):service.refresh_auth(self.manager,'project')
        config=self.registry.get(self.owner,'project')
        self.registry.register(self.owner,'project',{**config['config'],'name':'Changed'},expected_revision=config['revision'])
        self.assertEqual(service.check(self.owner,'project')['reason'],'policy_binding_changed')

    def test_cross_project_scope_and_unapproved_reservation_never_mutate_counts(self):
        service=self.service(BudgetPolicy(1,2,60));limited=self.store.add_token('limited-manager','manager',['other'])
        with self.assertRaises((PermissionDenied,NotFound)):service.report(limited,['other','project'])
        task=self.task(approved=False)
        with self.assertRaises(Conflict):self.reserve(service,task)
        with self.store._transaction() as c:
            with self.assertRaises(PermissionDenied):service.reserve_in_transaction(c,self.owner,'project','a'*64,task['id'])
        self.assertEqual(service.check(self.owner,'project')['counts']['total'],0)

    def test_policy_change_does_not_reset_old_unknown_reservation(self):
        service=self.service(BudgetPolicy(1,2,60));reservation=self.reserve(service,self.task())
        service.install(self.owner,'project',BudgetPolicy(1,100,1),expected_revision=1)
        self.now+=1000;service.refresh_auth(self.manager,'project')
        self.assertEqual(service.check(self.manager,'project')['reason'],'max_inflight')
        self.assertFalse(service.reconcile(self.owner,reservation['reservation_id'])['released'])

    def test_tampered_acceptance_or_revoked_reviewer_never_releases_budget(self):
        service=self.service(BudgetPolicy(1,2,60));task=self.task();reservation=self.admitted(service,task)
        reservation['reservation_id']=reservation['id']
        reviewed=self.reviewed_admitted(task)
        # Recovery after restart: review/accept remains available without an attached
        # budget object, but held admissions require exact explicit reconciliation.
        self.core.admission_budget=None
        self.core.decide(self.manager,task['id'],reviewed['candidate_digest'])
        with self.store._transaction() as c:
            original=c.execute('SELECT review FROM bridge_tasks WHERE id=?',(task['id'],)).fetchone()[0]
            corrupt=json.loads(original);corrupt['evidence_digest']='0'*64
            c.execute('UPDATE bridge_tasks SET review=? WHERE id=?',(canonical_json(corrupt),task['id']))
        self.assertFalse(service.reconcile(self.manager,reservation['reservation_id'])['released'])
        with self.store._transaction() as c:
            c.execute('UPDATE bridge_tasks SET review=? WHERE id=?',(original,task['id']))
            row=c.execute('SELECT evidence FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone()[0]
            corrupt=json.loads(row);corrupt['checks'][0]['exit_code']=1
            c.execute('UPDATE hierarchy_tasks SET evidence=? WHERE task_id=?',(canonical_json(corrupt),task['id']))
        self.assertFalse(service.reconcile(self.manager,reservation['reservation_id'])['released'])
        with self.store._transaction() as c:c.execute('UPDATE hierarchy_tasks SET evidence=? WHERE task_id=?',(row,task['id']))
        self.store.revoke_token(self.owner,token_hash(self.reviewer))
        self.assertFalse(service.reconcile(self.manager,reservation['reservation_id'])['released'])
        self.assertEqual(service.check(self.owner,'project')['counts']['inflight'],1)

    def test_due_only_auth_polling_persists_throttling_and_honors_short_validity(self):
        calls=[]
        def observe(project):
            calls.append(project)
            return BudgetAuthObservation(project,self.registry.get(self.owner,project)['fingerprint'],'ready',self.now,self.now+10,'fixture/current')
        service=self.service(observer=observe)
        self.assertEqual(service.poll_auth(self.manager,['project'])['state'],'not_configured')
        self.assertEqual(calls,[])
        service.install(self.owner,'project',BudgetPolicy(1,2,60))
        first=service.poll_auth(self.manager,['project']);self.assertEqual(first['due_count'],1)
        self.assertEqual(service.poll_auth(self.manager,['project'])['state'],'no_due_observations')
        reopened=OperatingBudget(self.core,auth_observer=observe)
        self.now+=9;self.assertEqual(reopened.poll_auth(self.manager,['project'])['observed'],[])
        self.now+=1;self.assertEqual(len(reopened.poll_auth(self.manager,['project'])['observed']),1)
        self.assertEqual(len(calls),2)

    def test_unknown_auth_polling_is_throttled_bounded_and_scope_checked_before_callback(self):
        calls=[]
        service=self.service(observer=lambda project:calls.append(project))
        for project in ('project','other'):service.install(self.owner,project,BudgetPolicy(1,2,60))
        limited=self.store.add_token('limited-poller','manager',['project'])
        with self.assertRaises((PermissionDenied,NotFound)):service.poll_auth(limited,['project','other'])
        self.assertEqual(calls,[])
        first=service.poll_auth(self.manager,['project','other'],limit=1)
        self.assertTrue(first['truncated']);self.assertEqual(len(calls),1)
        second=service.poll_auth(self.manager,['project','other'],limit=1)
        self.assertEqual(len(second['observed']),1);self.assertEqual(len(calls),2)
        self.assertEqual(service.poll_auth(self.manager,['project','other'])['observed'],[])
        self.assertEqual(service.check(self.owner,'project')['reason'],'authentication_unknown')

    def test_strict_types_bounds_and_bounded_report_no_spend_estimates(self):
        for policy in ((True,1,60),(1,0,60),(1,1,0),(1,1,60,301)):
            with self.assertRaises(ValueError):BudgetPolicy(*policy)
        service=self.service(BudgetPolicy(4,5,60))
        for _ in range(3):self.reserve(service,self.task())
        value=service.report(self.manager,['project'],limit=2)
        self.assertEqual(len(value['reservations']),2);self.assertTrue(value['truncated'])
        self.assertIsNone(value['monetary_spend']);self.assertFalse(value['subscription_usage_measured'])
        self.assertNotIn(self.manager,json.dumps(value))
