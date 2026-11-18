import contextlib
import datetime as dt
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import erp_invitation_management as m
import order_operations
import checkout

class Management(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.journal=self.root/'journal.sqlite'
  c=sqlite3.connect(self.journal);c.execute('CREATE TABLE jobs(input TEXT,result TEXT,created_at TEXT,invitation_id TEXT,action TEXT,state TEXT)');data={'kind':'wedding','values':{'name':'가상 예식','date':'2099-01-01','time':'12:00','venue':'검증홀','address':'가상 주소','groomName':'가상신랑','brideName':'가상신부'}};c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(json.dumps(data),json.dumps({'items':[{'productId':'123','price':99000,'orderUrl':'https://example.org/order'}]}),'2098-12-01','iv1','publish','done'));c.commit();c.close()
  @contextlib.contextmanager
  def db():
   c=sqlite3.connect(self.journal);c.row_factory=sqlite3.Row
   try:yield c
   finally:c.close()
  self.worker=SimpleNamespace(db=db,origin="https://example.org")
  c=sqlite3.connect(self.root/'state.sqlite3');checkout.setup(c);order_operations.setup(c);c.execute('CREATE TABLE staff(owner TEXT)');c.commit();c.close()
 def tearDown(self):self.temp.cleanup()
 def test_filters_unknown_goods_preserves_cancel_and_does_not_write(self):
  class Client:
   def __init__(self,*a):pass
   def request(self,endpoint,**fields):
    self.assertions=endpoint
    return {'orderList':{'x':{'orderNum':'test-order','orderState':'4','registDate':'2099-01-01','orderName':'가상 주문자','orderTel':'01000000000','goodList':{'a':{'goodNum':'123','orderGoodNum':'1','orderQty':'1','orderPrice':'99000'},'b':{'goodNum':'999','orderGoodNum':'2'}}}},'pagenation':{'pageCount':2}}
  with patch.dict(os.environ,{'BLOGPAY_SHOP_ID':'testshop','BLOGPAY_API_TOKEN':'fake'}),patch('blogpay.BlogPay',Client):r=m.query(self.worker,self.root,{'from':['2099-01-01'],'to':['2099-01-02']})
  self.assertEqual(len(r['orders']),0);self.assertEqual(len(r['products']),0);self.assertEqual(r['pages'],1)
  c=sqlite3.connect(self.root/'state.sqlite3');self.assertEqual(c.execute('SELECT COUNT(*) FROM order_sms').fetchone()[0],0);c.close()
 def test_sms_audit_never_claims_delivery_or_sends(self):
  with patch.dict(os.environ,{'SOLAPI_API_KEY':'x','SOLAPI_API_SECRET':'y','SOLAPI_FROM':'z'}):r=m.notifications(self.root)
  self.assertTrue(r['providerConfigured']);self.assertFalse(r['queueEnabled']);self.assertEqual(r['customer']['state'],'not-configured');self.assertIsNone(r['staff']['delivered'])
 def test_staff_recipient_configuration_is_counted_without_sending(self):
  with patch.dict(os.environ,{'INVITATION_STAFF_NOTIFY_PHONES':'010-0000-0001,01000000001,invalid'}):r=m.notifications(self.root)
  self.assertEqual(r['staff']['recipients'],1);self.assertFalse(r['queueEnabled']);self.assertIsNone(r['staff']['delivered'])
 def test_invalid_period_rejected(self):
  with self.assertRaises(ValueError):m.query(self.worker,self.root,{'from':['2099-02-01'],'to':['2099-01-01']})
 def test_unauthenticated_management_is_denied_by_bridge(self):
  import erp_invitations
  h=SimpleNamespace(headers={},send_json=lambda data,status=200:setattr(self,'status',status))
  with patch.dict(os.environ,{'ERP_INVITATION_TOKEN':'test'}):self.assertTrue(erp_invitations.dispatch(h,'GET','/erp/invitations/management',self.root,'https://example.org',None,None))
  self.assertEqual(self.status,401)
