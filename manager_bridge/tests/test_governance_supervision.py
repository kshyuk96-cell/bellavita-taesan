"""Actual canonical loop with synthetic registered services; no model calls."""
from types import SimpleNamespace
from unittest.mock import patch
import threading
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.governance_supervision import GovernanceSupervision,SupervisionSettings
from manager_bridge.governance_service import build_service


class SupervisionTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify

    def host(self):return SimpleNamespace(store=self.store,core=self.core,registry=self.registry,fleet=None,manager_token=lambda:self.manager)

    def supervisor(self,**settings):
        controller=ContinuousManager(self.core)
        return GovernanceSupervision(self.host(),controller,['project'],SupervisionSettings(**settings),
            health=lambda *_:{'state':'running','http_state':'listening','manager_state':'running','observed_at':self.now})

    def test_three_tick_canonical_service_persists_due_and_reports(self):
        controller=ContinuousManager(self.core);settings={'projects':('project',),'instance_id':'service','poll_seconds':1}
        service=build_service(self.host(),controller,settings,listen={'port':0},supervision=SupervisionSettings())
        self.addCleanup(service.close)
        def advance_clock(*args,**kwargs):self.now+=61;return {}
        with patch.object(controller,'tick',side_effect=advance_clock) as tick,patch.object(service.supervision.durability,'auth_watch',wraps=service.supervision.durability.auth_watch) as auth:
            result=service.run(max_ticks=3)
        self.assertEqual(auth.call_count,3)
        self.assertEqual(result['ticks'],3);self.assertEqual(tick.call_count,3)
        report=service.supervision.report(self.manager,['project'])
        self.assertEqual({row['job'] for row in report['jobs']},{'auth','health','recovery'})
        self.assertTrue(all(row['last_attempt']==1122 for row in report['jobs']))
        self.assertTrue(all(row['next_due']>1122 for row in report['jobs']))
        self.assertFalse(report['backup_enabled']);self.assertFalse(report['credential_refresh_performed'])

    def test_restart_before_due_does_not_repeat_observers(self):
        first=self.supervisor();first.poll(self.manager)
        second=self.supervisor()
        with patch.object(second.durability,'auth_watch',side_effect=AssertionError('duplicate read')):
            second.poll(self.manager)
        self.now+=61
        with patch.object(second.durability,'auth_watch',wraps=second.durability.auth_watch) as observer:
            second.poll(self.manager);self.assertEqual(observer.call_count,1)

    def test_failure_persists_nextdue_and_does_not_storm(self):
        service=self.supervisor()
        with patch.object(service.durability,'auth_watch',side_effect=RuntimeError('PRIVATE PROVIDER VALUE')) as observer:
            service.poll(self.manager);service.poll(self.manager)
        self.assertEqual(observer.call_count,1)
        job=next(j for j in service.report(self.manager,['project'])['jobs'] if j['job']=='auth')
        self.assertEqual(job['state'],'unavailable');self.assertGreater(job['next_due'],self.now)
        self.assertNotIn('PRIVATE',str(job))

    def test_backup_requires_ownerloader_and_retains_unknown_without_deleting(self):
        root=self.root/'snapshots';root.mkdir(mode=0o700)
        settings=SupervisionSettings(backup_root=str(root),backup_interval=300,retention=1)
        with self.assertRaises(ValueError):GovernanceSupervision(self.host(),ContinuousManager(self.core),['project'],settings,health=lambda *_:{})
        service=GovernanceSupervision(self.host(),ContinuousManager(self.core),['project'],settings,
            health=lambda *_:{},owner_token_loader=lambda:self.owner)
        service.poll(self.manager)
        snapshots=list(root.iterdir());self.assertEqual(len(snapshots),1)
        self.now+=301;service.poll(self.manager)
        job=next(j for j in service.report(self.manager,['project'])['jobs'] if j['job']=='backup')
        self.assertEqual(job['state'],'retention_required');self.assertEqual(list(root.iterdir()),snapshots)

    def test_backup_failure_persists_attempt_and_due_across_restart(self):
        root=self.root/'snapshots';root.mkdir(mode=0o700)
        settings=SupervisionSettings(backup_root=str(root),backup_interval=300,retention=2)
        controller=ContinuousManager(self.core)
        service=GovernanceSupervision(self.host(),controller,['project'],settings,health=lambda *_:{},owner_token_loader=lambda:self.owner)
        with patch.object(service.durability,'backup',side_effect=OSError('PRIVATE PATH')) as backup:
            service.poll(self.manager);service.poll(self.manager)
        self.assertEqual(backup.call_count,1)
        restarted=GovernanceSupervision(self.host(),controller,['project'],settings,health=lambda *_:{},owner_token_loader=lambda:self.owner)
        with patch.object(restarted.durability,'backup',side_effect=AssertionError('backup storm')):
            restarted.poll(self.manager)
        job=next(j for j in restarted.report(self.manager,['project'])['jobs'] if j['job']=='backup')
        self.assertEqual(job['state'],'uncertain');self.assertGreater(job['next_due'],self.now)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM governance_supervision_backups').fetchone()[0],1)
        self.assertNotIn('PRIVATE',str(job))

    def test_report_marks_old_configuration_snapshot_stale(self):
        service=self.supervisor();service.poll(self.manager)
        self.registry.register(self.owner,'project',self.config,expected_revision=1)
        report=service.report(self.manager,['project'])
        self.assertTrue(all(job['stale'] for job in report['jobs']))
        self.assertNotIn('_configs',str(report))

    def test_running_auth_is_not_reentered_after_due_or_restart(self):
        first=self.supervisor();second=self.supervisor();calls=[]
        original=first.durability.auth_watch
        def slow_observer(*args,**kwargs):
            calls.append('first');self.now+=61
            with patch.object(second.durability,'auth_watch',side_effect=AssertionError('overlapping auth')):
                second.poll(self.manager)
            return original(*args,**kwargs)
        with patch.object(first.durability,'auth_watch',side_effect=slow_observer):first.poll(self.manager)
        self.assertEqual(calls,['first'])
        claim=first._claim(self.manager,'auth',60)
        self.assertIsNotNone(claim)
        self.now+=1000
        restarted=self.supervisor()
        with patch.object(restarted.durability,'auth_watch',side_effect=AssertionError('unknown replay')):
            report=restarted.poll(self.manager)
        job=next(j for j in report['jobs'] if j['job']=='auth')
        self.assertEqual(job['state'],'running');self.assertTrue(job['reconciliation_required'])

    def test_finish_rejects_stale_attempt_cas(self):
        from manager_bridge.store import Conflict
        service=self.supervisor();claim=service._claim(self.manager,'auth',60)
        with self.store._transaction() as c:
            c.execute("UPDATE governance_supervision_jobs SET attempt='different' WHERE job='auth'")
        with self.assertRaises(Conflict):service._finish(self.manager,'auth',claim,'observed',{'items':[]})
        with self.store._transaction() as c:
            self.assertEqual(c.execute("SELECT state FROM governance_supervision_jobs WHERE job='auth'").fetchone()[0],'running')

    def test_two_instances_atomic_backup_retention_reservation(self):
        from concurrent.futures import ThreadPoolExecutor
        root=self.root/'parallel';root.mkdir(mode=0o700)
        settings=SupervisionSettings(backup_root=str(root),backup_interval=300,retention=1)
        controller=ContinuousManager(self.core)
        services=[GovernanceSupervision(self.host(),controller,['project'],settings,health=lambda *_:{},owner_token_loader=lambda:self.owner) for _ in range(2)]
        entered=threading.Event();release=threading.Event();calls=[]
        def backup(*args):
            calls.append(args);entered.set();self.assertTrue(release.wait(3))
            return {'state':'private_backup_complete'}
        with patch.object(services[0].durability,'backup',side_effect=backup),patch.object(services[1].durability,'backup',side_effect=backup):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first=executor.submit(services[0]._backup,'1'*32)
                self.assertTrue(entered.wait(3))
                second=executor.submit(services[1]._backup,'2'*32)
                state,_=second.result(timeout=3)
                self.assertEqual(state,'retention_required');release.set()
                self.assertEqual(first.result(timeout=3)[0],'observed')
        self.assertEqual(len(calls),1)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM governance_supervision_backups').fetchone()[0],1)

    def test_subset_report_cannot_expose_shared_scope_aggregates(self):
        from manager_bridge.store import NotFound,PermissionDenied
        service=GovernanceSupervision(self.host(),ContinuousManager(self.core),['project','other'],SupervisionSettings(),
            health=lambda *_:{'state':'running','http_state':'listening','manager_state':'idle','observed_at':self.now})
        service.poll(self.manager)
        limited=self.store.add_token('subset-reader','manager',['project'])
        with self.assertRaises((NotFound,PermissionDenied)):
            service.report(limited,['project'])
        # Full-scope callers may still request a filtered set of auth identities.
        report=service.report(self.manager,['project'])
        auth=next(job for job in report['jobs'] if job['job']=='auth')
        self.assertTrue(all(item['project']=='project' for item in auth['snapshot']['items']))

    def test_stopped_event_skips_new_scheduled_work(self):
        stopped=threading.Event();stopped.set();service=self.supervisor()
        service.poll(self.manager,stop_event=stopped)
        self.assertEqual(service.report(self.manager,['project'])['jobs'],[])



class SubscriptionSupervisionTests(unittest.TestCase):
    from manager_bridge.tests import test_subscription_broker as broker_fixtures
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    task=fixtures.ThreeTierTests.task
    start=fixtures.ThreeTierTests.start
    auth=broker_fixtures.SubscriptionBrokerTests.auth
    broker=broker_fixtures.SubscriptionBrokerTests.broker
    host=SupervisionTests.host

    def configured(self,broker,loader=None,projects=('project',)):
        return GovernanceSupervision(self.host(),ContinuousManager(self.core),projects,
            SupervisionSettings(subscription_interval=300),health=lambda *_:{'state':'running','http_state':'listening','manager_state':'idle','observed_at':self.now},
            owner_token_loader=loader or (lambda:self.owner),subscription_broker=broker)

    def test_opt_in_canonical_three_ticks_refreshes_once_and_due_is_durable(self):
        broker=self.broker();controller=ContinuousManager(self.core)
        settings={'projects':('project',),'instance_id':'subscription-service','poll_seconds':1}
        service=build_service(self.host(),controller,settings,listen={'port':0},supervision=SupervisionSettings(subscription_interval=300),
            owner_token_loader=lambda:self.owner,subscription_broker=broker)
        self.addCleanup(service.close)
        with patch.object(controller,'tick',return_value={}):result=service.run(max_ticks=3)
        self.assertEqual(result['ticks'],3);self.assertEqual(len(self.calls),1)
        job=next(j for j in service.supervision.report(self.manager,['project'])['jobs'] if j['job']=='subscription')
        self.assertEqual(job['state'],'observed');self.assertEqual(job['snapshot']['generation'],1)
        restarted=self.configured(broker);restarted.poll(self.manager)
        self.assertEqual(len(self.calls),1)
        self.now+=301;restarted.poll(self.manager)
        self.assertEqual(len(self.calls),2);self.assertEqual(broker.status(self.owner)['generation'],2)

    def test_missing_registration_wrong_owner_and_project_scope_rejected(self):
        from manager_bridge.store import PermissionDenied
        broker=self.broker()
        for interval in (True,299,86401):
            with self.assertRaises(ValueError):SupervisionSettings(subscription_interval=interval)
        with self.assertRaises(ValueError):self.configured(None)
        with patch('manager_bridge.governance_service.make_server') as listener:
            with self.assertRaises(ValueError):
                build_service(self.host(),ContinuousManager(self.core),{'projects':('project',),'instance_id':'missing-broker','poll_seconds':1},supervision=SupervisionSettings(subscription_interval=300))
            listener.assert_not_called()
        with self.assertRaises(PermissionDenied):self.configured(broker,loader=lambda:self.manager)
        with self.assertRaises(ValueError):self.configured(broker,projects=('project','other'))
        owner=[self.owner];service=self.configured(broker,loader=lambda:owner[0]);owner[0]=self.manager
        service.poll(self.manager)
        self.assertEqual(self.calls,[])
        job=next(j for j in service.report(self.manager,['project'])['jobs'] if j['job']=='subscription')
        self.assertEqual(job['state'],'unavailable')

    def test_shared_profile_guard_busy_is_retryable_without_refresh(self):
        broker=self.broker();broker.refresh_and_distribute(self.owner,0,'initial-for-guard')
        service=self.configured(broker)
        with broker.profile_guard(self.dev,'project','dev'):
            report=service.poll(self.manager)
            service.poll(self.manager)
            self.assertEqual(len(self.calls),1)
            job=next(j for j in report['jobs'] if j['job']=='subscription')
            self.assertEqual(job['state'],'unavailable');self.assertFalse(job['reconciliation_required'])
            self.assertFalse(report['credential_refresh_performed'])
        self.now+=301;report=service.poll(self.manager)
        self.assertEqual(len(self.calls),2)
        self.assertTrue(report['credential_refresh_performed'])
        self.assertEqual(report['credential_refresh_scope'],'last_retained_subscription_attempt')
        self.assertEqual(report['observed_subscription_refresh']['generation'],2)
        self.assertEqual(report['observed_subscription_refresh']['observed_at'],self.now)

    def test_in_use_targets_do_not_invoke_external_refresh(self):
        broker=self.broker();service=self.configured(broker)
        self.start(self.task());service.poll(self.manager)
        self.assertEqual(self.calls,[])
        job=next(j for j in service.report(self.manager,['project'])['jobs'] if j['job']=='subscription')
        self.assertEqual(job['state'],'unavailable');self.assertGreater(job['next_due'],self.now)

    def test_partial_distribution_uncertainty_blocks_automatic_replay(self):
        import manager_bridge.subscription_broker as module
        broker=self.broker();service=self.configured(broker);original=module._atomic;copied=[]
        def partial(home,data,expected):
            copied.append(home)
            if len(copied)==2:raise OSError('PRIVATE AUTH PATH')
            return original(home,data,expected)
        with patch.object(module,'_atomic',side_effect=partial):service.poll(self.manager)
        self.assertEqual(broker.status(self.owner)['state'],'uncertain')
        self.assertEqual(len(self.calls),1)
        self.now+=1000
        restarted=self.configured(broker);report=restarted.poll(self.manager)
        job=next(j for j in report['jobs'] if j['job']=='subscription')
        self.assertEqual(job['state'],'uncertain');self.assertTrue(job['reconciliation_required'])
        self.assertEqual(len(self.calls),1)
        self.assertIsNone(report['credential_refresh_performed'])
        self.assertIsNone(report['observed_subscription_refresh'])
        self.assertNotIn('PRIVATE',str(report));self.assertNotIn('synthetic-access',str(report))


if __name__=='__main__':unittest.main()
