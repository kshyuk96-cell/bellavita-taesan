from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from manager_bridge.maintenance_registry import Registry
from manager_bridge.maintenance_checks import (CheckHost,Checks,DependencyReview,
    DependencySnapshot,LocalFixtureExecutor)
from manager_bridge.regression_selection import RegressionSelector
from manager_bridge.store import Store,Conflict,PermissionDenied,NotFound,digest


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=1000
        self.store=Store(self.root/'private.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        self.config={'company':'bellavita','name':'가상 ERP','source_kind':'github','source_ref':'fixture/erp',
                     'branch':'main','services':[],'maintenance_enabled':True}
        self.registry.register(self.owner,'erp',self.config)
        self.registry.register(self.owner,'other',dict(self.config,company='other',source_ref='fixture/other'))
        self.manager=self.store.add_token('manager','manager',['erp'])
        self.worker=self.store.add_token('worker','worker',['erp'])
        self.other=self.store.add_token('other','manager',['other'])
        self.checkout=self.root/'fixture';self.checkout.mkdir()
        self.sha='a'*40
        self.checks={'authorization_regression':(sys.executable,'-c','pass'),
                     'approval_regression':(sys.executable,'-c','pass'),
                     'business':(sys.executable,'-c','pass')}
        self.selector=RegressionSelector(self.checks,{'src/':['business']})
        self.calls=[]
        self.result={'passed':True,'timed_out':False,'reference':'fixture/ok','duration_seconds':0.1}
        self.host=CheckHost(self.checkout,self.sha,self.selector,executor=self.execute,
                            observe_source=lambda _:self.sha)
        self.service=Checks(self.store,self.registry,{'erp':self.host})

    def execute(self,*args):
        self.calls.append(args)
        return dict(self.result)

    def proposal(self,key='request',paths=('src/a.py',)):
        return self.service.propose(self.manager,'erp',key,list(paths))

    def approved(self,key='request'):
        p=self.proposal(key)
        self.service.approve(self.owner,p['id'],p['fingerprint'])
        return p

    def test_registered_selection_always_includes_auth_approval(self):
        p=self.proposal();names=[x['name'] for x in p['plan']['selection']['checks']]
        self.assertEqual(names[:2],['authorization_regression','approval_regression'])
        self.assertIn('business',names)
        self.assertEqual(p['state'],'pending_approval');self.assertEqual(self.calls,[])

    def test_unknown_paths_full_fallback(self):
        self.assertTrue(self.proposal(paths=('unknown.py',))['plan']['selection']['full_fallback_selected'])

    def test_unapproved_does_not_execute(self):
        p=self.proposal()
        with self.assertRaises(PermissionDenied):self.service.run(self.manager,p['id'])
        self.assertFalse(self.calls)

    def test_only_owner_approves_exact_digest(self):
        p=self.proposal()
        with self.assertRaises(PermissionDenied):self.service.approve(self.manager,p['id'],p['fingerprint'])
        with self.assertRaises(Conflict):self.service.approve(self.owner,p['id'],'b'*64)
        self.assertFalse(self.calls)

    def test_cross_project_access_denied(self):
        p=self.approved()
        for callback in (lambda:self.service.get(self.other,p['id']),lambda:self.service.run(self.other,p['id'])):
            with self.assertRaises(NotFound):callback()
        self.assertFalse(self.calls)

    def test_approved_run_and_idempotent_replay(self):
        p=self.approved();first=self.service.run(self.worker,p['id']);second=self.service.run(self.worker,p['id'])
        self.assertEqual(first,second);self.assertEqual(first['state'],'passed')
        self.assertEqual(len(self.calls),3)
        self.assertFalse(first['operational_change'])

    def test_worker_without_test_capability_cannot_execute(self):
        worker=self.store.add_token('developer-only','worker',['erp'],capabilities=('development',))
        p=self.approved()
        with self.assertRaises(PermissionDenied):self.service.run(worker,p['id'])
        self.assertEqual(self.calls,[])

    def test_executor_token_reference_rejected_without_leak(self):
        self.result['reference']=self.worker
        p=self.approved();result=self.service.run(self.worker,p['id'])
        self.assertEqual(result['state'],'uncertain')
        self.assertNotIn(self.worker,str(result))

    def test_source_change_before_approval_rejected(self):
        p=self.proposal();self.sha='b'*40
        with self.assertRaises(Conflict):self.service.approve(self.owner,p['id'],p['fingerprint'])

    def test_source_change_after_approval_rejected(self):
        p=self.approved();self.sha='b'*40
        with self.assertRaises(Conflict):self.service.run(self.worker,p['id'])
        self.assertFalse(self.calls)

    def test_configuration_change_invalidates_approval(self):
        p=self.approved();self.registry.register(self.owner,'erp',dict(self.config,name='updated'),expected_revision=1)
        with self.assertRaises(Conflict):self.service.run(self.worker,p['id'])

    def test_changed_command_configuration_invalidates_approval(self):
        p=self.approved()
        self.host.selector.checks['business']=(sys.executable,'-c','raise RuntimeError()')
        with self.assertRaises(Conflict):self.service.run(self.worker,p['id'])

    def test_disabled_not_executed(self):
        self.registry.register(self.owner,'erp',dict(self.config,maintenance_enabled=False),expected_revision=1)
        p=self.approved();self.assertEqual(self.service.run(self.worker,p['id'])['state'],'disabled')
        self.assertFalse(self.calls)

    def test_missing_executor_not_claimed(self):
        self.service=Checks(self.store,self.registry,{'erp':replace(self.host,executor=None)})
        p=self.approved();self.assertEqual(self.service.run(self.worker,p['id'])['state'],'not_configured')
        self.assertEqual(self.service.get(self.manager,p['id'])['state'],'approved')

    def test_unregistered_project_host_not_configured(self):
        self.assertEqual(Checks(self.store,self.registry).propose(self.manager,'erp','key',[])['state'],'not_configured')

    def test_request_key_content_conflict(self):
        p=self.proposal();self.assertEqual(p,self.proposal())
        with self.assertRaises(Conflict):self.proposal(paths=('other.py',))

    def test_executor_exception_is_uncertain_and_not_retried(self):
        def failed(*_):self.calls.append(True);raise RuntimeError('credential=private')
        self.service=Checks(self.store,self.registry,{'erp':replace(self.host,executor=failed)})
        p=self.approved();first=self.service.run(self.worker,p['id'])
        self.assertEqual(first['state'],'uncertain');self.assertNotIn('credential',str(first))
        self.assertEqual(self.service.run(self.worker,p['id']),first);self.assertEqual(len(self.calls),1)

    def test_malformed_executor_cannot_claim_success(self):
        for value in ({'passed':True},{**self.result,'passed':1},{**self.result,'duration_seconds':float('nan')}):
            with self.subTest(value=value):
                self.service=Checks(self.store,self.registry,{'erp':replace(self.host,executor=lambda *_:value)})
                p=self.approved('key-'+str(len(self.calls))+str(len(str(value))))
                result=self.service.run(self.worker,p['id'])
                self.assertIn(result['state'],('uncertain','busy'))

    def test_failure_is_not_success(self):
        self.result['passed']=False
        p=self.approved();self.assertEqual(self.service.run(self.worker,p['id'])['state'],'failed')

    def test_source_change_during_execution_uncertain(self):
        def change(*args):self.calls.append(args);self.sha='b'*40;return dict(self.result)
        self.service=Checks(self.store,self.registry,{'erp':replace(self.host,executor=change)})
        p=self.approved();self.assertEqual(self.service.run(self.worker,p['id'])['state'],'uncertain')
        self.assertEqual(len(self.calls),1)

    def test_revoke_during_execution_no_report_retrieval(self):
        workerhash=self.store.authenticate(self.worker)['token_hash']
        def revoke(*args):self.store.revoke_token(self.owner,workerhash);return dict(self.result)
        self.service=Checks(self.store,self.registry,{'erp':replace(self.host,executor=revoke)})
        p=self.approved()
        with self.assertRaises(PermissionDenied):self.service.run(self.worker,p['id'])
        # Authorized manager can see the honest uncertain result after revoked worker returns.
        self.assertEqual(self.service.get(self.manager,p['id'])['result']['state'],'uncertain')

    def test_two_workers_same_plan_execute_once(self):
        entered=threading.Event();release=threading.Event()
        def blocked(*args):entered.set();release.wait(5);return dict(self.result)
        self.service=Checks(self.store,self.registry,{'erp':replace(self.host,executor=blocked)})
        p=self.approved()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.service.run,self.worker,p['id']);self.assertTrue(entered.wait(2))
            second=pool.submit(self.service.run,self.worker,p['id']).result()
            self.assertEqual(second['state'],'uncertain');release.set()
            self.assertEqual(first.result()['state'],'passed')

    def test_distinct_plans_same_repository_do_not_overlap(self):
        entered=threading.Event();release=threading.Event()
        def blocked(*args):entered.set();release.wait(5);return dict(self.result)
        self.service=Checks(self.store,self.registry,{'erp':replace(self.host,executor=blocked)})
        first=self.approved('first');second=self.approved('second')
        with ThreadPoolExecutor(max_workers=2) as pool:
            running=pool.submit(self.service.run,self.worker,first['id']);self.assertTrue(entered.wait(2))
            other=self.service.run(self.worker,second['id']);self.assertEqual(other['state'],'busy')
            release.set();self.assertEqual(running.result()['state'],'passed')
        self.assertEqual(self.service.run(self.worker,second['id'])['state'],'passed')

    def test_process_loss_running_not_replayed_after_restart(self):
        p=self.approved()
        with self.store._transaction() as c:c.execute("UPDATE maintenance_check_plans SET state='running' WHERE id=?",(p['id'],))
        service=Checks(self.store,self.registry,{'erp':self.host})
        self.assertEqual(service.run(self.worker,p['id'])['state'],'uncertain');self.assertFalse(self.calls)

    def test_fixture_executor_minimal_environment_and_no_shell(self):
        marker=self.checkout/'.taesan-synthetic-fixture';marker.write_text('synthetic-only')
        executor=LocalFixtureExecutor(self.root)
        old=os.environ.get('OPENAI_API_KEY');os.environ['OPENAI_API_KEY']='synthetic-private-sentinel'
        try:
            result=executor('env-check',(sys.executable,'-c',"import os;assert 'OPENAI_API_KEY' not in os.environ;assert 'TAESAN_FIXTURE_ONLY' in os.environ;print('private output')"),self.checkout,2)
        finally:
            if old is None:os.environ.pop('OPENAI_API_KEY',None)
            else:os.environ['OPENAI_API_KEY']=old
        self.assertTrue(result['passed']);self.assertNotIn('private',str(result))

    def test_fixture_executor_rejects_unmarked_or_foreign_worktree(self):
        executor=LocalFixtureExecutor(self.root)
        with self.assertRaises(PermissionDenied):executor('check',(sys.executable,'-c','pass'),self.checkout,1)
        foreign=Path(self.enterContext(tempfile.TemporaryDirectory()));(foreign/'.taesan-synthetic-fixture').write_text('synthetic-only')
        with self.assertRaises(PermissionDenied):executor('check',(sys.executable,'-c','pass'),foreign,1)

    def test_parent_path_traversal_rejected(self):
        with self.assertRaises(ValueError):CheckHost(self.checkout/'..'/'fixture',self.sha,self.selector)
        with self.assertRaises(ValueError):LocalFixtureExecutor(self.checkout/'..')

    def test_fixture_timeout_is_failure(self):
        (self.checkout/'.taesan-synthetic-fixture').write_text('synthetic-only')
        result=LocalFixtureExecutor(self.root)('timeout',(sys.executable,'-c','import time;time.sleep(30)'),self.checkout,1)
        self.assertFalse(result['passed']);self.assertTrue(result['timed_out'])

    def test_fixture_descendants_cleaned_after_parent_exits(self):
        (self.checkout/'.taesan-synthetic-fixture').write_text('synthetic-only')
        code="import subprocess,sys,pathlib;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);pathlib.Path('child.pid').write_text(str(p.pid))"
        result=LocalFixtureExecutor(self.root)('descendants',(sys.executable,'-c',code),self.checkout,2)
        self.assertTrue(result['passed'])
        pid=int((self.checkout/'child.pid').read_text())
        def stopped():
            path=Path('/proc')/str(pid)/'stat'
            return not path.exists() or path.read_text().split()[2]=='Z'
        end=time.monotonic()+2
        while not stopped() and time.monotonic()<end:time.sleep(.01)
        self.assertTrue(stopped())

    def test_real_clean_git_source_verified(self):
        checkout=self.root/'git-fixture';checkout.mkdir()
        env=dict(os.environ,GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_SYSTEM='/dev/null')
        for args in (['init'],['config','user.name','Fixture'],['config','user.email','fixture@example.invalid']):
            subprocess.run(['/usr/bin/git','-C',str(checkout),*args],env=env,capture_output=True,check=True)
        (checkout/'test.txt').write_text('fixture')
        subprocess.run(['/usr/bin/git','-C',str(checkout),'add','test.txt'],env=env,check=True,capture_output=True)
        subprocess.run(['/usr/bin/git','-C',str(checkout),'commit','-m','fixture'],env=env,check=True,capture_output=True)
        sha=subprocess.check_output(['/usr/bin/git','-C',str(checkout),'rev-parse','HEAD'],text=True).strip()
        host=CheckHost(checkout,sha,self.selector)
        self.assertEqual(host.observed_sha(),sha)
        (checkout/'test.txt').write_text('changed')
        with self.assertRaises(Conflict):host.observed_sha()

    def core_task(self,*,key='core',base=None,checks=None,worker=None):
        manifest={'project':'erp','repository':'fixture/erp','base_sha':base or self.sha,
                  'description':'가상 유지보수 검증','checks':checks or list(self.checks),'kind':'test'}
        task=self.store.submit(self.manager,key,manifest)
        self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'])
        assignment=self.store.claim(worker or self.worker,'erp',lease_seconds=30,task_id=task['id'])
        self.assertIsNotNone(assignment)
        return assignment

    def test_active_core_task_blocks_standalone_check(self):
        p=self.approved();self.core_task()
        result=self.service.run(self.worker,p['id'])
        self.assertEqual(result['state'],'busy');self.assertEqual(self.calls,[])

    def test_reported_uncertain_and_cancel_unconfirmed_core_remain_reserved(self):
        for state in ('reported','needs_verification','cancelled'):
            with self.subTest(state=state):
                p=self.approved('check-'+state);task=self.core_task(key='core-'+state)
                with self.store._transaction() as c:
                    c.execute('UPDATE bridge_tasks SET state=? WHERE id=?',(state,task['id']))
                self.assertEqual(self.service.run(self.worker,p['id'])['state'],'busy')
                with self.store._transaction() as c:
                    c.execute("UPDATE bridge_tasks SET state='reviewed' WHERE id=?",(task['id'],))
        self.assertEqual(self.calls,[])

    def test_nested_assigned_parent_can_execute_exact_approved_checks(self):
        p=self.approved();task=self.core_task()
        result=self.service.run(self.worker,p['id'],parent_task_id=task['id'])
        self.assertEqual(result['state'],'passed')
        self.assertEqual(result['parent_task_id'],task['id'])
        self.assertEqual(len(self.calls),3)

    def test_manager_cannot_bypass_core_reservation_with_parent_id(self):
        p=self.approved();task=self.core_task()
        with self.assertRaises(PermissionDenied):
            self.service.run(self.manager,p['id'],parent_task_id=task['id'])
        self.assertFalse(self.calls)

    def test_wrong_worker_cannot_bypass_parent_reservation(self):
        p=self.approved();task=self.core_task()
        worker=self.store.add_token('other-worker','worker',['erp'])
        with self.assertRaises(PermissionDenied):
            self.service.run(worker,p['id'],parent_task_id=task['id'])
        self.assertFalse(self.calls)

    def test_expired_cancelled_and_reported_parent_cannot_nest(self):
        for state in ('expired','cancelled','reported'):
            with self.subTest(state=state):
                p=self.approved('check-'+state);task=self.core_task(key='core-'+state)
                with self.store._transaction() as c:
                    if state=='expired':c.execute('UPDATE bridge_tasks SET lease_expires=? WHERE id=?',(self.now-1,task['id']))
                    else:c.execute('UPDATE bridge_tasks SET state=? WHERE id=?',(state,task['id']))
                with self.assertRaises(PermissionDenied):self.service.run(self.worker,p['id'],parent_task_id=task['id'])
                with self.store._transaction() as c:c.execute("UPDATE bridge_tasks SET state='reviewed' WHERE id=?",(task['id'],))
        self.assertFalse(self.calls)

    def test_nested_check_requires_parent_check_scope(self):
        p=self.approved();task=self.core_task(checks=['business'])
        with self.assertRaises(PermissionDenied):self.service.run(self.worker,p['id'],parent_task_id=task['id'])
        self.assertFalse(self.calls)

    def test_nested_source_mismatch_blocked(self):
        p=self.approved();task=self.core_task(base='b'*40)
        with self.assertRaises(PermissionDenied):self.service.run(self.worker,p['id'],parent_task_id=task['id'])
        self.assertFalse(self.calls)

    def test_nested_parent_approval_must_remain_valid(self):
        p=self.approved();task=self.core_task()
        with self.store._transaction() as c:c.execute("UPDATE bridge_tasks SET approval='{}' WHERE id=?",(task['id'],))
        with self.assertRaises(PermissionDenied):self.service.run(self.worker,p['id'],parent_task_id=task['id'])
        self.assertFalse(self.calls)

    def test_expiry_mid_check_stops_remaining_and_keeps_uncertain(self):
        def expire(*args):self.calls.append(args);self.now+=31;return dict(self.result)
        self.service=Checks(self.store,self.registry,{'erp':replace(self.host,executor=expire)})
        p=self.approved();task=self.core_task()
        result=self.service.run(self.worker,p['id'],parent_task_id=task['id'])
        self.assertEqual(result['state'],'uncertain');self.assertEqual(len(self.calls),1)

    def test_parent_binding_cannot_be_reused_for_another_task(self):
        p=self.approved();task=self.core_task()
        first=self.service.run(self.worker,p['id'],parent_task_id=task['id'])
        self.assertEqual(first['state'],'passed')
        with self.store._transaction() as c:c.execute("UPDATE bridge_tasks SET state='reviewed' WHERE id=?",(task['id'],))
        second=self.core_task(key='core-new')
        with self.assertRaises(Conflict):self.service.run(self.worker,p['id'],parent_task_id=second['id'])
        self.assertEqual(len(self.calls),3)

    def test_other_core_task_on_same_repo_blocks_even_with_valid_parent(self):
        p=self.approved();task=self.core_task()
        # An independent old task with an unconfirmed stop reserves the same repo.
        manifest={'project':'erp','repository':'fixture/erp','base_sha':self.sha,
                  'description':'old fixture','checks':list(self.checks),'kind':'test'}
        old=self.store.submit(self.manager,'old-task',manifest)
        with self.store._transaction() as c:
            c.execute("UPDATE bridge_tasks SET state='needs_verification' WHERE id=?",(old['id'],))
        self.assertEqual(self.service.run(self.worker,p['id'],parent_task_id=task['id'])['state'],'busy')
        self.assertFalse(self.calls)

    def test_core_reservation_blocks_other_project_sharing_repository(self):
        self.registry.register(self.owner,'shared',dict(self.config,company='other-company'))
        manager=self.store.add_token('shared-manager','manager',['shared'])
        worker=self.store.add_token('shared-worker','worker',['shared'])
        manifest={'project':'shared','repository':'fixture/erp','base_sha':self.sha,
                  'description':'shared repository fixture','checks':list(self.checks),'kind':'test'}
        task=self.store.submit(manager,'shared-core',manifest)
        self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'])
        self.assertIsNotNone(self.store.claim(worker,'shared',task_id=task['id']))
        p=self.approved();self.assertEqual(self.service.run(self.worker,p['id'])['state'],'busy')
        self.assertFalse(self.calls)

    def test_active_check_blocks_new_native_claim(self):
        entered=threading.Event();release=threading.Event()
        def blocked(*args):entered.set();release.wait(5);return dict(self.result)
        self.service=Checks(self.store,self.registry,{'erp':replace(self.host,executor=blocked)})
        p=self.approved()
        manifest={'project':'erp','repository':'fixture/erp','base_sha':self.sha,
                  'description':'core fixture','checks':list(self.checks),'kind':'test'}
        task=self.store.submit(self.manager,'next-core',manifest)
        self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            running=pool.submit(self.service.run,self.worker,p['id']);self.assertTrue(entered.wait(2))
            self.assertIsNone(self.store.claim(self.worker,'erp',task_id=task['id']))
            release.set();self.assertEqual(running.result()['state'],'passed')
        self.assertIsNotNone(self.store.claim(self.worker,'erp',task_id=task['id']))

    def test_dependency_provider_absent_not_measured(self):
        result=DependencyReview(self.store,self.registry).inspect(self.manager,'erp',self.sha)
        self.assertEqual(result['state'],'not_measured')

    def snapshot(self,**kwargs):
        values={'project':'erp','source_sha':self.sha,'manifest_digest':'b'*64,'lock_digest':'c'*64,
                'advisories':({'package':'example','current':'1.0','proposed':'1.1','severity':'high','evidence':'advisory/1'},),
                'reference':'fixture/catalog','observed_at':self.now}
        values.update(kwargs);return DependencySnapshot(**values)

    def test_dependency_proposal_does_not_apply_or_claim_tests(self):
        result=DependencyReview(self.store,self.registry,{'erp':lambda *_:self.snapshot()}).inspect(self.manager,'erp',self.sha)
        self.assertEqual(result['state'],'proposal');self.assertFalse(result['packages_changed']);self.assertFalse(result['tests_passed'])

    def test_scoped_package_and_exact_prerelease_versions_supported(self):
        observed=self.snapshot(advisories=({'package':'@scope/example','current':'1.0.0-beta+1','proposed':'1.0.0','severity':'high','evidence':'advisory/1'},))
        result=DependencyReview(self.store,self.registry,{'erp':lambda *_:observed}).inspect(self.manager,'erp',self.sha)
        self.assertEqual(result['state'],'proposal')
        self.assertEqual(result['advisories'][0]['package'],'@scope/example')
        self.assertEqual(result['config_fingerprint'],self.registry.get(self.manager,'erp')['fingerprint'])

    def test_empty_advisories_distinct_from_not_measured(self):
        result=DependencyReview(self.store,self.registry,{'erp':lambda *_:self.snapshot(advisories=())}).inspect(self.manager,'erp',self.sha)
        self.assertEqual(result['state'],'observed_no_findings')

    def test_wrong_project_source_stale_and_failure_not_measured(self):
        for observed in (self.snapshot(project='other'),self.snapshot(source_sha='f'*40),self.snapshot(observed_at=1)):
            review=DependencyReview(self.store,self.registry,{'erp':lambda *_:observed},max_age=10)
            self.assertEqual(review.inspect(self.manager,'erp',self.sha)['state'],'not_measured')
        def failure(*_):raise RuntimeError('private')
        self.assertEqual(DependencyReview(self.store,self.registry,{'erp':failure}).inspect(self.manager,'erp',self.sha)['state'],'not_measured')

    def test_dependency_cross_project_denied(self):
        with self.assertRaises(NotFound):DependencyReview(self.store,self.registry).inspect(self.other,'erp',self.sha)

    def test_mutation_of_original_selector_does_not_change_registration(self):
        self.selector.checks['business']=(sys.executable,'-c','raise SystemExit(1)')
        p=self.approved();self.assertEqual(self.service.run(self.worker,p['id'])['state'],'passed')
