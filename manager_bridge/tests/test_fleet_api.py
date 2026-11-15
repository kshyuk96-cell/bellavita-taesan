"""Actual loopback requests exercise management schema/auth, never a live host."""
import json
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from manager_bridge.tests import test_fleet as fixtures
from manager_bridge.server import make_server
from manager_bridge.control import request

PREFIX='/v1/representative/runtime/'

class FleetHTTPTests(unittest.TestCase):
    setUp=fixtures.FleetTests.setUp
    observation=fixtures.FleetTests.observation
    discover=fixtures.FleetTests.discover
    create=fixtures.FleetTests.create

    def server_start(self,configured=True):
        server=make_server(self.store,port=0)
        if configured:server.fleet=self.fleet
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(lambda:thread.join(timeout=3));self.addCleanup(server.shutdown)
        return server,'http://127.0.0.1:'+str(server.server_address[1])

    def test_inventory_is_scoped_no_callback_and_does_not_connect(self):
        _,base=self.server_start()
        result=request(base,self.manager,'POST',PREFIX+'fleet/inventory',{'projects':['project']})
        self.assertTrue(result['ok']);self.assertEqual(self.discover_calls,0);self.assertEqual(self.create_calls,0)
        self.assertFalse(result['result']['account_wide_inventory_verified'])
        denied=request(base,self.limited,'POST',PREFIX+'fleet/inventory',{'projects':['other']})
        self.assertIn(denied['status'],(403,404))
        denied=request(base,self.worker,'POST',PREFIX+'fleet/inventory',{'projects':['project']})
        self.assertEqual(denied['status'],403)

    def test_not_configured_is_explicit_and_cannot_supply_executor(self):
        _,base=self.server_start(False)
        result=request(base,self.manager,'POST',PREFIX+'fleet/inventory',{'projects':['project']})
        self.assertEqual(result['result']['state'],'not_configured');self.assertFalse(result['result']['execution_started'])
        for token,projects,statuses in ((self.worker,['project'],(403,)),(self.limited,['other'],(403,404))):
            denied=request(base,token,'POST',PREFIX+'fleet/inventory',{'projects':projects})
            self.assertIn(denied['status'],statuses)
        bad=request(base,self.manager,'POST',PREFIX+'fleet/inventory',{'projects':['project'],'executor':'secret command'})
        self.assertEqual(bad['status'],400);self.assertNotIn('secret command',json.dumps(bad))

    def test_caller_cannot_supply_success_proof_or_handlers(self):
        _,base=self.server_start()
        for key in ('instruction_delivery_verified','delivery_observer','provider','OPENAI_API_KEY'):
            bad=request(base,self.manager,'POST',PREFIX+'fleet/delivery/verify',{'project':'project','host_ref':'host','external_ref':'thread-one','task_id':'a'*32,key:True})
            self.assertEqual(bad['status'],400)
        self.assertEqual(self.discover_calls,0)

    def test_unauthenticated_and_cross_site_cookie_request_rejected(self):
        _,base=self.server_start()
        data=json.dumps({'projects':['project']}).encode()
        for headers in ({'Content-Type':'application/json'}, {'Content-Type':'application/json','Cookie':'taesan_session=invalid','Origin':'https://evil.example','X-Taesan-Client':'dashboard','Sec-Fetch-Site':'cross-site'}):
            with self.assertRaises(HTTPError) as caught:urlopen(Request(base+PREFIX+'fleet/inventory',data=data,headers=headers),timeout=3)
            self.assertEqual(caught.exception.code,403)

    def test_query_and_readonly_status_schema_rejected(self):
        _,base=self.server_start()
        for route,body in [('fleet/inventory',{'projects':[{}]}),('fleet/inventory',{'projects':[[]]}),('fleet/inventory?project=project',{'projects':['project']}),('continuous/status',{'projects':['project'],'limit':True}),('continuous/tick',{'projects':['project'],'instance_id':'host','limit':True,'executor':'command'})]:
            bad=request(base,self.manager,'POST',PREFIX+route,body)
            self.assertEqual(bad['status'],400)

    def test_reserved_routes_accept_only_references_never_private_execution_proof(self):
        _,base=self.server_start()
        for action in ('reserved/begin','reserved/bind'):
            for key in ('lease_token','session','delivery','execution_started','provider'):
                response=request(base,self.manager,'POST',PREFIX+'fleet/'+action,{'reservation_id':'a'*64,key:'private proof'})
                self.assertEqual(response['status'],400)
        response=request(base,self.manager,'POST',PREFIX+'fleet/reserve',{'task_id':'b'*32,'template_ref':'template','request_key':'reserve'})
        self.assertEqual(response['status'],404)
        self.assertEqual(self.create_calls,0)
