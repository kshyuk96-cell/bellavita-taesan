"""Actual authenticated loopback for the single manager surface; fixture workers."""
import json
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_three_tier as hierarchy_fixtures
from manager_bridge.tests import test_governance_service as service_fixtures
from manager_bridge.control import request
from manager_bridge.control_center_api import PREFIX
from manager_bridge.store import digest,token_hash,PermissionDenied,Conflict


class ControlCenterTests(unittest.TestCase):
    setUp=hierarchy_fixtures.ThreeTierTests.setUp
    observe=hierarchy_fixtures.ThreeTierTests.observe
    verify=hierarchy_fixtures.ThreeTierTests.verify
    compose=service_fixtures.GovernanceServiceTests.compose
    start_http=service_fixtures.GovernanceServiceTests.start_http

    def call(self,base,token,action,body):return request(base,token,'POST',PREFIX+action,body)

    def specs(self,service):
        from manager_bridge.tests.test_qa_admission import QAAdmissionTests
        qa=QAAdmissionTests();qa.fixture()
        config=self.registry.get(self.manager,'project')
        with self.store._transaction() as c:
            c.execute('INSERT INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',('project',config['fingerprint'],'a'*40,'fixture-revision','fixture/head',self.now,1))
        service.server.governance_gate.register_policy(self.owner,'project',contract_version='fixture/qa',
            expected_contract=qa.expected_contract,required_coverage=['money'],provider_identity='fixture-provider')
        return [{'project':'project','repository':'fixture/project','base_sha':'a'*40,
                 'description':'Synthetic central task '+str(i),'checks':['fixture-check'],'kind':'development',
                 'allowed_paths':['src/item'+str(i)+'/'],'dependencies':[],'qa_contract_ref':'fixture/qa',
                 'qa_contract_digest':digest(qa.expected_contract),'priority':'P1','risk_rationale':'Synthetic approved scope'} for i in range(20)]

    def test_actual_http_twenty_item_prepare_approve_read_and_host_scope(self):
        service=self.compose();base=self.start_http(service);specs=self.specs(service)
        batch=self.call(base,self.manager,'bundles/prepare',{'request_key':'central-twenty','specs':specs})
        self.assertTrue(batch['ok']);self.assertEqual(len(batch['result']['items']),20)
        args={'batch_id':batch['result']['id'],'batch_digest':batch['result']['digest']}
        self.assertEqual(self.call(base,self.manager,'bundles/approve',args)['status'],403)
        accepted=self.call(base,self.owner,'bundles/approve',args)
        self.assertTrue(accepted['ok']);self.assertEqual(len(accepted['result']['tasks']),20)
        self.assertFalse(accepted['result']['technical_acceptance'])
        self.assertEqual(self.call(base,self.manager,'bundles/get',{'batch_id':args['batch_id']})['result'],accepted['result'])
        task=accepted['result']['tasks'][0]
        result=self.call(base,self.manager,'tasks/get',{'task_id':task['id']})
        self.assertTrue(result['ok']);self.assertFalse(result['result']['technical_accepted'])
        wrong=[{**specs[0],'project':'other','repository':'fixture/other'}]
        self.assertEqual(self.call(base,self.manager,'bundles/prepare',{'request_key':'outside','specs':wrong})['status'],404)

    def test_inventory_plan_summary_and_restart_readback(self):
        service=self.compose();base=self.start_http(service)
        inventory=self.call(base,self.manager,'connections/inventory',{'projects':['project']})
        self.assertTrue(inventory['ok']);self.assertFalse(inventory['result']['connection_probe_performed'])
        config=self.registry.get(self.manager,'project')
        args={'projects':['project'],'request_key':'connection-plan','expected_bindings':{
            'project':{'revision':config['revision'],'fingerprint':config['fingerprint']}}}
        plan=self.call(base,self.manager,'connections/plan',args)
        self.assertTrue(plan['ok']);self.assertFalse(plan['result']['approval_granted'])
        read=self.call(base,self.manager,'connections/get',{'plan_id':plan['result']['id']})
        self.assertEqual(read['result'],plan['result'])
        report=self.call(base,self.manager,'report',{'projects':['project']})
        self.assertTrue(report['ok']);self.assertFalse(report['result']['messages_sent'])
        self.assertIsNone(report['result']['completion_percentage'])

    def test_strict_fields_roles_stop_and_no_unconfigured_fallback(self):
        service=self.compose();base=self.start_http(service)
        for action,args in [('step',{'projects':['project'],'instance_id':'malicious'}),
                            ('report',{'projects':['project'],'callback':'private'}),
                            ('bundles/prepare',{'request_key':'bad','specs':[{'project':[]}]}),
                            ('bundles/prepare',{'request_key':'bad','specs':[]})]:
            self.assertEqual(self.call(base,self.manager,action,args)['status'],400)
        self.assertEqual(self.call(base,self.dev,'report',{'projects':['project']})['status'],403)
        self.assertEqual(self.call(base,self.owner,'step',{'projects':['project']})['status'],403)
        service.stop()
        with patch.dict(self.controller.handlers,{'start':lambda *_:self.fail('dispatch after stop')}):
            result=self.call(base,self.manager,'step',{'projects':['project']})
        self.assertEqual(result['result']['state'],'stopped')
        del service.server.control_center
        self.assertEqual(self.call(base,self.manager,'report',{'projects':['project']})['status'],404)

    def test_revocation_and_configuration_change_during_summary_fail_closed(self):
        service=self.compose();control=service.server.control_center
        original=control.summary.report
        def drift(*args):
            result=original(*args)
            self.registry.register(self.owner,'project',self.config,expected_revision=1)
            return result
        with patch.object(control.summary,'report',side_effect=drift):
            with self.assertRaises(Conflict):control.report(self.manager,['project'])
        self.store.revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(PermissionDenied):control.report(self.manager,['project'])

    def test_step_refreshes_registered_source_before_dispatch_and_outage_blocks(self):
        from manager_bridge.source_watch import HeadProbe,SourceHead
        service=self.compose();base=self.start_http(service);seen=[]
        def observe(project):
            seen.append(project)
            if len(seen)>1:raise ConnectionError('private provider diagnostic')
            return SourceHead(project,'github','fixture/project','main','a'*40,'fixture-revision','fixture/head',self.now)
        service.server.source_watch.probes['project']=HeadProbe('github','fixture/project','main',observe)
        with patch.object(self.controller,'tick',return_value={'state':'fixture_no_execution'}) as tick:
            first=self.call(base,self.manager,'step',{'projects':['project']})
            self.assertEqual(first['result']['state'],'fixture_no_execution')
            tick.assert_called_once()
            second=self.call(base,self.manager,'step',{'projects':['project']})
            self.assertEqual(second['result']['state'],'source_observation_blocked')
            tick.assert_called_once()
        self.assertEqual(seen,['project','project']);self.assertNotIn('private',json.dumps(second))

    def test_step_rechecks_authority_and_scope_after_trusted_preflight(self):
        service=self.compose();control=service.server.control_center
        with patch.object(self.controller,'tick',side_effect=AssertionError('unsafe dispatch')):
            control.source_preflight=None
            self.assertEqual(control.step(self.manager,['project'])['state'],'source_preflight_not_configured')
            def drift(*_):
                self.registry.register(self.owner,'project',self.config,expected_revision=1)
                return True
            control.source_preflight=drift
            with self.assertRaises(Conflict):control.step(self.manager,['project'])
            def revoke(*_):
                self.store.revoke_token(self.owner,token_hash(self.manager))
                return True
            control.source_preflight=revoke
            with self.assertRaises(PermissionDenied):control.step(self.manager,['project'])

    def test_outside_host_task_is_scoped_before_expiry_or_host_observation(self):
        service=self.compose();control=service.server.control_center
        task=self.store.submit(self.owner,'outside-task',{'project':'other','repository':'fixture/other','base_sha':'a'*40,
            'description':'Synthetic other host task','checks':['fixture-check'],'kind':'development'})
        from manager_bridge.store import NotFound
        with patch.object(self.store,'get_task',side_effect=AssertionError('out-of-host expiry mutation')):
            with self.assertRaises(NotFound):control.task(self.manager,task['id'])

    def test_http_instruction_starts_registered_developer_once_through_core(self):
        from manager_bridge.source_watch import HeadProbe,SourceHead
        from manager_bridge.continuous_manager import ActionReceipt
        service=self.compose();base=self.start_http(service);specs=self.specs(service)[:1]
        service.server.source_watch.probes['project']=HeadProbe('github','fixture/project','main',
            lambda p:SourceHead(p,'github','fixture/project','main','a'*40,'fixture-revision','fixture/head',self.now))
        batch=self.call(base,self.manager,'bundles/prepare',{'request_key':'single-instruction','specs':specs})['result']
        approved=self.call(base,self.owner,'bundles/approve',{'batch_id':batch['id'],'batch_digest':batch['digest']})['result']
        calls=[]
        def start(action,heartbeat):
            heartbeat();calls.append(action.task_id)
            self.core.start(self.manager,action.task_id,'dev','control-start-'+action.action_key)
            return ActionReceipt(action.action_key,action.task_id,action.phase,'observed','fixture/control-start',self.now)
        self.controller.handlers['start']=start
        first=self.call(base,self.manager,'step',{'projects':['project']})
        self.assertEqual(first['result']['actions'][0]['state'],'confirmed')
        task_id=approved['tasks'][0]['id']
        self.assertEqual(self.core.get(self.manager,task_id)['state'],'development_queued')
        self.call(base,self.manager,'step',{'projects':['project']})
        self.assertEqual(calls,[task_id])
        self.assertFalse(self.core.get(self.manager,task_id)['technical_accepted'])
