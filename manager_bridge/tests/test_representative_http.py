import json
from pathlib import Path
import tempfile
import threading
import unittest
from manager_bridge.store import Store
from manager_bridge.maintenance_registry import Registry
from manager_bridge.server import make_server
from manager_bridge.control import request


class RepresentativeHTTP(unittest.TestCase):
    def setUp(self):
        self.folder=self.enterContext(tempfile.TemporaryDirectory())
        self.store=Store(Path(self.folder)/'manager.sqlite3')
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        config={'company':'company','name':'Fixture ERP','source_kind':'github','source_ref':'fixture/erp','branch':'main','services':[],'maintenance_enabled':False}
        self.registry.register(self.owner,'erp',config)
        self.manager=self.store.add_token('manager','manager',['erp'])
        self.worker=self.store.add_token('worker','worker',['erp'])
        self.server=make_server(self.store,port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_address[1])
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown();self.thread.join(timeout=3);self.server.server_close()

    def test_authenticated_single_window_request_creates_no_approval(self):
        response=request(self.base,self.manager,'POST','/v1/representative/intake',{'request_key':'http-1','conversation':'meeting','text':'Fixture ERP tests'})
        self.assertTrue(response['ok'])
        result=response['result']
        self.assertEqual(result['project'],'erp')
        self.assertIs(result['approval_granted'],False)
        read=request(self.base,self.manager,'GET','/v1/representative/intake?id='+result['id'])
        self.assertEqual(read['result']['id'],result['id'])

    def test_old_manager_routes_and_auth_remain_available(self):
        self.assertTrue(request(self.base,self.manager,'GET','/v1/tasks?project=erp')['ok'])
        result=request(self.base,self.manager,'GET','/v1/representative/agents?project=erp')
        self.assertTrue(result['ok']);self.assertEqual(result['result'],[])

    def test_worker_cannot_impersonate_representative(self):
        response=request(self.base,self.worker,'POST','/v1/representative/intake',{'request_key':'http-1','conversation':'meeting','text':'Fixture ERP tests'})
        self.assertEqual(response['status'],403)

    def test_client_cannot_register_execution_callback(self):
        result=request(self.base,self.owner,'POST','/v1/representative/intake',{'request_key':'http-1','conversation':'meeting','text':'Fixture ERP','execute':'arbitrary code'})
        self.assertEqual(result['status'],400)
        self.assertNotIn('arbitrary code',json.dumps(result))

    def test_unknown_and_duplicate_queries_are_rejected(self):
        self.assertEqual(request(self.base,self.manager,'GET','/v1/representative/unknown')['status'],404)
        self.assertEqual(request(self.base,self.manager,'GET','/v1/representative/agents?project=erp&project=erp')['status'],400)

    def test_worker_http_claim_uses_exact_inbox_task_not_another_queue_item(self):
        tasks=[]
        for index in (1,2):
            task=self.store.submit(self.owner,'claim-'+str(index),{'project':'erp','repository':'fixture/erp','base_sha':'a'*40,'description':'fixture','checks':['fixture'],'kind':'test'})
            tasks.append(self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest']))
        claimed=request(self.base,self.worker,'POST','/v1/worker/claim',{'project':'erp','task_id':tasks[1]['id']})
        self.assertTrue(claimed['ok'])
        self.assertEqual(claimed['task']['id'],tasks[1]['id'])
        self.assertEqual(self.store.get_task(self.owner,tasks[0]['id'])['state'],'queued')

    def test_cross_project_inventory_not_visible(self):
        self.assertEqual(request(self.base,self.manager,'GET','/v1/representative/agents?project=foreign')['status'],404)
