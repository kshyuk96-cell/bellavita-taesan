"""Exercise real Handler dispatch without opening sockets or contacting services."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import agent


class ProbeHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.token='synthetic-http-test-token-000000000000'
        self.payload={'schema':'planning-probe-v1','room':'기획','text':'태산이 수신 테스트 '+'c'*24}
    def call(self,body=None,auth=None,length=None,configured=True):
        raw=json.dumps(self.payload if body is None else body).encode()
        handler=agent.Handler.__new__(agent.Handler)
        handler.path='/pc/planning-probe'
        handler.headers={'Content-Length':str(len(raw)) if length is None else length,
                         'Authorization':'Bearer '+self.token if auth is None else auth}
        handler.rfile=io.BytesIO(raw)
        result=[]
        handler.send_json=lambda payload,status=200:result.append((status,payload))
        with patch.object(agent,'ROOT',self.root),patch.dict(os.environ,{'TAESAN_PC_PROBE_TOKEN':self.token if configured else ''}),\
             patch.object(agent,'db',side_effect=AssertionError('Order/staff DB must not be opened')),\
             patch.object(agent,'dispatch',side_effect=AssertionError('Kakao staff dispatcher must not be called')):
            handler.do_POST()
        return result[0]
    def test_real_handler_routes_only_test_probe(self):
        code,data=self.call();self.assertEqual(code,200);self.assertFalse(data['sheet_written'])
        self.assertEqual([p.name for p in self.root.iterdir()],['planning-probe.sqlite3'])
        self.assertTrue(self.call()[1]['duplicate'])
    def test_missing_configuration_or_auth_opens_no_database(self):
        self.assertEqual(self.call(configured=False)[0],401)
        self.assertEqual(self.call(auth='Bearer wrong')[0],401)
        self.assertEqual(list(self.root.iterdir()),[])
    def test_wrong_size_and_malformed_content_rejected(self):
        for size in ['0','1025','not-a-number','-1']:
            self.assertEqual(self.call(length=size)[0],400)
        self.assertEqual(self.call(body=['invalid'])[0],422)
    def test_ordinary_order_does_not_reach_order_dispatch(self):
        self.assertEqual(self.call(body=dict(self.payload,text='주문 접수 synthetic'))[0],422)
