"""Admissions are counted inside the real dispatch fence, before any handler."""
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.continuous_manager import ContinuousManager,ActionReceipt
from manager_bridge.operating_budget import OperatingBudget,BudgetPolicy,BudgetAuthObservation
from manager_bridge.store import PermissionDenied,token_hash


class OperatingBudgetIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.ThreeTierTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.budget=OperatingBudget(self.fx.core,auth_observer=self.auth)
        self.budget.install(self.fx.owner,'project',BudgetPolicy(1,2,60))
        self.budget.refresh_auth(self.fx.manager,'project');self.calls=[]

    def auth(self,p):return BudgetAuthObservation(p,self.fx.registry.get(self.fx.owner,p)['fingerprint'],'ready',self.fx.now,self.fx.now+100,'fixture/auth-observed')
    def receipt(self,a,state='observed'):return ActionReceipt(a.action_key,a.task_id,a.phase,state,'fixture/budget-action',self.fx.now)
    def start(self,a,heartbeat):
        heartbeat();self.calls.append(a.task_id)
        self.fx.core.start(self.fx.manager,a.task_id,'dev','budget-start-'+a.action_key)
        return self.receipt(a)

    def test_one_admission_no_double_start_and_existing_dispatch_not_quota_blocked(self):
        self.fx.task();self.fx.task()
        scheduler=ContinuousManager(self.fx.core,handlers={'start':self.start},admission_budget=self.budget)
        result=scheduler.tick(self.fx.manager,['project'],'budget-fixture',limit=20)
        self.assertEqual(len(self.calls),1)
        self.assertEqual([a['state'] for a in result['actions']],['confirmed','admission_paused'])
        check=self.budget.check(self.fx.manager,'project')
        self.assertEqual(check['counts']['inflight'],1);self.assertEqual(check['reason'],'max_inflight')
        # Existing admitted work still reaches its registered next handler.
        seen=[]
        def dispatch(a,h):h();seen.append(a.task_id);return self.receipt(a,'not_ready')
        scheduler.handlers['dispatch_development']=dispatch
        scheduler.tick(self.fx.manager,['project'],'budget-fixture',limit=20)
        self.assertEqual(seen,self.calls)

    def test_unknown_auth_blocks_before_intent_and_failed_handler_consumes_budget(self):
        self.fx.task();self.budget.auth_observer=None;self.budget.refresh_auth(self.fx.manager,'project')
        scheduler=ContinuousManager(self.fx.core,handlers={'start':self.start},admission_budget=self.budget)
        first=scheduler.tick(self.fx.manager,['project'],'budget-fixture')
        self.assertEqual(first['actions'][0]['state'],'admission_paused');self.assertFalse(self.calls)
        with self.fx.store._transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM continuous_actions').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM operating_budget_reservations').fetchone()[0],0)
        self.budget.auth_observer=self.auth;self.budget.refresh_auth(self.fx.manager,'project')
        def fail(a,h):self.calls.append(a.task_id);raise ConnectionError('synthetic unknown outcome')
        scheduler.handlers['start']=fail
        second=scheduler.tick(self.fx.manager,['project'],'budget-fixture')
        self.assertEqual(second['actions'][0]['state'],'uncertain')
        self.assertEqual(self.budget.report(self.fx.manager,['project'])['reservations'][0]['state'],'uncertain')
        self.fx.now+=1000
        scheduler.tick(self.fx.manager,['project'],'new-budget-fixture')
        self.assertEqual(len(self.calls),1)

    def test_confirmed_not_started_releases_without_replaying_observed_work(self):
        self.fx.task()
        scheduler=ContinuousManager(self.fx.core,handlers={'start':lambda a,h:self.receipt(a,'not_ready')},admission_budget=self.budget)
        scheduler.tick(self.fx.manager,['project'],'budget-fixture')
        check=self.budget.check(self.fx.manager,'project')
        self.assertEqual(check['counts']['inflight'],0);self.assertEqual(check['counts']['starts_in_window'],0)
        self.fx.now+=2;scheduler.handlers['start']=self.start
        scheduler.tick(self.fx.manager,['project'],'budget-fixture')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.budget.check(self.fx.manager,'project')['counts']['starts_in_window'],1)

    def test_revocation_after_handler_cannot_rollback_uncertain_journal(self):
        self.fx.task()
        def revoke(a,h):
            self.fx.store.revoke_token(self.fx.owner,token_hash(self.fx.manager))
            raise PermissionDenied('synthetic authority revoked')
        scheduler=ContinuousManager(self.fx.core,handlers={'start':revoke},admission_budget=self.budget)
        scheduler.tick(self.fx.manager,['project'],'budget-fixture')
        with self.fx.store._transaction() as c:
            self.assertEqual(c.execute('SELECT state FROM continuous_actions').fetchone()[0],'uncertain')
            self.assertIn(c.execute('SELECT state FROM operating_budget_reservations').fetchone()[0],('reserved','uncertain'))

    def test_legacy_start_and_reopened_core_cannot_bypass_installed_policy(self):
        task=self.fx.task()
        with self.assertRaises(PermissionDenied):self.fx.core.start(self.fx.manager,task['id'],'dev','legacy-bypass')
        from manager_bridge.three_tier import ThreeTier
        reopened=ThreeTier(self.fx.store,self.fx.registry)
        with self.assertRaises(PermissionDenied):reopened.start(self.fx.manager,task['id'],'dev','reopened-bypass')
        calls=[];scheduler=ContinuousManager(reopened,handlers={'start':lambda a,h:calls.append(a)})
        result=scheduler.tick(self.fx.manager,['project'],'missing-budget-coordinator')
        self.assertEqual(result['actions'][0]['state'],'admission_paused');self.assertFalse(calls)
        with self.fx.store._transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM hierarchy_tasks').fetchone()[0],0)

    def test_budgeted_project_without_members_cannot_use_legacy_worker_claim(self):
        self.fx.task()
        with self.fx.store._transaction() as c:c.execute("DELETE FROM hierarchy_members WHERE project='project'")
        self.assertIsNone(self.fx.store.claim(self.fx.dev,'project'))

    def test_older_ready_callback_cannot_overwrite_newer_negative_auth(self):
        entered=threading.Event();release=threading.Event();calls=[]
        fingerprint=self.fx.registry.get(self.fx.owner,'project')['fingerprint']
        def observe(project):
            calls.append(1)
            if len(calls)==1:
                entered.set()
                if not release.wait(5):raise AssertionError('fixture release timeout')
                return BudgetAuthObservation(project,fingerprint,'ready',1000,1100,'fixture/older-ready')
            return BudgetAuthObservation(project,fingerprint,'unavailable',1001,None,'fixture/newer-negative')
        self.budget.auth_observer=observe
        with ThreadPoolExecutor(max_workers=1) as pool:
            older=pool.submit(self.budget.refresh_auth,self.fx.manager,'project')
            self.assertTrue(entered.wait(5))
            self.fx.now=1001;self.budget.refresh_auth(self.fx.manager,'project')
            self.assertFalse(self.budget.check(self.fx.manager,'project')['allowed'])
            self.fx.now=1002;release.set()
            self.assertEqual(older.result(timeout=5)['state'],'superseded')
        self.assertEqual(self.budget.check(self.fx.manager,'project')['reason'],'authentication_unavailable')
        self.budget.auth_observer=lambda p:BudgetAuthObservation(p,fingerprint,'ready',1000,1100,'fixture/cached-old-ready')
        self.assertEqual(self.budget.refresh_auth(self.fx.manager,'project')['state'],'superseded')
        self.assertFalse(self.budget.check(self.fx.manager,'project')['allowed'])
