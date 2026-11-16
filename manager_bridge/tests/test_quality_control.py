"""Owner-approved baseline API, exact task pins and grouped quality reporting."""
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.tests import test_governance_service as service_fixtures
from manager_bridge.tests import test_control_center as control_fixtures
from manager_bridge.control import request
from manager_bridge.operations_control_api import PREFIX


class QualityControlTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    compose=service_fixtures.GovernanceServiceTests.compose
    start_http=service_fixtures.GovernanceServiceTests.start_http

    def call(self,base,token,route,body):return request(base,token,'POST',PREFIX+route,body)

    def contract(self):return {'version':'approved-v1','requirements':[{'id':'price','category':'price_behavior','priority':'P1','criterion':'Preserve approved pricing calculation.','scenario_ids':['s1']}],'references':[],'preserve':['Preserve event identity.']}

    def setup_baseline(self):
        service=self.compose();specs=control_fixtures.ControlCenterTests.specs(self,service);base=self.start_http(service)
        draft=self.call(base,self.manager,'quality/baseline/draft',{'project':'project','request_key':'baseline','contract':self.contract()})
        self.assertTrue(draft['ok'],draft)
        decision={'draft_id':draft['result']['id'],'draft_digest':draft['result']['digest']}
        return service,specs,base,decision

    def test_draft_is_not_approval_and_only_owner_approves_exact_digest(self):
        service,specs,base,decision=self.setup_baseline()
        current=self.call(base,self.manager,'quality/baseline/current',{'project':'project'})
        self.assertEqual(current['result']['state'],'not_configured')
        self.assertEqual(self.call(base,self.manager,'quality/baseline/approve',decision)['status'],403)
        wrong={**decision,'draft_digest':'b'*64}
        self.assertEqual(self.call(base,self.owner,'quality/baseline/approve',wrong)['status'],409)
        self.assertEqual(self.call(base,self.owner,'quality/baseline/approve',{'draft_id':[],'draft_digest':'a'*64})['status'],400)
        approved=self.call(base,self.owner,'quality/baseline/approve',decision)
        self.assertEqual(approved['result']['state'],'approved');self.assertFalse(approved['result']['business_acceptance_verified'])

    def test_approved_baseline_pins_actual_task_and_handoff(self):
        service,specs,base,decision=self.setup_baseline()
        baseline=self.call(base,self.owner,'quality/baseline/approve',decision)['result']
        board=service.server.proposal_board;batch=board.propose_bundle(self.manager,'quality-task',specs[:1])
        task=board.approve_batch(self.owner,batch['id'],batch['digest'])['tasks'][0]
        self.assertEqual(task['approval']['business_baseline']['digest'],baseline['digest'])
        packet=self.call(base,self.manager,'quality/handoff',{'task_id':task['id'],'role':'developer'})
        self.assertTrue(packet['ok'],packet);self.assertEqual(packet['result']['business_baseline']['digest'],baseline['digest'])
        self.assertFalse(packet['result']['execution_authorized']);self.assertFalse(packet['result']['quality_equivalence_guaranteed'])

    def test_grouped_quality_report_has_missing_work_without_quality_score(self):
        service,specs,base,decision=self.setup_baseline();self.call(base,self.owner,'quality/baseline/approve',decision)
        result=self.call(base,self.manager,'quality/report',{'projects':['project'],'freshness_limit':0})
        self.assertTrue(result['ok'],result)
        self.assertNotIn('completion_percentage',result['result']);self.assertFalse(result['result']['worker_quality_equivalence_verified'])
        self.assertEqual(self.call(base,self.dev,'quality/report',{'projects':['project']})['status'],403)
        self.assertEqual(self.call(base,self.manager,'quality/report',{'projects':['other']})['status'],404)
        self.assertEqual(self.call(base,self.manager,'quality/report',{'projects':['project'],'freshness_limit':4})['status'],400)

    def test_no_public_callback_path_or_secret_requirement_is_accepted(self):
        service,specs,base,decision=self.setup_baseline()
        body={'project':'project','request_key':'bad','contract':self.contract(),'reference_provider':'/tmp/program'}
        self.assertEqual(self.call(base,self.owner,'quality/baseline/draft',body)['status'],400)
        body.pop('reference_provider');body['contract']['preserve']=['sk-proj-privatefixture123456']
        self.assertEqual(self.call(base,self.manager,'quality/baseline/draft',body)['status'],400)
        self.assertEqual(self.call(base,self.manager,'quality/baseline/current',{'project':'other'})['status'],404)
