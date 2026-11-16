"""Durable admission drain and inert unit draft fixtures; no deployment/models."""
import multiprocessing
import os
from pathlib import Path
import signal
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.runtime_transition import RuntimeTransition,assert_new_start,UnitDraft,render_unit,write_unit_draft
from manager_bridge.store import Store,Conflict,PermissionDenied,NotFound
from manager_bridge.three_tier import ThreeTier
from manager_bridge.maintenance_registry import Registry
from manager_bridge.recovery_readiness import RecoveryReadiness


def _synthetic_service(database,token,events,mode):
    store=Store(database);core=ThreeTier(store,Registry(store));transition=RuntimeTransition(core)
    stopped=False
    def stop(*_):
        nonlocal stopped;stopped=True
    signal.signal(signal.SIGTERM,stop)
    with RecoveryReadiness(core).instance_guard(token,['project'],'synthetic-service'):
        events.put('started')
        while not stopped:
            state=transition.status(token,['project'])
            if state['projects'][0]['mode']=='draining':
                events.put('draining')
                if mode=='drain_then_exit':break
            import time
            time.sleep(.02)
        events.put('graceful_exit')


class RuntimeTransitionTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    task=fixtures.ThreeTierTests.task
    start=fixtures.ThreeTierTests.start
    reported=fixtures.ThreeTierTests.reported
    reviewed=fixtures.ThreeTierTests.reviewed
    accepted=fixtures.ThreeTierTests.accepted

    def test_owner_cas_atomic_scope_idempotence_and_persistent_drain(self):
        transition=RuntimeTransition(self.core)
        for token in (self.manager,self.dev):
            with self.assertRaises(PermissionDenied):transition.drain(token,['project'],{'project':0},'denied')
        limited=self.store.add_token('limited-owner','owner',['project'])
        with self.assertRaises((PermissionDenied,NotFound)):transition.drain(limited,['project','other'],{'project':0,'other':0},'scoped')
        self.assertEqual(transition.status(self.manager,['project'])['projects'][0]['revision'],0)
        first=transition.drain(self.owner,['project'],{'project':0},'drain-one')
        self.assertEqual(transition.drain(self.owner,['project'],{'project':0},'drain-one'),first)
        with self.assertRaises(Conflict):transition.resume(self.owner,['project'],{'project':0},'wrong-revision')
        reopened=Store(self.store.path,clock=lambda:self.now);core=ThreeTier(reopened,Registry(reopened))
        with reopened._transaction() as c:
            with self.assertRaises(Conflict):assert_new_start(c,'project')
        RuntimeTransition(core).resume(self.owner,['project'],{'project':1},'explicit-resume')
        with reopened._transaction() as c:assert_new_start(c,'project')
        with self.assertRaises(Conflict):transition.drain(self.owner,['project'],{'project':0},'drain-one')

    def test_stale_configuration_remains_blocked_until_owner_cas(self):
        transition=RuntimeTransition(self.core)
        transition.resume(self.owner,['project'],{'project':0},'enable-state')
        self.registry.register(self.owner,'project',self.config,expected_revision=1)
        with self.store._transaction() as c:
            with self.assertRaises(Conflict):assert_new_start(c,'project')
        self.assertFalse(transition.status(self.manager,['project'])['projects'][0]['configuration_current'])
        transition.resume(self.owner,['project'],{'project':1},'review-new-config')
        with self.store._transaction() as c:assert_new_start(c,'project')

    def test_drain_actual_start_fence_preserves_existing_report_review_and_accept(self):
        # Root wires assert_new_start at ThreeTier._admission and budget._check.
        task,assignment=self.task(),None
        assignment=self.start(task)
        pending=self.task();transition=RuntimeTransition(self.core)
        transition.drain(self.owner,['project'],{'project':0},'planned-update')
        with self.assertRaises(Conflict):self.core.start(self.manager,pending['id'],'dev','blocked-start')
        self.store.report(self.dev,task['id'],assignment['lease_token'],{'head_sha':'b'*40,'summary':'Fixture only','checks':[{'name':'fixture-check','passed':True}]})
        self.core.submit_candidate(self.dev,task['id']);lease=self.core.claim_review(self.reviewer,task['id'])
        reviewed=self.core.verify_review(self.reviewer,task['id'],lease['lease_token'])
        accepted=self.core.decide(self.manager,task['id'],reviewed['candidate_digest'])
        self.assertTrue(accepted['technical_accepted'])
        status=transition.status(self.manager,['project'])
        self.assertTrue(status['drain_ready']);self.assertFalse(status['workers_stopped_verified'])

    def test_budget_and_next_child_new_admission_are_blocked_during_drain(self):
        from manager_bridge.operating_budget import OperatingBudget
        task,accepted=self.accepted()
        child=self.task(dependencies=[task['id']],base='b'*40)
        budget=OperatingBudget(self.core)
        self.assertTrue(budget.check(self.manager,'project')['allowed'])
        transition=RuntimeTransition(self.core)
        transition.drain(self.owner,['project'],{'project':0},'drain-new-admission')
        decision=budget.check(self.manager,'project')
        self.assertFalse(decision['allowed']);self.assertEqual(decision['reason'],'runtime_transition_blocked')
        with self.assertRaises(Conflict):
            self.core.approve_next(self.manager,task['id'],child['id'],accepted['candidate_digest'],'no-child-start')
        self.assertTrue(self.core.get(self.manager,task['id'])['technical_accepted'])

    def test_raw_legacy_claim_cannot_bypass_drain_and_owner_cas_resume_restores_it(self):
        # Separate legacy project has no hierarchy members/budget admission policy.
        store=Store(self.root/'legacy-manager.sqlite',clock=lambda:self.now)
        owner=store.add_token('legacy-owner','owner',['*']);registry=Registry(store)
        registry.register(owner,'project',self.config)
        core=ThreeTier(store,registry);worker=store.add_token('legacy-worker','worker',['project'])
        task=store.submit(owner,'before-drain',{'project':'project','repository':'fixture/project',
            'base_sha':'a'*40,'description':'Legacy fixture','checks':['fixture'],'kind':'development'})
        store.approve(owner,task['id'],task['version'],task['manifest_digest'])
        transition=RuntimeTransition(core)
        transition.drain(owner,['project'],{'project':0},'drain-legacy')
        self.assertIsNone(store.claim(worker,'project',task_id=task['id']))
        self.assertEqual(store.get_task(owner,task['id'])['state'],'queued')
        transition.resume(owner,['project'],{'project':1},'resume-legacy')
        assignment=store.claim(worker,'project',task_id=task['id'])
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment['id'],task['id'])
        self.assertEqual(store.get_task(owner,task['id'])['state'],'running')

    def test_timeout_or_active_unknown_work_is_never_drain_ready(self):
        task=self.task();self.start(task);transition=RuntimeTransition(self.core)
        transition.drain(self.owner,['project'],{'project':0},'drain-busy');self.now+=1000
        result=transition.status(self.manager,['project'])
        self.assertFalse(result['drain_ready']);self.assertFalse(result['workers_stopped_verified'])
        self.assertGreater(result['projects'][0]['counts']['unconfirmed_worker_tasks'],0)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT state FROM bridge_tasks WHERE id=?',(task['id'],)).fetchone()[0],'running')

    def test_unit_generator_writes_only_private_review_file(self):
        draft=UnitDraft('/opt/taesan/app','/opt/taesan/venv/bin/python','/var/lib/taesan/governance.json','/var/lib/taesan')
        value=render_unit(draft)
        self.assertIn('KillSignal=SIGTERM',value);self.assertIn('KillMode=control-group',value)
        self.assertNotIn('ExecStop=',value);self.assertNotIn('systemctl',value)
        output=self.root/'draft.service';receipt=write_unit_draft(draft,output)
        self.assertFalse(receipt['installed']);self.assertFalse(receipt['started'])
        self.assertEqual(output.stat().st_mode&0o777,0o600)
        with self.assertRaises(FileExistsError):write_unit_draft(draft,output)
        for path in ('/etc/systemd/system/example.service','/run/systemd/system/example.service','//etc/systemd/system/example.service','//run/systemd/system/example.service','relative-draft.service'):
            with self.assertRaises(ValueError):write_unit_draft(draft,path)
        for unsafe in ('/opt/app\nExecStart=/bin/sh','/opt/%i/app','/opt/../tmp','/opt/app path','//opt/taesan/app'):
            with self.assertRaises(ValueError):UnitDraft(unsafe,draft.python,draft.config_file,draft.state_directory)

    def test_unit_cli_generates_draft_without_reading_private_config(self):
        import json
        import subprocess
        import sys
        private=self.root/'private.json';private.write_text('DO_NOT_COPY_PRIVATE_CONTENT');private.chmod(0o600)
        output=self.root/'cli-draft.service'
        script=Path(__file__).resolve().parents[2]/'scripts/prepare_governance_unit.py'
        result=subprocess.run([sys.executable,str(script),'--application-root','/opt/taesan/app','--python','/opt/taesan/venv/bin/python',
            '--config-file',str(private),'--state-directory','/var/lib/taesan','--output',str(output)],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        self.assertEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stdout)['state'],'unit_draft_created')
        self.assertNotIn(b'DO_NOT_COPY_PRIVATE_CONTENT',result.stdout+result.stderr+output.read_bytes())
        self.assertFalse(json.loads(result.stdout)['installed'])

    def test_fixture_service_graceful_stop_force_kill_restart_and_drain(self):
        context=multiprocessing.get_context('spawn');events=context.Queue();transition=RuntimeTransition(self.core)
        def launch(mode):
            child=context.Process(target=_synthetic_service,args=(str(self.store.path),self.owner,events,mode))
            child.start();self.assertEqual(events.get(timeout=5),'started');return child
        child=launch('wait')
        child.terminate();self.assertEqual(events.get(timeout=5),'graceful_exit');child.join(timeout=5)
        self.assertEqual(child.exitcode,0)
        child=launch('wait');os.kill(child.pid,signal.SIGKILL);child.join(timeout=5)
        self.assertLess(child.exitcode,0)
        # A crash releases only the local manager guard; it proves no worker stop.
        child=launch('drain_then_exit')
        transition.drain(self.owner,['project'],{'project':0},'drain-child')
        self.assertEqual(events.get(timeout=5),'draining');self.assertEqual(events.get(timeout=5),'graceful_exit')
        child.join(timeout=5);self.assertEqual(child.exitcode,0)
        self.assertFalse(transition.status(self.owner,['project'])['workers_stopped_verified'])


if __name__=='__main__':unittest.main()
