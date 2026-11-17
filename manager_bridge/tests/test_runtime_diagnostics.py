import hashlib
import json
from unittest.mock import patch
import unittest
from manager_bridge.tests import test_operations_control as fixtures
from manager_bridge.control import request
from manager_bridge.store import PermissionDenied,NotFound,Conflict
from manager_bridge.operations_control_api import PREFIX
from scripts import inspect_governance_runtime as cli


class RuntimeDiagnosticsTests(unittest.TestCase):
    setUp=fixtures.OperationsControlTests.setUp
    observe=fixtures.OperationsControlTests.observe
    verify=fixtures.OperationsControlTests.verify
    compose=fixtures.OperationsControlTests.compose
    start_http=fixtures.OperationsControlTests.start_http

    def snapshot(self,token=None):
        service=self.compose()
        return service.server.operations_control.diagnostics(token or self.manager,['project'])

    def test_ten_sections_do_not_claim_live_verification(self):
        value=self.snapshot();self.assertEqual(10,value['section_count'])
        self.assertEqual(cli.SECTIONS,{i['section'] for i in value['sections']})
        self.assertIs(value,cli.validate_snapshot(value))
        self.assertTrue(all(value[k] is False for k in cli.FLAGS))

    def test_metadata_snapshot_does_not_change_database_bytes(self):
        service=self.compose();before=hashlib.sha256(self.store.path.read_bytes()).hexdigest()
        service.server.operations_control.diagnostics(self.manager,['project'])
        self.assertEqual(before,hashlib.sha256(self.store.path.read_bytes()).hexdigest())

    def test_worker_and_outside_scope_denied(self):
        service=self.compose()
        for token,projects in ((self.dev,['project']),(self.manager,['other'])):
            with self.assertRaises((PermissionDenied,NotFound)):service.server.operations_control.diagnostics(token,projects)

    def test_backup_totals_global_owner_only(self):
        manager=self.snapshot();owner=self.snapshot(self.owner)
        def backup(v):return next(i for i in v['sections'] if i['section']=='backup_records')
        self.assertEqual('restricted',backup(manager)['availability'])
        self.assertEqual('not_recorded',backup(owner)['availability'])
        limited=self.store.add_token('limited-owner','owner',['project'])
        self.assertEqual('restricted',backup(self.snapshot(limited))['availability'])

    def test_no_providers_or_runtime_commands(self):
        service=self.compose()
        with patch.object(service.controller,'tick',side_effect=AssertionError('dispatch')),patch.object(service.server.operations_control,'workers',side_effect=AssertionError('native RPC')),patch.object(service.server.operations_control,'refresh_auth',side_effect=AssertionError('refresh')):
            service.server.operations_control.diagnostics(self.manager,['project'])

    def test_unknown_states_counted_without_returning_raw_text(self):
        service=self.compose()
        with self.store._transaction() as c:
            c.execute("INSERT INTO runtime_transition_state VALUES ('project',1,'x','PRIVATE_SENTINEL','x',1000)")
        value=service.server.operations_control.diagnostics(self.manager,['project'])
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(value))
        self.assertEqual(1,next(i for i in value['sections'] if i['section']=='transitions')['other_count'])

    def test_http_exact_fields_and_roles(self):
        service=self.compose();base=self.start_http(service)
        self.assertEqual(10,request(base,self.manager,'POST',PREFIX+'diagnostics',{'projects':['project']})['result']['section_count'])
        self.assertEqual(400,request(base,self.manager,'POST',PREFIX+'diagnostics',{'projects':['project'],'refresh':True})['status'])
        self.assertEqual(403,request(base,self.dev,'POST',PREFIX+'diagnostics',{'projects':['project']})['status'])

    def test_cli_reads_existing_api_only_and_sanitizes_errors(self):
        value=self.snapshot();token=self.root/'manager.token';token.write_text(self.manager);token.chmod(0o600)
        with patch.object(cli,'request',return_value={'ok':True,'result':value}) as called,patch('builtins.print'):
            self.assertEqual(0,cli.main(['--base-url','https://example.invalid','--token-file',str(token),'--project','project']))
            self.assertEqual(PREFIX+'diagnostics',called.call_args.args[3])
        with patch.object(cli,'request',side_effect=RuntimeError('PRIVATE_SENTINEL')),patch('builtins.print') as printed:
            self.assertEqual(2,cli.main(['--base-url','https://example.invalid','--token-file',str(token),'--project','project']))
            self.assertNotIn('PRIVATE_SENTINEL',str(printed.call_args))

    def test_client_rejects_secret_fields_and_live_claims(self):
        value=self.snapshot()
        for change in ({'secret':'PRIVATE_SENTINEL'},{'authentication_verified':True},{'models_started':1}):
            with self.assertRaises(ValueError):cli.validate_snapshot({**value,**change})

    def test_output_excludes_tokens_paths_and_project_names(self):
        value=json.dumps(self.snapshot())
        for secret in (self.owner,self.manager,self.dev,str(self.root),'fixture/project'):
            self.assertNotIn(secret,value)

    def test_actual_cli_process_against_running_service(self):
        import subprocess,sys
        from pathlib import Path
        service=self.compose();base=self.start_http(service)
        token=self.root/'cli.token';token.write_text(self.manager);token.chmod(0o600)
        script=Path(__file__).resolve().parents[2]/'scripts/inspect_governance_runtime.py'
        run=subprocess.run([sys.executable,str(script),'--base-url',base,'--token-file',str(token),'--project','project'],capture_output=True,text=True,timeout=20)
        self.assertEqual(0,run.returncode,run.stderr)
        value=json.loads(run.stdout);self.assertEqual(10,value['section_count'])
        self.assertNotIn(self.manager,run.stdout+run.stderr)

    def test_configuration_drift_between_scope_and_snapshot_denied(self):
        service=self.compose();control=service.server.operations_control.control
        original=control._scope
        def drift(*args,**kwargs):
            before=original(*args,**kwargs)
            old=self.registry.get(self.owner,'project')
            self.registry.register(self.owner,'project',old['config'],expected_revision=old['revision'])
            return before
        with patch.object(control,'_scope',side_effect=drift):
            with self.assertRaises(Conflict):service.server.operations_control.diagnostics(self.manager,['project'])
