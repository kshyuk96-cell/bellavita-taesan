import json
from pathlib import Path
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_managed_service_launch as fixtures
from manager_bridge.managed_launch_preflight import inspect_managed_launch,CHECKS
from scripts import run_governance_service as cli


class ManagedLaunchPreflightTests(unittest.TestCase):
    setUp=fixtures.ManagedServiceLaunchTests.setUp
    observe=fixtures.ManagedServiceLaunchTests.observe
    verify=fixtures.ManagedServiceLaunchTests.verify
    auth=fixtures.ManagedServiceLaunchTests.auth
    setup_runtime=fixtures.ManagedServiceLaunchTests.setup_runtime
    setup_files=fixtures.ManagedServiceLaunchTests.setup_files
    write_files=fixtures.ManagedServiceLaunchTests.write_files

    def setup_complete(self):
        self.setup_files()
        manager=self.root/'manager.token';manager.write_text(self.manager);manager.chmod(0o600)
        self.hostconfig.update(version=1,application_root=str(Path(__file__).resolve().parents[2]),expected_source_commit='a'*40,data_root=str(self.root),database=str(self.store.path),manager_token_file=str(manager),instance_id='preflight')
        self.entry.update(checks={'fixture-check':['/usr/bin/true']},developer_token_file=str(self.paths['dev']),reviewer_token_file=str(self.paths['reviewer']))
        self.write_files()

    def inspect(self):
        with patch('manager_bridge.managed_launch_preflight._git',side_effect=lambda root,*args:b'a'*40 if args[0]=='rev-parse' else b''):
            return inspect_managed_launch(self.servicepath,self.subpath)

    def test_ten_metadata_checks_never_authorize_execution(self):
        self.setup_complete();result=self.inspect()
        self.assertEqual(list(CHECKS),[c['check'] for c in result['checks']])
        self.assertEqual(10,result['passed_checks']);self.assertTrue(result['configuration_preflight_passed'])
        for key in ('authentication_verified','scheduler_running_verified','execution_authorized','credential_file_contents_inspected','database_contents_read'):
            self.assertFalse(result[key])
        encoded=json.dumps(result)
        for secret in (self.owner,self.dev,self.manager,str(self.root),'synthetic-access'):self.assertNotIn(secret,encoded)

    def test_missing_configuration_has_ten_blocked_entries(self):
        self.setup_complete();self.subpath.unlink();result=self.inspect()
        self.assertEqual(10,len(result['checks']));self.assertEqual(0,result['passed_checks']);self.assertFalse(result['configuration_preflight_passed'])

    def test_each_structural_failure_identified(self):
        mutations=[('schedule',lambda case:case.serviceconfig.update(supervision={'subscription_interval':1})),
            ('project_scope',lambda case:case.hostconfig.update(projects=[case.entry,case.entry])),
            ('role_pairs',lambda case:case.entry.update(reviewer_id='dev')),
            ('profile_directories',lambda case:case.entry['reviewer'].update(codex_home=str(case.devhome))),
            ('credential_file_metadata',lambda case:case.paths['dev'].chmod(0o644)),
            ('workspace_separation',lambda case:case.entry.update(source=str(case.canonical.parent))),
            ('native_executables',lambda case:case.entry['developer'].update(binary='/not-an-executable')),
            ('state_storage',lambda case:case.hostconfig.update(database=str(case.ownerpath)))]
        for expected,mutation in mutations:
            with self.subTest(check=expected):
                case=ManagedLaunchPreflightTests('runTest');case.setUp()
                try:
                    case.setup_complete();mutation(case);case.write_files();result=case.inspect()
                    self.assertEqual('blocked',next(x for x in result['checks'] if x['check']==expected)['state'])
                    self.assertFalse(result['configuration_preflight_passed'])
                finally:case.doCleanups()

    def test_application_dirty_or_wrong_pin(self):
        self.setup_complete()
        with patch('manager_bridge.managed_launch_preflight._git',return_value=b'wrong'):
            result=inspect_managed_launch(self.servicepath,self.subpath)
        self.assertEqual('blocked',next(x for x in result['checks'] if x['check']=='application_pin')['state'])

    def test_config_change_during_observation_blocks_overall(self):
        self.setup_complete()
        def changed(root,*args):
            self.subconfig['broker_ref']='changed';self.write_files()
            return b'a'*40 if args[0]=='rev-parse' else b''
        with patch('manager_bridge.managed_launch_preflight._git',side_effect=changed):
            result=inspect_managed_launch(self.servicepath,self.subpath)
        self.assertFalse(result['configuration_stable']);self.assertFalse(result['configuration_preflight_passed'])

    def test_cli_preflight_never_constructs_service(self):
        result={'configuration_preflight_passed':True}
        with patch('manager_bridge.managed_launch_preflight.inspect_managed_launch',return_value=result),patch('manager_bridge.managed_service_launch.load_managed_service',side_effect=AssertionError('service constructed')),patch('builtins.print'):
            self.assertEqual(0,cli.main(['--config','/private/service','--subscription-config','/private/sub','--preflight']))
        with patch('manager_bridge.managed_launch_preflight.inspect_managed_launch',return_value={'configuration_preflight_passed':False}),patch('builtins.print'):
            self.assertEqual(2,cli.main(['--config','/private/service','--subscription-config','/private/sub','--preflight']))

    def test_inspection_never_reads_credentials_or_opens_database_or_socket(self):
        self.setup_complete()
        forbidden={self.ownerpath,self.paths['dev'],self.paths['reviewer'],self.root/'manager.token',self.store.path}
        forbidden.update(p/'auth.json' for p in (self.canonical,self.devhome,self.reviewhome))
        original=Path.open
        def opened(path,*args,**kwargs):
            if path in forbidden:raise AssertionError('protected contents inspected')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',opened),patch('sqlite3.connect',side_effect=AssertionError('database opened')),patch('socket.socket',side_effect=AssertionError('socket opened')):
            result=self.inspect()
        self.assertTrue(result['configuration_preflight_passed'])
