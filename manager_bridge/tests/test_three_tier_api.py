"""Actual HTTP boundary tests; host evidence callbacks are synthetic fixtures."""
import threading
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.server import make_server
from manager_bridge.control import request


class ThreeTierApiTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    task=fixtures.ThreeTierTests.task
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify

    def server(self):
        server=make_server(self.store,port=0);server.three_tier=self.core
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(lambda:thread.join(timeout=3));self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:'+str(server.server_address[1])

    def post(self,base,route,args,token=None):
        return request(base,token or self.manager,'POST','/v1/representative/runtime/three-tier/'+route,args)

    def test_absent_host_is_not_configured_and_does_not_execute(self):
        base=self.server();self.core=None
        # Clear only the registered host; network server retains its private store.
        # A distinct fresh server is used because there is no global mutable host.
        server=make_server(self.store,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            response=self.post('http://127.0.0.1:'+str(server.server_address[1]),'overview',{'projects':['project']})
            self.assertEqual(response['result']['state'],'not_configured');self.assertFalse(response['result']['execution_started'])
        finally:server.shutdown();thread.join(timeout=3);server.server_close()

    def test_real_http_registered_overview_and_html_projection(self):
        base=self.server()
        summary=self.post(base,'overview',{'projects':['project']})['result']
        self.assertIn('projects',summary);self.assertFalse(summary['operational_change'])
        html=self.post(base,'dashboard',{'projects':['project']})['result']['html']
        self.assertIn('태산이',html);self.assertNotIn(self.manager,html);self.assertNotIn('<script',html)

    def test_input_cannot_supply_proof_commands_callbacks_or_roles(self):
        base=self.server()
        for field,value in [('passed',True),('commands',['true']),('review_hosts',{}),('role','owner')]:
            with self.subTest(field=field):
                result=self.post(base,'overview',{'projects':['project'],field:value})
                self.assertEqual(result['status'],400)

    def test_owner_registration_and_worker_actions_cannot_be_done_by_manager(self):
        base=self.server()
        value={'project':'project','member_id':'third','credential_hash':'a'*64,'role':'developer'}
        self.assertEqual(self.post(base,'members/register',value)['status'],403)
        task=self.task();self.core.start(self.manager,task['id'],'dev','http-start')
        self.assertEqual(self.post(base,'development/claim',{'task_id':task['id']})['status'],403)

    def test_worker_cannot_read_manager_all_project_dashboard(self):
        base=self.server()
        self.assertEqual(self.post(base,'overview',{'projects':['project']},self.dev)['status'],403)

    def test_missing_or_crossproject_scope_not_reported_as_empty(self):
        base=self.server()
        limited=self.store.add_token('limited','manager',['project'])
        result=self.post(base,'overview',{'projects':['project','other']},limited)
        self.assertIn(result['status'],(403,404))

    def test_legacy_worker_claim_cannot_take_unassigned_managed_task(self):
        task=self.task()
        self.assertIsNone(self.store.claim(self.dev,'project',task_id=task['id']))
        self.core.start(self.manager,task['id'],'dev','http-start')
        self.assertIsNone(self.store.claim(self.outsider,'project',task_id=task['id']))
