"""Actual authenticated operations routes with private synthetic host bindings."""
import hashlib
import unittest
from manager_bridge.tests import test_three_tier as hierarchy_fixtures
from manager_bridge.tests import test_governance_service as service_fixtures
from manager_bridge.control import request
from manager_bridge.operations_control_api import PREFIX
from manager_bridge.operations_reporting import ArtifactObservation


class OperationsControlTests(unittest.TestCase):
    setUp=hierarchy_fixtures.ThreeTierTests.setUp
    observe=hierarchy_fixtures.ThreeTierTests.observe
    verify=hierarchy_fixtures.ThreeTierTests.verify
    task=hierarchy_fixtures.ThreeTierTests.task
    compose=service_fixtures.GovernanceServiceTests.compose
    start_http=service_fixtures.GovernanceServiceTests.start_http

    def call(self,base,token,action,body):return request(base,token,'POST',PREFIX+action,body)

    def test_budget_install_owner_only_and_legacy_http_start_cannot_bypass(self):
        service=self.compose();base=self.start_http(service)
        from manager_bridge.tests import test_control_center as control_fixtures
        specs=control_fixtures.ControlCenterTests.specs(self,service)[:1]
        batch=service.server.proposal_board.propose_bundle(self.manager,'budgeted-proposal',specs)
        task=service.server.proposal_board.approve_batch(self.owner,batch['id'],batch['digest'])['tasks'][0]
        args={'project':'project','policy':{'max_inflight':1,'max_starts':2,'window_seconds':60}}
        self.assertEqual(self.call(base,self.manager,'budget/install',args)['status'],403)
        installed=self.call(base,self.owner,'budget/install',args)
        self.assertTrue(installed['ok']);self.assertFalse(installed['result']['execution_authorized'])
        observed=self.call(base,self.manager,'budget/auth',{'project':'project'})
        self.assertEqual(observed['result']['state'],'unknown')
        report=self.call(base,self.manager,'budget/report',{'projects':['project']})
        self.assertFalse(report['result']['projects'][0]['allowed'])
        legacy=request(base,self.manager,'POST','/v1/representative/runtime/three-tier/start',
            {'task_id':task['id'],'developer_id':'dev','request_key':'legacy-budget-bypass'})
        self.assertEqual(legacy['status'],403)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT count(*) FROM hierarchy_tasks').fetchone()[0],0)

    def test_grouped_report_snapshots_and_no_artifact_upload_or_path(self):
        service=self.compose();base=self.start_http(service)
        body={'projects':['project'],'request_key':'one-grouped-report','limit':1}
        first=self.call(base,self.manager,'report',body)
        self.assertTrue(first['ok']);self.assertFalse(first['result']['messages_sent'])
        self.assertIn('admission_budget',first['result'])
        self.assertEqual(self.call(base,self.manager,'report',body)['result']['report_id'],first['result']['report_id'])
        missing=self.call(base,self.manager,'artifacts/register',{'project':'project','kind':'candidate','artifact_key':'missing'})
        self.assertEqual(missing['status'],404)
        raw=b'Synthetic observed result bytes'
        service.server.operations_control.reporting.artifact_sources[('project','candidate')]=lambda p,k:ArtifactObservation(p,'candidate',k,raw,self.now)
        receipt=self.call(base,self.manager,'artifacts/register',{'project':'project','kind':'candidate','artifact_key':'fixture-result'})
        self.assertEqual(receipt['result']['sha256'],hashlib.sha256(raw).hexdigest())
        found=self.call(base,self.manager,'artifacts/get',{'reference':receipt['result']['reference']})
        self.assertEqual(found['result']['retrieval'],'metadata_only');self.assertNotIn(raw.decode(),str(found))
        self.assertFalse(found['result']['physical_deletion_verified'])
        rejected=self.call(base,self.manager,'artifacts/register',{'project':'project','kind':'candidate','artifact_key':'x','data':'pretend evidence'})
        self.assertEqual(rejected['status'],400)

    def test_operations_scope_roles_exact_fields_and_unknown_routes(self):
        service=self.compose();base=self.start_http(service)
        for action,body in [('budget/auth',{'project':'other'}),('budget/report',{'projects':['other']}),('batches',{'projects':['other']})]:
            self.assertEqual(self.call(base,self.manager,action,body)['status'],404)
        self.assertEqual(self.call(base,self.dev,'budget/report',{'projects':['project']})['status'],403)
        self.assertEqual(self.call(base,self.manager,'budget/auth',{'project':'project','state':'ready'})['status'],400)
        self.assertEqual(self.call(base,self.manager,'budget/install',{'project':'project','policy':{}})['status'],403)
        self.assertEqual(self.call(base,self.owner,'budget/install',{'project':'project','policy':{'script':'bad'}})['status'],400)
        self.assertEqual(self.call(base,self.manager,'deploy',{})['status'],404)
        del service.server.operations_control
        self.assertEqual(self.call(base,self.manager,'budget/report',{'projects':['project']})['status'],404)
