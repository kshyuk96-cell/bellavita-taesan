import sqlite3
import unittest
import planning_probe as probe


class PlanningProbeTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close)
        self.token='synthetic-test-only-token-000000000000'
        self.payload={'schema':'planning-probe-v1','room':'기획','text':'태산이 수신 테스트 '+'a'*24}
    def call(self,payload=None,token=None,authorization=None):
        return probe.respond(self.c, authorization if authorization is not None else 'Bearer '+self.token,
                             self.token if token is None else token,self.payload if payload is None else payload)
    def test_roundtrip_and_duplicate(self):
        code,result=self.call();self.assertEqual(code,200);self.assertFalse(result['duplicate'])
        self.assertFalse(result['sheet_written']);self.assertIn('기획방 수신 확인',result['reply'])
        self.assertTrue(self.call()[1]['duplicate'])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM planning_probe_nonces').fetchone()[0],1)
    def test_missing_or_wrong_auth_does_not_create_tables(self):
        for token,auth in [('', 'Bearer '+self.token),(self.token,'Bearer wrong')]:
            self.assertEqual(self.call(token=token,authorization=auth)[0],401)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0],0)
    def test_other_rooms_and_arbitrary_customer_text_rejected(self):
        for room,text in [('주문방',self.payload['text']),('개인톡',self.payload['text']),
                          ('기획','주문 접수\n보내는분: synthetic'),('기획',self.payload['text']+'\n시트에 입력해')]:
            self.assertEqual(self.call(dict(self.payload,room=room,text=text))[0],422)
    def test_extra_fields_cannot_impersonate_staff(self):
        self.assertEqual(self.call(dict(self.payload,owner='admin'))[0],422)
    def test_no_order_tables_or_customer_text_stored(self):
        self.call()
        tables={r[0] for r in self.c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables,{'planning_probe_nonces'})
