"""Read-only recovery classification and real local process guard fixtures."""
import multiprocessing
import os
from pathlib import Path
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.recovery_readiness import RecoveryReadiness
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.store import Store,Conflict,PermissionDenied,NotFound
from manager_bridge.maintenance_registry import Registry
from manager_bridge.three_tier import ThreeTier


def _guard_child(database,token,events,release):
    store=Store(database);core=ThreeTier(store,Registry(store))
    try:
        with RecoveryReadiness(core).instance_guard(token,['project'],'fixture-child'):
            events.put('held');release.wait(10)
    except Conflict:events.put('blocked')


class RecoveryReadinessTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    task=fixtures.ThreeTierTests.task
    start=fixtures.ThreeTierTests.start

    def test_clean_inspection_never_claims_worker_stop_or_uptime(self):
        result=RecoveryReadiness(self.core).inspect(self.manager,['project'])
        self.assertTrue(result['dispatch_ready']);self.assertFalse(result['automatic_replay'])
        self.assertFalse(result['workers_stopped_verified']);self.assertFalse(result['always_on_verified'])
        self.assertEqual(result['local_process_guard'],'not_acquired_by_inspection')
        limited=self.store.add_token('limited','manager',['other'])
        with self.assertRaises((NotFound,PermissionDenied)):RecoveryReadiness(self.core).inspect(limited,['project'])
        with self.assertRaises(PermissionDenied):RecoveryReadiness(self.core).inspect(self.dev,['project'])

    def test_unknown_actions_persist_after_reopen_and_are_never_replayed(self):
        task=self.task();calls=[]
        controller=ContinuousManager(self.core,handlers={'start':lambda *args:(_ for _ in ()).throw(ConnectionError())})
        controller.tick(self.manager,['project'],'before-disconnect')
        reopened=Store(self.store.path,clock=lambda:self.now);core=ThreeTier(reopened,Registry(reopened))
        service=RecoveryReadiness(core)
        result=service.inspect(self.manager,['project'])
        self.assertFalse(result['dispatch_ready']);self.assertIn('uncertain_actions',result['blockers'])
        self.assertEqual(result['counts']['uncertain_actions'],1)
        ContinuousManager(core,handlers={'start':lambda *args:calls.append(args)}).tick(self.manager,['project'],'after-restart')
        self.assertEqual(calls,[])
        self.assertFalse(result['workers_stopped_verified'])
        self.assertEqual(reopened.get_task(self.manager,task['id'])['state'],'queued')

    def test_unresponsive_worker_expired_lease_is_not_a_stopped_worker(self):
        task=self.task();self.start(task);self.now+=301
        result=RecoveryReadiness(self.core).inspect(self.manager,['project'])
        self.assertEqual(result['counts']['expired_worker_leases'],1)
        self.assertFalse(result['dispatch_ready']);self.assertFalse(result['workers_stopped_verified'])
        # Inspection does not expire/requeue task rows or manufacture stop evidence.
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT state FROM bridge_tasks WHERE id=?',(task['id'],)).fetchone()[0],'running')
            self.assertIsNone(c.execute('SELECT 1 FROM bridge_stop_confirmations WHERE task=?',(task['id'],)).fetchone())

    def test_restore_marker_and_bounded_details_fail_closed(self):
        controller=ContinuousManager(self.core)
        with self.store._transaction() as c:
            c.execute('CREATE TABLE governance_restore_marker (reconciliation_required INTEGER)')
            c.execute('INSERT INTO governance_restore_marker VALUES (1)')
            for index in range(3):
                c.execute("INSERT INTO continuous_actions VALUES (?,?,?,?,?,?,?,'uncertain',0,0,NULL,NULL,?,?)",(str(index).zfill(64),str(index).zfill(32),'project','start','{}','actor',1,self.now,self.now))
        result=RecoveryReadiness(self.core,controller=controller).inspect(self.manager,['project'],limit=1)
        self.assertTrue(result['restore_quarantined']);self.assertFalse(result['dispatch_ready'])
        self.assertTrue(result['details']['partial']);self.assertEqual(len(result['items']),1)
        self.assertEqual(result['counts']['uncertain_actions'],3)

    def test_unresolved_supervision_is_scoped_and_not_retried_by_inspection(self):
        from types import SimpleNamespace
        from manager_bridge.governance_supervision import GovernanceSupervision,SupervisionSettings
        controller=ContinuousManager(self.core)
        host=SimpleNamespace(core=self.core,store=self.store,registry=self.registry)
        supervision=GovernanceSupervision(host,controller,['project','other'],SupervisionSettings(),health=lambda *_:{})
        claim=supervision._claim(self.manager,'auth',60)
        self.assertIsNotNone(claim);self.now+=61
        recovery=RecoveryReadiness(self.core,controller=controller,supervision=supervision)
        result=recovery.inspect(self.manager,['project'])
        self.assertEqual(result['counts']['unresolved_supervision_jobs'],1)
        self.assertFalse(result['dispatch_ready'])
        limited=self.store.add_token('limited-recovery','manager',['project'])
        with self.assertRaises((PermissionDenied,NotFound)):recovery.inspect(limited,['project'])
        with self.store._transaction() as c:
            self.assertEqual(c.execute("SELECT state FROM governance_supervision_jobs WHERE job='auth'").fetchone()[0],'running')

    def test_unresolved_host_assignment_is_not_completed_from_missing_process(self):
        task=self.task()
        with self.store._transaction() as c:
            c.execute('CREATE TABLE continuous_host_assignments (task_id TEXT PRIMARY KEY,phase TEXT)')
            c.execute("INSERT INTO continuous_host_assignments VALUES (?,'uncertain')",(task['id'],))
        result=RecoveryReadiness(self.core).inspect(self.manager,['project'])
        self.assertEqual(result['counts']['unresolved_host_assignments'],1)
        self.assertIn('unresolved_host_assignments',result['blockers'])
        self.assertFalse(result['workers_stopped_verified'])
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT phase FROM continuous_host_assignments').fetchone()[0],'uncertain')

    def test_foreign_scope_actor_epoch_leader_cannot_hide_running_or_host_work(self):
        from manager_bridge.store import digest,token_hash
        ContinuousManager(self.core);task=self.task();manager_hash=token_hash(self.manager)
        with self.store._transaction() as c:
            c.execute('INSERT INTO continuous_leader VALUES (1,?,?,?,?,?)',('instance',manager_hash,digest(['project']),1,self.now+300))
            c.execute("INSERT INTO continuous_actions VALUES (?,?,?,?,?,?,?,'running',0,0,NULL,NULL,?,?)",('a'*64,task['id'],'project','start','{}',manager_hash,1,self.now,self.now))
            c.execute('CREATE TABLE continuous_host_assignments (task_id TEXT PRIMARY KEY,phase TEXT)')
            c.execute("INSERT INTO continuous_host_assignments VALUES (?,'executing')",(task['id'],))
        recovery=RecoveryReadiness(self.core)
        self.assertEqual(recovery.inspect(self.manager,['project'])['leader_state'],'live')
        for change in ('scope','action_epoch','action_actor','member_binding'):
            with self.store._transaction() as c:
                c.execute('UPDATE continuous_leader SET projects_digest=?',(digest(['project']),))
                c.execute('UPDATE continuous_actions SET actor_hash=?,epoch=1',(manager_hash,))
                if change=='scope':c.execute('UPDATE continuous_leader SET projects_digest=?',(digest(['other']),))
                elif change=='action_epoch':c.execute('UPDATE continuous_actions SET epoch=2')
                elif change=='action_actor':c.execute('UPDATE continuous_actions SET actor_hash=?',(token_hash(self.owner),))
                else:c.execute("UPDATE hierarchy_members SET config_fingerprint=? WHERE project='project' AND role='taesan'",('f'*64,))
            result=recovery.inspect(self.manager,['project'])
            self.assertEqual(result['leader_state'],'unobserved',change)
            self.assertFalse(result['dispatch_ready'],change)
            self.assertIn('running_actions',result['blockers'])
            self.assertIn('unresolved_host_assignments',result['blockers'])

    def test_lifetime_guard_blocks_second_object_and_releases_without_stale_pid(self):
        first=RecoveryReadiness(self.core);second=RecoveryReadiness(self.core)
        with first.instance_guard(self.manager,['project'],'first') as held:
            self.assertEqual(held['state'],'exclusive_local_manager_guard_held')
            with self.assertRaises(Conflict):
                with second.instance_guard(self.manager,['project'],'second'):pass
        with second.instance_guard(self.manager,['project'],'restart'):pass
        locks=list(self.root.glob('.manager-runtime-*.guard'))
        self.assertEqual(len(locks),1);self.assertEqual(locks[0].stat().st_mode&0o777,0o600)
        self.assertEqual(locks[0].read_bytes(),b'')
        link=self.root/'extra-link';os.link(locks[0],link)
        with self.assertRaises(ValueError):
            with first.instance_guard(self.manager,['project'],'hardlink'):pass
        link.unlink()

    def test_real_second_process_blocked_and_crash_releases_local_guard(self):
        context=multiprocessing.get_context('spawn');events=context.Queue();release=context.Event()
        service=RecoveryReadiness(self.core)
        with service.instance_guard(self.manager,['project'],'parent'):
            child=context.Process(target=_guard_child,args=(str(self.store.path),self.manager,events,release));child.start()
            self.assertEqual(events.get(timeout=5),'blocked');child.join(timeout=5)
            self.assertEqual(child.exitcode,0)
        child=context.Process(target=_guard_child,args=(str(self.store.path),self.manager,events,release));child.start()
        try:
            self.assertEqual(events.get(timeout=5),'held')
            with self.assertRaises(Conflict):
                with service.instance_guard(self.manager,['project'],'duplicate'):pass
        finally:
            child.terminate();child.join(timeout=5)
        with service.instance_guard(self.manager,['project'],'after-crash'):pass


if __name__=='__main__':unittest.main()
