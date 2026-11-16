"""Synthetic native delivery transport tests; no model or network calls."""
import json,subprocess,tomllib,unittest
from pathlib import Path
from unittest.mock import patch
from manager_bridge.tests import test_reference_delivery as fixtures
from manager_bridge.fleet_codex import CodexHost,ProjectBinding,TransportUnavailable
from manager_bridge.store import PermissionDenied

class FleetReferenceTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.ReferenceDeliveryTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.root=self.fx.fx.fx.fx.root
        self.source=self.root/'native-source';self.home=self.root/'native-home'
        self.source.mkdir();self.home.mkdir(mode=0o700)
        self.host=CodexHost('/usr/bin/true',self.home,{'project':ProjectBinding('project','fixture/project',self.source,require_origin=False)},reference_provider=self.fx.delivery.prepare)
        self.host._confidentiality['project']=((self.root,),{'verified':True})
    def response(self,argv,**kwargs):
        ro=argv[-4]=='1' # arguments: ... ro git refs root scaffolds; use Python args below instead
        marker=argv.index('--');arguments=argv[marker+5:];ro=arguments[3]=='1'
        value={'git_write_denied':True,'inside_read':True,'inside_write':not ro,'private_tmp_write':True,'network_blocked':True,'private_resources_hidden':True,'reference_readonly':True,'sanitized_ancestor_scaffold':True,'real_parent_contents_hidden':True}
        return subprocess.CompletedProcess(argv,0,json.dumps(value).encode(),b'')
    def test_exact_context_readonly_policy_and_dual_role_fresh_proof(self):
        packet=self.fx.packet();calls=[]
        def observed(argv,**kwargs):calls.append(argv);return self.response(argv,**kwargs)
        with patch('manager_bridge.fleet_codex.subprocess.run',side_effect=observed):
            with self.host._reference_scope('project',self.fx.task,'developer',None,packet,lambda:True) as context:
                self.assertEqual(len(calls),2)
                evidence=self.host._confidentiality['project'][1]
                self.assertEqual(evidence['reference_binding_digest'],context.mount_digest)
                args=self.host._permission_args('project',False,self.root/'tmpjob',(self.root,),context)
                line=next(args[i+1] for i,x in enumerate(args[:-1]) if x=='-c' and args[i+1].startswith('permissions.taesan-worker='))
                fs=tomllib.loads('v='+line.split('=',1)[1])['v']['filesystem']
                self.assertEqual(fs[str(context.directory)],'read');self.assertNotIn(str(context.directory.parent),fs)
        self.assertEqual(self.host._confidentiality['project'][1],{'verified':True});self.fx.assert_clean()
    def test_refs_missing_provider_or_wrong_typed_context_never_start(self):
        self.host.reference_provider=None
        with self.assertRaises(PermissionDenied):
            with self.host._reference_scope('project',self.fx.task,'developer',None,self.fx.packet(),lambda:True):self.fail()
    def test_no_refs_legacy_needs_no_provider_or_probe(self):
        self.host.reference_provider=None
        with self.host._reference_scope('project',self.fx.task,'developer',None,None,lambda:True) as context:self.assertIsNone(context)
    def test_failed_probe_restores_previous_proof_and_closes_prepared_context(self):
        with patch('manager_bridge.fleet_codex.subprocess.run',return_value=subprocess.CompletedProcess([],3,b'{}',b'')):
            with self.assertRaises(TransportUnavailable):
                with self.host._reference_scope('project',self.fx.task,'developer',None,self.fx.packet(),lambda:True):self.fail()
        self.assertEqual(self.host._confidentiality['project'][1],{'verified':True});self.fx.assert_clean()
    def test_unexpected_real_parent_name_fails_actual_probe_script(self):
        # Run the generated probe script unsandboxed only against synthetic fixtures.
        # Everything except parent visibility is mocked with expected-safe responses;
        # an extra basename must make denied(parent) false inside the actual script.
        from types import SimpleNamespace
        captured=[]
        def inspect(argv,**kwargs):
            script=argv[argv.index('--')+4];captured.append(script)
            return self.response(argv,**kwargs)
        with self.fx.prepare() as context,patch('manager_bridge.fleet_codex.subprocess.run',side_effect=inspect):self.host.verify_confidentiality('project',(self.root,),reference_context=context)
        script=captured[0];definition=script[script.index('def denied(path):'):script.index("with open(inside)")]
        fake=SimpleNamespace(path=SimpleNamespace(isdir=lambda path:True),listdir=lambda path:['approved-child','private-secret-name'])
        namespace={'os':fake,'scaffolds':{'parent':['approved-child']}}
        exec(definition,namespace)
        self.assertFalse(namespace['denied']('parent'))
