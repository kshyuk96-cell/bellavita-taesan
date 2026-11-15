"""Invitation checkout regression: isolated SQLite, synthetic orders, mocked PayApp."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import checkout
import order_operations

CONFIG={'USER_ID':'synthetic','LINK_KEY':'key','LINK_VALUE':'value','REQUEST_KEY':'test-only'}
PRODUCT={'number':'101','title':'가상 행사','price':93500,'images':[], 'groups':[{'label':'상품 선택','choices':{'ten':{'label':'10kg','amount':0,'custom':False},'twenty':{'label':'20kg','amount':20000,'custom':False}}}], 'venue':'가상 문화홀','address':'서울 테스트로 10','event_time':'2099-12-13T13:00:00+09:00'}
FIELDS={'attempt':'a'*32,'choice0':'twenty','sender':'가상 보내는 분','same_buyer':'yes','buyer':'변조된 이름','buyer_phone':'010-0000-0000','consent':'yes','amount':'1'}

class CheckoutIntegrity(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        with self.db() as c:checkout.setup(c);order_operations.setup(c)
    def tearDown(self):self.temp.cleanup()
    def db(self):
        c=sqlite3.connect(self.root/'state.sqlite3');c.row_factory=sqlite3.Row;return c
    def start(self):
        with patch.object(checkout,'payapp_request',return_value=('123','https://payapp.kr/test')) as provider:
            url=checkout.start_order(self.db,'b'*32,93500,PRODUCT,FIELDS,'https://example.org',CONFIG)
        return url,provider.call_count
    def event(self,state,**extra):
        date='20261115120000'
        return {'userid':CONFIG['USER_ID'],'linkkey':'key','linkval':'value','hash':checkout.signature(CONFIG,date),'responsedate':date,'pay_state':state,'var1':'a'*32,'mul_no':'123','price':'113500',**extra}
    def state(self):
        with self.db() as c:return c.execute('SELECT state FROM invitation_orders').fetchone()[0]
    def test_recomputed_price_same_buyer_snapshot_and_retry(self):
        url,calls=self.start();self.assertEqual(calls,1)
        with self.db() as c:details=json.loads(c.execute('SELECT details FROM invitation_orders').fetchone()[0])
        self.assertEqual(details['amount'],113500);self.assertEqual(details['buyer'],FIELDS['sender']);self.assertEqual(details['product_snapshot'],PRODUCT)
        with patch.object(checkout,'payapp_request',side_effect=AssertionError('duplicate provider request')):
            self.assertEqual(checkout.start_order(self.db,'b'*32,93500,copy.deepcopy(PRODUCT),FIELDS,'https://example.org',CONFIG),url)
    def test_uncertain_response_cannot_retry_payment(self):
        with patch.object(checkout,'payapp_request',side_effect=OSError('timeout')) as provider:
            with self.assertRaises(OSError):checkout.start_order(self.db,'b'*32,93500,PRODUCT,FIELDS,'https://example.org',CONFIG)
            with self.assertRaises(ValueError):checkout.start_order(self.db,'b'*32,93500,PRODUCT,FIELDS,'https://example.org',CONFIG)
            self.assertEqual(provider.call_count,1)
        self.assertEqual(self.state(),'requesting')
    def test_callbacks_duplicate_partial_full_and_late_paid(self):
        self.start()
        for _ in range(2):self.assertTrue(checkout.receive_payment(self.db,self.event('4'),CONFIG))
        partial=self.event('70',orig_mul_no='123',orig_price='113500',mul_no='124',price='10000')
        self.assertTrue(checkout.receive_payment(self.db,partial,CONFIG));self.assertTrue(checkout.receive_payment(self.db,{**partial,'pay_state':'71'},CONFIG))
        self.assertEqual(self.state(),'partial_cancel')
        self.assertFalse(checkout.receive_payment(self.db,{**partial,'price':'9999'},CONFIG))
        self.assertTrue(checkout.receive_payment(self.db,self.event('9'),CONFIG))
        self.assertTrue(checkout.receive_payment(self.db,self.event('4'),CONFIG));self.assertEqual(self.state(),'cancelled')
        with self.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM order_operations').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT state FROM order_operations').fetchone()[0],'payment_hold')
            self.assertEqual(c.execute("SELECT SUM(amount) FROM payment_events WHERE kind='partial'").fetchone()[0],10000)
    def test_cancelled_order_cannot_be_written_to_production_queue(self):
        self.start();checkout.receive_payment(self.db,self.event('4'),CONFIG)
        checkout.receive_payment(self.db,self.event('9'),CONFIG)
        key=order_operations.ledger_key('a'*32)
        with self.assertRaises(ValueError):
            order_operations.save(self.db,key,lambda _: {},lambda *args,**kwargs:self.fail('cancelled write'))

    def test_reject_forged_amount_hash_date_and_unknown_status(self):
        self.start()
        for fields in ({'hash':'bad'},{'price':'1'},{'responsedate':'20261315120000'},{'pay_state':'999'}):
            self.assertFalse(checkout.receive_payment(self.db,self.event('4',**fields),CONFIG))
        self.assertEqual(self.state(),'waiting')
    def test_backup_restore_preserves_snapshot_and_deduplication(self):
        self.start();checkout.receive_payment(self.db,self.event('4'),CONFIG)
        backup=sqlite3.connect(self.root/'restore.sqlite3')
        with self.db() as c:c.backup(backup)
        backup.close()
        def restored():
            c=sqlite3.connect(self.root/'restore.sqlite3');c.row_factory=sqlite3.Row;return c
        self.assertTrue(checkout.receive_payment(restored,self.event('4'),CONFIG))
        with restored() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM payment_events').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM order_operations').fetchone()[0],1)
            self.assertEqual(json.loads(c.execute('SELECT details FROM invitation_orders').fetchone()[0])['product_snapshot'],PRODUCT)
    def test_uncommitted_erp_snapshot_is_not_public(self):
        job='b'*32;folder=self.root/'invitations'/job;folder.mkdir(parents=True)
        (folder/'93500.order.json').write_text(json.dumps({**PRODUCT,'erp_publication':job}))
        self.assertIsNone(checkout.read(self.root,job,93500))
        public=self.root/'erp-invitations'/'public'/job;public.mkdir(parents=True)
        (public/'result.json').write_text(json.dumps({'status':'done','jobId':job}))
        for price in (93500,99000):
            (folder/f'{price}.order.json').write_text(json.dumps({**PRODUCT,'price':price,'erp_publication':job}));(public/f'{price}.html').write_text('synthetic')
        self.assertEqual(checkout.read(self.root,job,93500)['number'],'101')
        (folder/'99000.order.json').unlink();self.assertIsNone(checkout.read(self.root,job,93500))

if __name__=='__main__':unittest.main()
