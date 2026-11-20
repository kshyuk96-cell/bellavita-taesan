import contextlib
import datetime as dt
import json
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import checkout
import order_operations
import order_sms
import erp_invitation_management


class OrderNotifications(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'state.sqlite3'
        @contextlib.contextmanager
        def db():
            c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row
            try:
                yield c
                c.commit()
            except BaseException:
                c.rollback();raise
            finally:c.close()
        self.db=db
        with db() as c:
            checkout.setup(c);order_operations.setup(c);order_sms.setup(c)
            c.execute('CREATE TABLE staff(owner TEXT)')
        self.key='a'*32;self.order={'amount':93500,'buyer':'가상 주문자','buyer_phone':'010-0000-0001','sender':'검증 단체','title':'가상 예식 쌀빛그림화환','event_time':'2099-01-01T12:00:00+09:00','venue':'검증홀','options':[]}
        self.config={'USER_ID':'fixture','LINK_KEY':'key','LINK_VALUE':'value','REQUEST_KEY':'secret'}
        self.env={'INVITATION_SMS_PROVIDER':'solapi','INVITATION_ORDER_SMS_ENABLED':'1','INVITATION_STAFF_NOTIFY_PHONES':'01000000002,010-0000-0002'}
        with db() as c:c.execute('INSERT INTO invitation_orders VALUES (?,?,?,?,?,?,?,?)',(self.key,'b'*32,93500,dt.datetime.now(dt.timezone.utc).isoformat(),json.dumps(self.order),'waiting','12345','https://payapp.kr/test'))
    def tearDown(self):self.tmp.cleanup()
    def paid(self,**changes):
        stamp='20990101120000'
        fields={'responsedate':stamp,'userid':'fixture','linkkey':'key','linkval':'value','hash':checkout.signature(self.config,stamp),'pay_state':'4','var1':self.key,'mul_no':'12345','price':'93500',**changes}
        return checkout.receive_payment(self.db,fields,self.config)
    def rows(self):
        with self.db() as c:return [dict(x) for x in c.execute('SELECT * FROM invitation_notice_outbox ORDER BY audience')]
    def test_paid_callback_enqueues_customer_and_staff_once_without_sheet_registration(self):
        with patch.dict(os.environ,self.env):
            self.assertTrue(self.paid());self.assertTrue(self.paid())
        rows=self.rows();self.assertEqual([r['audience'] for r in rows],['customer','staff']);self.assertTrue(all(r['state']=='pending' for r in rows))
        with self.db() as c:self.assertEqual(c.execute('SELECT state FROM order_operations').fetchone()[0],'pending')
    def test_bad_payment_signature_or_waiting_payment_does_not_notify(self):
        with patch.dict(os.environ,self.env):
            self.assertFalse(self.paid(hash='wrong'))
            self.assertTrue(self.paid(pay_state='1'))
        self.assertEqual(self.rows(),[])
    def test_dispatch_is_independent_of_sheet_worker_and_records_acceptance_not_delivery(self):
        sent=[]
        with patch.dict(os.environ,self.env):
            self.paid()
            for _ in range(3):order_sms.send_pending(self.db,'https://example.org',sender=lambda to,text,key:(sent.append((to,text,key)) or 'provider-group'),enabled=lambda:True)
        self.assertEqual(len(sent),2);self.assertTrue(all(r['state']=='accepted' for r in self.rows()))
        self.assertIn('직원이 주문 내용을 확인',next(msg for to,msg,_ in sent if to=='01000000001'))
        self.assertIn('승인 대기',next(msg for to,msg,_ in sent if to=='01000000002'))
        self.assertTrue(any('/invitation-orders/' in text for _,text,_ in sent));self.assertTrue(any('/staff-orders?' in text for _,text,_ in sent))
        with patch.dict(os.environ,self.env):audit=erp_invitation_management.notifications(self.root)
        self.assertIsNone(audit['customer']['delivered']);self.assertEqual(audit['customer']['accepted'],1)
    def test_uncertain_provider_timeout_is_never_automatically_retried(self):
        calls=[]
        def fail(*args):calls.append(args);raise TimeoutError('ambiguous result')
        with patch.dict(os.environ,self.env):
            self.paid()
            for _ in range(4):order_sms.send_pending(self.db,'https://example.org',sender=fail,enabled=lambda:True)
        self.assertEqual(len(calls),2);self.assertTrue(all(r['state']=='uncertain' for r in self.rows()))
    def test_missing_provider_or_disabled_flag_never_calls_sender(self):
        with patch.dict(os.environ,self.env):self.paid()
        with patch.dict(os.environ,{'INVITATION_ORDER_SMS_ENABLED':'0'}):
            self.assertFalse(order_sms.send_pending(self.db,'https://example.org',sender=lambda *a:self.fail('must not send'),enabled=lambda:True))
        with patch.dict(os.environ,self.env):
            self.assertFalse(order_sms.send_pending(self.db,'https://example.org',sender=lambda *a:self.fail('must not send'),enabled=lambda:False))
        self.assertTrue(all(r['state']=='pending' for r in self.rows()))
    def test_no_historical_scan_even_when_paid_rows_exist(self):
        with self.db() as c:c.execute("UPDATE invitation_orders SET state='paid'")
        with patch.dict(os.environ,self.env):
            self.assertFalse(order_sms.send_pending(self.db,'https://example.org',sender=lambda *a:self.fail('must not send'),enabled=lambda:True))
        self.assertEqual(self.rows(),[])
    def test_cancelled_order_does_not_send_previously_queued_notice(self):
        with patch.dict(os.environ,self.env):
            self.paid();self.paid(pay_state='8')
            self.assertFalse(order_sms.send_pending(self.db,'https://example.org',sender=lambda *a:self.fail('must not send'),enabled=lambda:True))
    def test_same_phone_staff_duplicates_are_merged_but_customer_and_staff_are_separate(self):
        with patch.dict(os.environ,{**self.env,'INVITATION_STAFF_NOTIFY_PHONES':'01000000001,010-0000-0001'}):self.paid()
        self.assertEqual(len(self.rows()),2)
    def test_native_checkout_payment_to_solapi_to_erp_management_with_staff_approval_pending(self):
        from types import SimpleNamespace
        self.order['options']=[['받으시는 분','신부 이다온 혼주님'],['상품 선택','쌀빛 10kg']]
        with self.db() as c:c.execute('UPDATE invitation_orders SET details=? WHERE id=?',(json.dumps(self.order),self.key))
        created=dt.datetime.now(dt.timezone.utc).date().isoformat()
        provider_calls=[]
        class Provider:
            def open(inner,request,timeout):
                provider_calls.append(json.loads(request.data))
                self.assertEqual(request.full_url,'https://api.solapi.com/messages/v4/send-many/detail')
                self.assertTrue(request.get_header('Authorization').startswith('HMAC-SHA256 apiKey='))
                return io.BytesIO(json.dumps({'groupInfo':{'groupId':'verified-provider-acceptance'}}).encode())
        env={**self.env,'SOLAPI_API_KEY':'test-api-key','SOLAPI_API_SECRET':'test-api-secret','SOLAPI_FROM':'0310000000'}
        worker=SimpleNamespace(origin='https://example.org')
        # An unpaid checkout is listed for staff review, without sending anything.
        with patch('erp_invitation_management.products',return_value=[]):
            before=erp_invitation_management.query(worker,self.root,{'from':[created],'to':[created]})
        self.assertEqual(before['orders'][0]['state'],'waiting');self.assertEqual(self.rows(),[])
        with patch.dict(os.environ,env),patch('order_sms.build_opener',return_value=Provider()):
            self.assertTrue(self.paid())
            self.assertTrue(self.paid())  # repeated authenticated PayApp callback
            for _ in range(3):order_sms.send_pending(self.db,'https://example.org')
            with patch('erp_invitation_management.products',return_value=[]):
                after=erp_invitation_management.query(worker,self.root,{'from':[created],'to':[created]})
        self.assertEqual(len(provider_calls),2)
        self.assertEqual({call['messages'][0]['to'] for call in provider_calls},{'01000000001','01000000002'})
        self.assertEqual(len({call['messages'][0]['customFields']['orderNotice'] for call in provider_calls}),2)
        self.assertEqual(len(after['orders']),1)
        row=after['orders'][0]
        self.assertEqual((row['id'],row['state'],row['amount']),('payapp:'+self.key,'paid',93500))
        self.assertEqual(row['fields']['recipient'],'신부 이다온 혼주님')
        self.assertEqual(row['fields']['sender'],'검증 단체')
        self.assertEqual(row['quantity'],1)
        self.assertEqual(after['notifications']['customer']['accepted'],1)
        self.assertEqual(after['notifications']['staff']['accepted'],1)
        self.assertIsNone(after['notifications']['customer']['delivered'])
        # No Sheet or ERP order is registered by the callback; staff still approves.
        with self.db() as c:
            self.assertEqual(c.execute('SELECT state FROM order_operations').fetchone()[0],'pending')
            self.assertIsNone(c.execute('SELECT order_number FROM order_operations').fetchone()[0])

    def test_expired_queue_is_not_sent(self):
        with patch.dict(os.environ,self.env):
            self.paid()
            with self.db() as c:c.execute('UPDATE invitation_notice_outbox SET created=0')
            self.assertFalse(order_sms.send_pending(self.db,'https://example.org',sender=lambda *a:self.fail('must not send'),enabled=lambda:True))
        self.assertTrue(all(r['state']=='expired' for r in self.rows()))

if __name__=='__main__':unittest.main()
