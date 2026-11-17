import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_managed_subscription_runtime as fixtures
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.managed_service_launch import load_managed_service
from scripts import run_governance_service as cli


class ManagedServiceLaunchTests(unittest.TestCase):
    setUp=fixtures.ManagedSubscriptionRuntimeTests.setUp
    observe=fixtures.ManagedSubscriptionRuntimeTests.observe
    verify=fixtures.ManagedSubscriptionRuntimeTests.verify
    auth=fixtures.ManagedSubscriptionRuntimeTests.auth
    setup_runtime=fixtures.ManagedSubscriptionRuntimeTests.setup_runtime

    def setup_files(self):
        self.setup_runtime()
        self.controller=ContinuousManager(self.core)
        for name in ('source','candidates','runtime','application'):
            p=self.root/name;p.mkdir(mode=0o700)
        self.hostpath=self.root/'host.json';self.servicepath=self.root/'service.json';self.subpath=self.root/'subscription.json'
        self.entry={'project':'project','repository':'fixture/project','source':str(self.root/'source'),
            'candidates':str(self.root/'candidates'),'runtime':str(self.root/'runtime'),
            'developer_id':'dev','reviewer_id':'reviewer','developer_token_file':str(self.paths['dev']),'reviewer_token_file':str(self.paths['reviewer']),'checks':{'fixture-check':['/bin/true']},
            'developer':{'binary':str(Path('/usr/bin/true').resolve()),'codex_home':str(self.devhome)},
            'reviewer':{'binary':str(Path('/usr/bin/true').resolve()),'codex_home':str(self.reviewhome)}}
        self.hostconfig={'application_root':str(self.root/'application'),'projects':[self.entry]}
        self.serviceconfig={'version':1,'host_config_file':str(self.hostpath),'listen':{'port':0},'governance':{},'supervision':{'subscription_interval':300}}
        self.subconfig={'version':1,'broker_ref':'fixture-broker','canonical_home':str(self.canonical),'owner_token_file':str(self.ownerpath)}
        self.write_files()

    def write_files(self):
        for p,value in ((self.hostpath,self.hostconfig),(self.servicepath,self.serviceconfig),(self.subpath,self.subconfig)):
            p.write_text(json.dumps(value));p.chmod(0o600)

    def loader(self,path,*,profile_guard_factory):
        self.guards=[profile_guard_factory(t.role,t.project,t.member_id,t.home,self.paths[t.member_id]) for t in self.targets]
        return self.host,self.controller,self.settings

    def load(self):
        with patch('manager_bridge.governance_service.load_host',side_effect=self.loader):
            return load_managed_service(self.servicepath,self.subpath)

    def test_load_fixed_refresher_without_invocation(self):
        self.setup_files()
        with patch('manager_bridge.subscription_broker.CodexSubscriptionRefresher.__call__',side_effect=AssertionError('must not refresh during load')):
            service=self.load()
        self.addCleanup(service.close)
        from manager_bridge.subscription_broker import CodexSubscriptionRefresher
        self.assertIsInstance(service.supervision.subscription_broker.refresh_callback,CodexSubscriptionRefresher)
        self.assertEqual(300,service.supervision.settings.subscription_interval)
        self.assertEqual(2,len(self.guards))
        self.assertEqual([],self.calls)

    def test_unknown_secret_or_callback_selector_denied(self):
        self.setup_files()
        for name in ('api_key','refresh_callback','endpoint'):
            self.subconfig[name]='untrusted-selector';self.write_files()
            with self.assertRaises(ValueError):self.load()
            del self.subconfig[name]

    def test_schedule_required_and_private_config(self):
        self.setup_files();self.serviceconfig.pop('supervision');self.write_files()
        with self.assertRaises(ValueError):self.load()
        self.serviceconfig['supervision']={'subscription_interval':300};self.write_files();self.subpath.chmod(0o644)
        with self.assertRaises(ValueError):self.load()

    def test_canonical_or_owner_cannot_be_exposed_to_workers(self):
        self.setup_files()
        for field,value in (('source',str(self.canonical.parent)),('candidates',str(self.ownerpath.parent)),('runtime',str(self.canonical))):
            old=self.entry[field];self.entry[field]=value;self.write_files()
            with self.assertRaises(ValueError):self.load()
            self.entry[field]=old

    def test_changed_config_closes_loaded_socket(self):
        self.setup_files();sentinel=SimpleNamespace(close=lambda:None)
        with patch('manager_bridge.managed_service_launch.load_service',return_value=sentinel) as load,patch.object(sentinel,'close') as close:
            def changed(*args,**kwargs):
                self.subconfig['broker_ref']='changed';self.write_files();return sentinel
            load.side_effect=changed
            with self.assertRaises(ValueError):load_managed_service(self.servicepath,self.subpath)
            close.assert_called_once()

    def test_cli_status_uses_managed_path_and_closes(self):
        fake=SimpleNamespace(status=lambda:{'state':'synthetic-status'},close=lambda:None,stop=lambda:None)
        with patch('manager_bridge.managed_service_launch.load_managed_service',return_value=fake) as load,patch.object(fake,'close') as close,patch('builtins.print') as output:
            self.assertEqual(0,cli.main(['--config','/private/service.json','--subscription-config','/private/subscription.json','--status']))
            load.assert_called_once_with(Path('/private/service.json'),Path('/private/subscription.json'))
            close.assert_called_once();self.assertIn('synthetic-status',output.call_args.args[0])

    def test_cli_failure_never_prints_provider_secret(self):
        with patch('manager_bridge.managed_service_launch.load_managed_service',side_effect=RuntimeError('private-secret')),patch('builtins.print') as output:
            self.assertEqual(2,cli.main(['--config','/private/service.json','--subscription-config','/private/subscription.json','--once']))
            self.assertNotIn('private-secret',output.call_args.args[0])

    def test_managed_unit_argument_and_injection_denial(self):
        from manager_bridge.runtime_transition import UnitDraft,render_unit
        values=('/opt/taesan/app','/opt/taesan/python','/private/service.json','/private/state')
        value=render_unit(UnitDraft(*values,subscription_config_file='/private/subscription.json'))
        self.assertIn('--subscription-config /private/subscription.json',value)
        self.assertNotIn('--subscription-config',render_unit(UnitDraft(*values)))
        for path in ('relative','/private/file name','/private/%i','/private/../secret','/private/file\nExecStart=/bin/sh'):
            with self.assertRaises(ValueError):UnitDraft(*values,subscription_config_file=path)

    def test_unit_cli_emits_managed_argument_without_reading_secrets(self):
        import subprocess,sys
        private=self.root/'private-subscription.json';private.write_text('SYNTHETIC_DO_NOT_READ');private.chmod(0o600)
        output=self.root/'draft.service'
        script=Path(__file__).resolve().parents[2]/'scripts/prepare_governance_unit.py'
        run=subprocess.run([sys.executable,str(script),'--application-root','/opt/taesan/app','--python','/opt/taesan/python','--config-file','/private/service.json','--state-directory','/private/state','--subscription-config-file',str(private),'--output',str(output)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,run.returncode,run.stderr)
        self.assertIn('--subscription-config '+str(private),output.read_text())
        self.assertNotIn('SYNTHETIC_DO_NOT_READ',run.stdout+run.stderr+output.read_text())
        self.assertFalse(json.loads(run.stdout)['installed'])

    def test_managed_cli_cannot_omit_governance_admission(self):
        self.setup_files();self.serviceconfig.pop('governance');self.write_files()
        with self.assertRaises(ValueError):self.load()
