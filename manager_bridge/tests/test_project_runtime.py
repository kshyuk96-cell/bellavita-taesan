from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from manager_bridge.agent_directory import AgentDirectory, AgentObservation, Connector
from manager_bridge.maintenance_registry import Registry
from manager_bridge.project_runtime import (BubblewrapTests, CHECK_COMMANDS, CONTROL_COMMANDS,
    ControlChecks, ProjectReceiver, ProjectTestProfile, _archive_source)
from manager_bridge.store import Store, Conflict, PermissionDenied, token_hash


class FixtureEngine:
    def __init__(self, available=True, execute=None):
        self.available, self.callback, self.calls = available, execute, []
    def probe(self):
        return {'available': self.available, 'state': 'ready' if self.available else 'sandbox_unavailable',
                'isolation_verified': self.available}
    def execute(self, profile, checks, heartbeat):
        self.calls.append((profile.project, checks)); heartbeat()
        if self.callback: return self.callback(profile, checks, heartbeat)
        return [{'name': name, 'passed': True} for name, _ in checks]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp); self.source = self.root/'source'; self.source.mkdir()
        self.runtime = self.root/'runtime'; self.runtime.mkdir()
        (self.source/'fixture.txt').write_text('source fixture only')
        self.git('init'); self.git('add','.'); self.git('commit','-m','Fixture source')
        self.sha = self.git('rev-parse','HEAD'); self.now = 1000.0
        self.store = Store(self.root/'private.sqlite3', clock=lambda:self.now)
        self.owner = self.store.add_token('owner','owner',['*'])
        self.registry = Registry(self.store)
        self.config = {'company':'bellavita','name':'Fixture Shop','source_kind':'github',
            'source_ref':'fixture/shop','branch':'main','services':[],'maintenance_enabled':True}
        self.registry.register(self.owner,'bellavita-shop',self.config)
        self.registry.register(self.owner,'hope-sunrise',{**self.config,'company':'hope-sunrise','source_ref':'fixture/hope'})
        self.manager = self.store.add_token('manager','manager',['bellavita-shop'])
        self.worker = self.store.add_token('worker','worker',['bellavita-shop'])
        self.other = self.store.add_token('other','worker',['hope-sunrise'])
        self.directory = AgentDirectory(self.store,{('bellavita-shop','agent'):Connector(lambda *_:
            AgentObservation('bellavita-shop','agent','session',token_hash(self.worker),True,
                             'fixture/ready',self.now,self.now+100))})
        self.directory.register(self.owner,'bellavita-shop','agent',token_hash(self.worker),'session',enabled=True)
        self.directory.probe(self.owner,'bellavita-shop','agent')
        self.profile = ProjectTestProfile('bellavita-shop','fixture/shop',self.sha,self.source,self.runtime)
        self.engine = FixtureEngine(); self.receiver = ProjectReceiver(self.store,self.directory,self.profile,engine=self.engine)
        self.sequence = 0

    def git(self,*args):
        return subprocess.check_output(['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','user.name=Fixture',
            '-c','user.email=fixture@example.invalid','-C',str(self.source),*args],stderr=subprocess.DEVNULL,text=True).strip()

    def task(self, **changes):
        self.sequence += 1
        manifest = {'project':'bellavita-shop','repository':'fixture/shop','base_sha':self.sha,
            'description':'untrusted description cannot set commands or send messages',
            'checks':['python_tests'],'kind':'test'}
        manifest.update(changes)
        task = self.store.submit(self.owner,'task-'+str(self.sequence),manifest)
        task = self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'])
        delivery = self.directory.enqueue(self.owner,'bellavita-shop','agent','delivery-'+str(self.sequence),
            task['id'],task['version'],task['manifest_digest'])
        return task, delivery

    def control(self):
        path = self.root/'control'; path.mkdir()
        self.git_control = lambda *args: subprocess.check_output(['/usr/bin/git','-c','core.hooksPath=/dev/null',
            '-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-C',str(path),*args],
            stderr=subprocess.DEVNULL,text=True).strip()
        tests = path/'manager_bridge'/'tests'; tests.mkdir(parents=True)
        for name in ('test_agent_directory','test_representative_worker','test_delegation','test_delegation_batch'):
            (tests/(name+'.py')).write_text('# synthetic manager source fixture\n')
        self.git_control('init'); self.git_control('add','.'); self.git_control('commit','-m','Control fixture')
        return ControlChecks(path,self.git_control('rev-parse','HEAD'),self.runtime)

    def test_fixed_commands_match_all_four_sources(self):
        self.assertEqual(set(CHECK_COMMANDS),{'bellavita-shop','bellavita-erp','hope-sunrise','funeral-association'})
        for project, commands in CHECK_COMMANDS.items():
            for name, argv in commands.items():
                self.assertTrue(argv[0].startswith('/runtime/bin/'))
                self.assertNotIn('sh',argv); self.assertNotIn('db:migrate',argv)

    def test_erp_build_is_before_tests(self):
        profile = replace(self.profile,project='bellavita-erp')
        self.assertEqual([name for name,_ in profile.checks(['node_tests'])],['build','node_tests'])

    def test_unknown_check_and_duplicate_check_rejected(self):
        for value in (['curl https://example.invalid'],['python_tests','python_tests'],['db:migrate']):
            with self.subTest(value=value),self.assertRaises((ValueError,PermissionDenied)):
                self.profile.checks(value)

    def test_missing_control_profile_cannot_claim_auth_or_approval_coverage(self):
        with self.assertRaisesRegex(Conflict,'missing_required_control_checks'):
            self.profile.checks(['authorization_regression','approval_regression','python_tests'])

    def test_control_checks_are_distinct_pinned_manager_test_commands(self):
        control = self.control(); control.source_check()
        profile = replace(self.profile,control=control)
        checks = profile.checks(['authorization_regression','approval_regression','python_tests'])
        self.assertEqual(checks[0][1],CONTROL_COMMANDS['authorization_regression'])
        self.assertEqual(checks[1][1],CONTROL_COMMANDS['approval_regression'])
        self.assertNotEqual(checks[0][1],checks[1][1])
        self.assertTrue(checks[0][1][0].startswith('/control-runtime/'))

    def test_receiver_reports_only_worker_claim(self):
        task,_ = self.task(); result = self.receiver.run_once(self.worker,'agent')
        self.assertEqual(result['state'],'reported'); self.assertFalse(result['independently_verified'])
        self.assertEqual(self.store.get_task(self.owner,task['id'])['report']['head_sha'],self.sha)
        self.assertEqual(self.engine.calls[0][1],[('python_tests',CHECK_COMMANDS['bellavita-shop']['python_tests'])])

    def test_delegated_check_labels_do_not_become_general_suite_aliases(self):
        profile = replace(self.profile,control=self.control())
        receiver = ProjectReceiver(self.store,self.directory,profile,engine=self.engine)
        task,_ = self.task(checks=['authorization_regression','approval_regression','python_tests'])
        self.assertEqual(receiver.run_once(self.worker,'agent')['state'],'reported')
        self.assertEqual([item['name'] for item in self.store.get_task(self.owner,task['id'])['report']['checks']],
                         ['authorization_regression','approval_regression','python_tests'])
        self.assertNotIn(self.worker,json.dumps(self.engine.calls))

    def test_sandbox_unavailable_does_not_claim_or_execute(self):
        task,_ = self.task(); self.engine.available = False
        result = self.receiver.run_once(self.worker,'agent')
        self.assertEqual(result['state'],'sandbox_unavailable'); self.assertFalse(result['execution_started'])
        self.assertEqual(self.store.get_task(self.owner,task['id'])['state'],'queued')
        self.assertEqual(self.engine.calls,[])

    def test_foreign_worker_or_manager_never_reaches_engine(self):
        self.task()
        for token in (self.manager,self.other):
            with self.assertRaises((PermissionDenied,LookupError)):
                self.receiver.run_once(token,'agent')
        self.assertEqual(self.engine.calls,[])

    def test_project_disabled_before_claim_rejects(self):
        task,_ = self.task()
        self.registry.register(self.owner,'bellavita-shop',{**self.config,'maintenance_enabled':False},expected_revision=1)
        with self.assertRaises(PermissionDenied): self.receiver.run_once(self.worker,'agent')
        self.assertEqual(self.store.get_task(self.owner,task['id'])['state'],'queued')

    def test_source_change_before_claim_rejects(self):
        task,_ = self.task(); (self.source/'fixture.txt').write_text('changed')
        with self.assertRaises(Conflict): self.receiver.run_once(self.worker,'agent')
        self.assertEqual(self.engine.calls,[])

    def test_development_description_is_not_executed(self):
        task,_ = self.task(kind='development')
        result = self.receiver.run_once(self.worker,'agent')
        self.assertEqual(result['state'],'needs_verification'); self.assertEqual(self.engine.calls,[])
        self.assertIsNone(self.store.get_task(self.owner,task['id'])['report'])

    def test_timeout_is_uncertain_and_not_replayed(self):
        task,_ = self.task()
        def timeout(*_): raise TimeoutError('private output never retained')
        self.engine.callback = timeout
        self.assertEqual(self.receiver.run_once(self.worker,'agent')['state'],'needs_verification')
        self.assertEqual(self.receiver.run_once(self.worker,'agent')['state'],'needs_verification')
        self.assertEqual(len(self.engine.calls),1)
        self.assertEqual(self.receiver.recovery(self.worker,'agent')['tasks'][0]['task_id'],task['id'])

    def test_revocation_mid_run_stops_heartbeat_and_report(self):
        task,_ = self.task()
        def revoked(profile,checks,heartbeat):
            self.store.revoke_token(self.owner,token_hash(self.worker)); heartbeat()
            self.fail('revoked worker continued')
        self.engine.callback = revoked
        self.assertEqual(self.receiver.run_once(self.worker,'agent')['state'],'needs_verification')
        self.assertIsNone(self.store.get_task(self.owner,task['id'])['report'])

    def test_project_disable_mid_run_stops_heartbeat_and_report(self):
        task,_ = self.task()
        def disabled(profile,checks,heartbeat):
            self.registry.register(self.owner,'bellavita-shop',{**self.config,'maintenance_enabled':False},expected_revision=1)
            heartbeat(); self.fail('disabled project continued')
        self.engine.callback = disabled
        self.assertEqual(self.receiver.run_once(self.worker,'agent')['state'],'needs_verification')
        self.assertIsNone(self.store.get_task(self.owner,task['id'])['report'])
        self.assertEqual(len(self.receiver.recovery(self.worker,'agent')['tasks']),1)

    def test_cancel_mid_run_retains_stop_proof_requirement(self):
        task,delivery = self.task()
        def cancel(profile,checks,heartbeat):
            self.directory.control(self.manager,delivery['id'],action='cancel'); heartbeat()
            self.fail('cancelled work continued')
        self.engine.callback = cancel
        self.assertEqual(self.receiver.run_once(self.worker,'agent')['state'],'needs_verification')
        recovered = self.receiver.recovery(self.worker,'agent')
        self.assertEqual(recovered['tasks'][0]['task_state'],'cancelled')
        self.assertFalse(recovered['tasks'][0]['stop_confirmed']); self.assertFalse(recovered['automatically_replayed'])

    def test_missing_result_not_accepted(self):
        self.task(); self.engine.callback = lambda *_:[]
        self.assertEqual(self.receiver.run_once(self.worker,'agent')['state'],'needs_verification')

    def test_foreign_check_result_not_accepted(self):
        self.task(); self.engine.callback = lambda *_:[{'name':'arbitrary','passed':True}]
        self.assertEqual(self.receiver.run_once(self.worker,'agent')['state'],'needs_verification')

    def test_nonboolean_result_not_accepted(self):
        self.task(); self.engine.callback = lambda *_:[{'name':'python_tests','passed':'yes'}]
        self.assertEqual(self.receiver.run_once(self.worker,'agent')['state'],'needs_verification')

    def test_source_archive_excludes_untracked_metadata(self):
        (self.source/'.gitignore').write_text('private/\n')
        self.git('add','.gitignore'); self.git('commit','-m','Ignore private fixture')
        private = self.source/'private'; private.mkdir(); (private/'secret.txt').write_text('fake-private-sentinel')
        profile = replace(self.profile,base_sha=self.git('rev-parse','HEAD'))
        destination = self.root/'archived'; destination.mkdir()
        _archive_source(profile,destination)
        self.assertTrue((destination/'fixture.txt').is_file())
        self.assertFalse((destination/'.git').exists()); self.assertFalse((destination/'private').exists())

    def test_real_engine_probe_has_no_fallback_and_no_raw_output(self):
        with patch('manager_bridge.project_runtime.subprocess.run',return_value=subprocess.CompletedProcess([],1,stdout='private',stderr='private')):
            result = BubblewrapTests().probe()
        self.assertEqual(result,{'available':False,'state':'sandbox_unavailable','isolation_verified':False})
        self.assertNotIn('private',json.dumps(result))

    def test_engine_argv_namespace_and_credentials_are_fixed(self):
        base = BubblewrapTests()._base()
        self.assertIn('--unshare-all',base); self.assertIn('--clearenv',base)
        self.assertNotIn(self.worker,' '.join(base)); self.assertNotIn(str(self.root),' '.join(base))
        self.assertNotIn('--share-net',base)

    def test_unavailable_engine_never_archives_or_starts_child(self):
        engine = BubblewrapTests()
        with patch.object(engine,'probe',return_value={'available':False}),patch('manager_bridge.project_runtime.subprocess.Popen') as child:
            with self.assertRaises(Conflict): engine.execute(self.profile,self.profile.checks(['python_tests']),lambda:None)
            child.assert_not_called()

    def test_timeout_terminates_actual_process_group_interface(self):
        class Child:
            pid = 12345
            returncode = None
            def poll(self): return self.returncode
            def wait(self,timeout=None): self.returncode = -15; return self.returncode
        child = Child(); engine = BubblewrapTests()
        with patch.object(engine,'probe',return_value={'available':True}), \
             patch('manager_bridge.project_runtime._archive_source'), \
             patch('manager_bridge.project_runtime.subprocess.Popen',return_value=child) as popen, \
             patch('manager_bridge.project_runtime.time.monotonic',side_effect=[0,10000]), \
             patch('manager_bridge.project_runtime.os.killpg') as stop:
            with self.assertRaises(TimeoutError):
                engine.execute(self.profile,self.profile.checks(['python_tests']),lambda:None)
            stop.assert_called_once()
            self.assertEqual(stop.call_args.args[0],12345)
            self.assertEqual(popen.call_args.kwargs['env'],{'PATH':'/usr/bin:/bin'})
            self.assertNotIn(self.worker,json.dumps(popen.call_args.args))

    def test_revocation_heartbeat_terminates_child(self):
        class Child:
            pid = 12345
            returncode = None
            def poll(self): return self.returncode
            def wait(self,timeout=None): self.returncode = -15; return self.returncode
        calls = []
        def heartbeat():
            calls.append(True)
            if len(calls) > 1: raise PermissionDenied('revoked')
        engine = BubblewrapTests()
        with patch.object(engine,'probe',return_value={'available':True}), \
             patch('manager_bridge.project_runtime._archive_source'), \
             patch('manager_bridge.project_runtime.subprocess.Popen',return_value=Child()), \
             patch('manager_bridge.project_runtime.os.killpg') as stop:
            with self.assertRaises(PermissionDenied):
                engine.execute(self.profile,self.profile.checks(['python_tests']),heartbeat)
            stop.assert_called_once()

    def test_readiness_connector_cannot_attest_disabled_project(self):
        connector = self.receiver.connector(token_hash(self.worker),'session')
        self.assertTrue(connector.observe('bellavita-shop','agent','session').ready)
        self.registry.register(self.owner,'bellavita-shop',{**self.config,'maintenance_enabled':False},expected_revision=1)
        self.assertFalse(connector.observe('bellavita-shop','agent','session').ready)

    def test_restart_after_claim_before_ack_is_not_reexecuted(self):
        task,_ = self.task()
        self.store.claim(self.worker,'bellavita-shop',task_id=task['id'])
        result = self.receiver.run_once(self.worker,'agent')
        self.assertEqual(result['state'],'needs_verification')
        self.assertEqual(self.engine.calls,[])

    def test_cli_requires_existing_private_registered_state(self):
        import importlib.util
        from contextlib import redirect_stdout
        spec = importlib.util.spec_from_file_location('receiver_cli',Path(__file__).resolve().parents[2]/'scripts'/'run_project_receiver.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        missing = self.root/'missing.sqlite3'
        with redirect_stdout(io.StringIO()) as output:
            result = module.main(['--database',str(missing),'--token-file',str(self.root/'none'),
                                  '--profile',str(self.root/'none'),'--agent','agent'])
        self.assertEqual(result,1); self.assertFalse(missing.exists())
        self.assertFalse(json.loads(output.getvalue())['execution_started'])
