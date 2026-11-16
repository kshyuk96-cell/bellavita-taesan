"""Actual authenticated loopback integration; no models or customer changes."""
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.tests import test_governance_service as service_fixtures
from manager_bridge.control import request
from manager_bridge.operations_control_api import PREFIX
from manager_bridge.host_admission import HostAdmission,HostObservation
from manager_bridge.host_preflight import local_host_identity


class ReadinessControlTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    compose=service_fixtures.GovernanceServiceTests.compose
    start_http=service_fixtures.GovernanceServiceTests.start_http

    def call(self,base,token,route,body):return request(base,token,'POST',PREFIX+route,body)

    def test_live_readiness_recovery_and_grouped_exceptions(self):
        service=self.compose();base=self.start_http(service)
        for route in ('readiness','recovery'):
            result=self.call(base,self.manager,route,{'projects':['project']})
            self.assertTrue(result['ok'],result);self.assertFalse(result['result']['always_on_verified'])
            self.assertEqual(self.call(base,self.dev,route,{'projects':['project']})['status'],403)
            self.assertEqual(self.call(base,self.manager,route,{'projects':['other']})['status'],404)
        summary=self.call(base,self.manager,'exceptions',{'projects':['project'],'request_key':'readiness-summary'})
        self.assertTrue(summary['ok'],summary);self.assertFalse(summary['result']['messages_sent'])
        self.assertIsNone(summary['result']['percentage'])
        forged=self.call(base,self.manager,'readiness',{'projects':['project'],'always_on_verified':True})
        self.assertEqual(forged['status'],400)

    def test_host_routes_need_trusted_binding_and_owner_pin(self):
        service=self.compose();base=self.start_http(service)
        self.assertEqual(self.call(base,self.manager,'host/refresh',{'project':'project'})['status'],404)
        gate=HostAdmission(self.core,observer=lambda req:HostObservation(req.host_identity,req.application_commit,True,True,self.now))
        service.server.operations_control.budget.host_admission=gate
        body={'project':'project','requirement':{'host_identity':local_host_identity(),'application_commit':'a'*40}}
        self.assertEqual(self.call(base,self.manager,'host/install',body)['status'],403)
        self.assertTrue(self.call(base,self.owner,'host/install',body)['ok'])
        result=self.call(base,self.manager,'host/refresh',{'project':'project'})
        self.assertTrue(result['result']['allowed']);self.assertFalse(result['result']['always_on_verified'])
        self.assertEqual(self.call(base,self.manager,'host/refresh',{'project':'project','namespace_isolation_verified':True})['status'],400)
        self.assertEqual(self.call(base,self.owner,'host/install',{'project':'project','requirement':{'callback':'bad'}})['status'],400)

    def test_registry_batch_is_owner_only_strict_and_does_not_expand_runtime(self):
        service=self.compose();base=self.start_http(service)
        denied=self.call(base,self.manager,'projects/register',{'request_key':'new','registrations':[]})
        self.assertEqual(denied['status'],403)
        config=dict(self.registry.get(self.owner,'project')['config']);config['maintenance_enabled']=False
        body={'request_key':'future-project','registrations':[{'project':'future-project','config':config,'expected_revision':0,'expected_fingerprint':None}]}
        result=self.call(base,self.owner,'projects/register',body)
        self.assertTrue(result['ok'],result);self.assertFalse(result['result']['execution_authorized'])
        self.assertNotIn('future-project',service.settings['projects'])
        self.assertEqual(self.call(base,self.owner,'readiness',{'projects':['future-project']})['status'],404)
        self.assertEqual(self.call(base,self.owner,'projects/register',body)['result'],result['result'])

    def test_duplicate_service_stops_before_http_thread_or_source_probe(self):
        from manager_bridge.store import Conflict
        from unittest.mock import patch
        service=self.compose()
        with service.recovery.instance_guard(self.manager,['project'],'other-manager'):
            with patch.object(service,'_run',side_effect=AssertionError('duplicate runtime started')):
                with self.assertRaises(Conflict):service.run(max_ticks=1)
        self.assertIsNone(service._thread)

    def test_private_config_can_register_host_gate_without_http_callbacks(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import patch
        from manager_bridge.governance_service import load_service
        from manager_bridge.continuous_manager import ContinuousManager
        host_config=self.root/'local-host.json'
        host_config.write_text(json.dumps({'application_root':str(self.root),'data_root':str(self.root)}));host_config.chmod(0o600)
        config=self.root/'local-service.json'
        config.write_text(json.dumps({'version':1,'host_config_file':str(host_config),'governance':{},'local_host_admission':{},'listen':{'port':0}}));config.chmod(0o600)
        host=SimpleNamespace(store=self.store,registry=self.registry,core=self.core,fleet=None,manager_token=lambda:self.manager)
        controller=ContinuousManager(self.core)
        settings={'projects':('project',),'instance_id':'fixture-host-service','poll_seconds':1}
        with patch('manager_bridge.governance_service.load_host',return_value=(host,controller,settings)):
            service=load_service(config)
        self.addCleanup(service.close)
        self.assertIsNotNone(controller.admission_budget.host_admission)
        self.assertEqual(controller.admission_budget.check(self.manager,'project')['reason'],'host_requirement_missing')
