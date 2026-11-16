"""Synthetic composition and actual authenticated loopback; no model execution."""
import json
from types import SimpleNamespace
from unittest.mock import patch
import threading
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.governance_service import load_service, _listener
from manager_bridge.control import request

PREFIX='/v1/representative/runtime/'

class GovernanceServiceTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify

    def compose(self,governance=True):
        host_config=self.root/'host.json';host_config.write_text('{}');host_config.chmod(0o600)
        config=self.root/'service.json'
        value={'version':1,'host_config_file':str(host_config),'listen':{'port':0}}
        if governance:value['governance']={}
        config.write_text(json.dumps(value));config.chmod(0o600)
        self.controller=ContinuousManager(self.core)
        host=SimpleNamespace(store=self.store,registry=self.registry,core=self.core,fleet=None,manager_token=lambda:self.manager)
        settings={'projects':('project',),'instance_id':'fixture-service','poll_seconds':1}
        with patch('manager_bridge.governance_service.load_host',return_value=(host,self.controller,settings)) as loader:
            service=load_service(config);loader.assert_called_once_with(host_config)
        self.addCleanup(service.close)
        return service

    def start_http(self,service):
        thread=threading.Thread(target=service.server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:thread.join(timeout=3));self.addCleanup(service.server.shutdown)
        return 'http://127.0.0.1:'+str(service.server.server_address[1])

    def test_composes_shared_server_only_objects_and_bounded_run(self):
        service=self.compose()
        self.assertIs(service.server.store,self.store);self.assertIs(service.server.three_tier,self.core)
        self.assertIs(service.server.continuous_manager,self.controller)
        self.assertIs(service.server.governance_gate,self.core.governance_gate)
        self.assertIs(service.server.proposal_board.store,self.store)
        with self.store._transaction() as c:
            self.assertIsNotNone(c.execute('SELECT project FROM governance_enrolled_projects WHERE project=?',('project',)).fetchone())
        result=service.run(max_ticks=1)
        self.assertEqual(result['state'],'bounded_run_complete');self.assertEqual(result['ticks'],1)
        self.assertFalse(result['always_on_verified'])

    def test_strict_listener_and_private_config(self):
        for value in ({'host':'0.0.0.0'},{'port':True},{'public_origin':'http://example.org'},{'public_origin':'https://example.org/?key=private'},{'callback':'anything'}):
            with self.assertRaises(ValueError):_listener(value)
        self.assertEqual(_listener({})[:2],('127.0.0.1',8791))
        self.assertEqual(_listener({'host':'0.0.0.0','public_origin':'https://example.org'})[0],'0.0.0.0')

    def test_http_strict_proposal_routes_and_scoped_unconfigured(self):
        service=self.compose(False);base=self.start_http(service)
        result=request(base,self.manager,'POST',PREFIX+'governance/proposals/list',{'project':'project'})
        self.assertEqual(result['result']['state'],'not_configured')
        bad=request(base,self.manager,'POST',PREFIX+'governance/proposals/list',{'project':'project','provider':'private command'})
        self.assertEqual(bad['status'],400);self.assertNotIn('private command',json.dumps(bad))
        denied=request(base,self.dev,'POST',PREFIX+'governance/proposals/list',{'project':'project'})
        self.assertEqual(denied['status'],403)
        approve=request(base,self.manager,'POST',PREFIX+'governance/batches/approve',{'batch_id':'a'*32,'batch_digest':'b'*64})
        self.assertEqual(approve['status'],403)

    def test_periodic_source_refresh_blocks_outage_and_retains_negative(self):
        from manager_bridge.source_watch import HeadProbe,SourceHead
        service=self.compose();observations=[]
        def observe(project):
            observations.append(project)
            if len(observations)>1:raise ConnectionError('private provider detail')
            return SourceHead(project,'github','fixture/project','main','a'*40,'fixture-revision','fixture/head',self.now)
        service.server.source_watch.probes['project']=HeadProbe('github','fixture/project','main',observe)
        with patch.object(self.controller,'tick',return_value={}) as tick:
            result=service.run(max_ticks=2)
        self.assertEqual(observations,['project','project']);self.assertEqual(tick.call_count,1)
        self.assertEqual(result['source_blocked_ticks'],1)
        self.assertEqual(result['source_watch'],[{'project':'project','state':'unavailable'}])
        with self.store._transaction() as c:
            row=c.execute('SELECT state FROM manager_source_observation_status WHERE project=?',('project',)).fetchone()
            self.assertEqual(row['state'],'unavailable')
            self.assertIsNotNone(c.execute('SELECT * FROM manager_source_heads WHERE project=?',('project',)).fetchone())
        self.assertNotIn('private provider detail',json.dumps(result))

    def test_revoked_manager_cannot_refresh_or_dispatch(self):
        from manager_bridge.store import PermissionDenied
        service=self.compose()
        with self.store._transaction() as c:
            c.execute('UPDATE bridge_tokens SET revoked=? WHERE subject=?',(self.now,'taesan'))
        with patch.object(self.controller,'tick',side_effect=AssertionError('revoked dispatch')):
            with self.assertRaises(PermissionDenied):service.refresh_sources(self.manager,['project'])

    def test_actual_http_proposal_batch_owner_decision(self):
        from manager_bridge.source_watch import SourceWatch
        service=self.compose();base=self.start_http(service)
        SourceWatch(self.store,self.registry)
        config=self.registry.get(self.manager,'project')
        with self.store._transaction() as c:
            c.execute('INSERT INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',('project',config['fingerprint'],'a'*40,'fixture-revision','fixture/head',self.now,1))
        from manager_bridge.tests.test_qa_admission import QAAdmissionTests
        from manager_bridge.store import digest
        qa=QAAdmissionTests();qa.fixture()
        service.server.governance_gate.register_policy(self.owner,'project',contract_version='fixture/qa',
            expected_contract=qa.expected_contract,required_coverage=['money'],provider_identity='fixture-provider')
        spec={'project':'project','repository':'fixture/project','base_sha':'a'*40,
              'description':'Synthetic HTTP approval fixture','checks':['fixture-check'],'kind':'development',
              'allowed_paths':['src/'],'dependencies':[],'qa_contract_ref':'fixture/qa',
              'qa_contract_digest':digest(qa.expected_contract),'priority':'P2','risk_rationale':'Synthetic scope'}
        proposal=request(base,self.manager,'POST',PREFIX+'governance/proposals/create',{'request_key':'http-proposal','spec':spec})
        self.assertTrue(proposal['ok'])
        listed=request(base,self.manager,'POST',PREFIX+'governance/proposals/list',{'project':'project'})
        self.assertTrue(listed['ok'])
        batch=request(base,self.manager,'POST',PREFIX+'governance/batches/create',{'request_key':'http-batch','proposal_ids':[proposal['result']['id']]})
        self.assertTrue(batch['ok'])
        body={'batch_id':batch['result']['id'],'batch_digest':batch['result']['digest']}
        self.assertEqual(request(base,self.manager,'POST',PREFIX+'governance/batches/approve',body)['status'],403)
        approved=request(base,self.owner,'POST',PREFIX+'governance/batches/approve',body)
        self.assertTrue(approved['ok']);self.assertFalse(approved['result']['technical_acceptance'])
        self.assertFalse(approved['result']['native_delivery_authorized'])
        read=request(base,self.owner,'GET',PREFIX+'governance/batches/get?batch_id='+body['batch_id'])
        self.assertTrue(read['ok'])

    def test_stopping_prevents_http_dispatch_and_readonly_remains(self):
        service=self.compose();base=self.start_http(service);service.stop()
        with patch.object(self.controller,'tick',side_effect=AssertionError('dispatch after stop')):
            result=request(base,self.manager,'POST',PREFIX+'continuous/tick',{'projects':['project'],'instance_id':'fixture-service'})
        self.assertEqual(result['result']['state'],'stopped')
        status=request(base,self.manager,'POST',PREFIX+'continuous/status',{'projects':['project']})
        self.assertTrue(status['ok']);self.assertFalse(status['result']['always_on_verified'])


if __name__=='__main__':unittest.main()
