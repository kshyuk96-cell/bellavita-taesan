"""Product edits affect future fulfillment only, preserving original releases and payments."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import checkout
import erp_invitation_library as library
import erp_invitation_management as management
import erp_invitation_product_edits as edits


class ProductEdits(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.job='7f494a310ca9371914b8863b45732bd5';self.folder=self.root/'invitations'/self.job;self.folder.mkdir(parents=True)
        groups=[{'label':label,'choices':{'1':{'label':'기본 선택','amount':0,'custom':False}}} for label in ('축하 문구','받으시는 분','상품 선택')]
        for price in (93500,99000):
            product={'number':str(price+100),'title':'원본 예식','price':price,'images':[],'groups':groups,'date':'2099년 12월 13일(일)','clock':'오후 1시','venue':'원본홀','address':'서울 원본로 10','event_time':'2099-12-13T13:00:00+09:00','localCheckout':False}
            (self.folder/f'{price}.order.json').write_text(json.dumps(product,ensure_ascii=False))
        with sqlite3.connect(self.root/'state.sqlite3') as db:
            checkout.setup(db);db.execute('CREATE TABLE payments(id TEXT,amount INTEGER,state TEXT)')
            db.execute('INSERT INTO invitation_orders VALUES(?,?,?,?,?,?,?,?)',('existing-paid',self.job,93500,'2099-12-01',json.dumps({'title':'원본 예식','venue':'원본홀','address':'서울 원본로 10','amount':93500}),'paid','payment-001','https://example.org/pay'))
            db.execute('INSERT INTO payments VALUES(?,?,?)',('payment-001',93500,'paid'))
        self.journal=self.root/'journal.sqlite'
        with sqlite3.connect(self.journal) as db:db.execute('CREATE TABLE jobs(input TEXT,result TEXT,created_at TEXT,invitation_id TEXT,action TEXT,state TEXT,public_id TEXT)')
        @contextlib.contextmanager
        def db():
            connection=sqlite3.connect(self.journal);connection.row_factory=sqlite3.Row
            try:yield connection
            finally:connection.close()
        self.worker=SimpleNamespace(root=self.root/'erp-invitations',origin='https://erp.example',db=db)
        self.worker.root.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def payload(self,**changes):
        return {'productId':'own:'+self.job+':93500','revision':0,'fields':{'title':'수정 예식','venue':'수정홀','address':'서울 수정로 20','orderNotice':'주문 안내 <script>alert(1)</script>\n두 번째 줄'},**changes}
    def test_legacy_original_snapshots_and_paid_database_remain_byte_identical(self):
        before={p:p.read_bytes() for p in [*self.folder.iterdir(),self.root/'state.sqlite3']}
        result=edits.update(self.root,self.payload(),'검증 직원')
        self.assertEqual(result['revision'],1);self.assertEqual(result['price'],93500)
        for path,raw in before.items():self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(checkout.read(self.root,self.job,'99000')['title'],'원본 예식')
        with sqlite3.connect(self.root/'state.sqlite3') as db:
            self.assertEqual(json.loads(db.execute('SELECT details FROM invitation_orders').fetchone()[0])['title'],'원본 예식')
            self.assertEqual(db.execute('SELECT amount,state FROM payments').fetchone(),(93500,'paid'))
    def test_future_checkout_and_order_use_latest_text_and_escape_notice(self):
        edits.update(self.root,self.payload())
        product=checkout.read(self.root,self.job,'93500');page=checkout.render(product,'#',True)
        self.assertIn('수정 예식',page);self.assertIn('서울 수정로 20',page);self.assertIn('수정홀',page)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',page);self.assertNotIn('<script>alert(1)</script>',page)
        order=checkout.normalize_order(product,{'consent':'yes','choice0':'1','choice1':'1','choice2':'1','sender':'가상 보내는분','same_buyer':'yes','buyer_phone':'01000000001'})
        self.assertEqual((order['title'],order['venue'],order['address'],order['amount']),('수정 예식','수정홀','서울 수정로 20',93500))
    def test_numeric_published_id_and_legacy_own_id_share_revision(self):
        result=edits.update(self.root,self.payload(productId='93600'));self.assertEqual(result['revision'],1)
        self.assertEqual(edits.detail(self.root,'own:'+self.job+':93500')['title'],'수정 예식')
        with self.assertRaisesRegex(ValueError,'다른 직원'):edits.update(self.root,self.payload())
        second=edits.update(self.root,self.payload(revision=1,fields={'orderNotice':''}));self.assertEqual(second['orderNotice'],'');self.assertEqual(second['title'],'수정 예식')
    def test_financial_and_identity_mutations_and_external_products_are_rejected(self):
        for fields in ({'price':'1'},{'number':'123'},{'groups':[]},{'payment_status':'paid'},{'title':''},{'address':' '},{'venue':3}):
            with self.subTest(fields=fields),self.assertRaises(ValueError):edits.update(self.root,self.payload(fields=fields))
        for product_id in ('external:abc:93500','https://example.org/order','999999'):
            with self.assertRaises(ValueError):edits.detail(self.root,product_id)
        self.assertFalse(edits.path(self.root).exists())
    def test_management_rows_apply_overlay_even_when_legacy_invitation_is_read_only(self):
        edits.update(self.root,self.payload())
        rows=management.products(self.worker,self.root);self.assertEqual(len(rows),2)
        changed=next(row for row in rows if row['price']==93500)
        self.assertTrue(changed['readOnly']);self.assertTrue(changed['canEditProduct']);self.assertEqual(changed['productEditRevision'],1)
        self.assertEqual((changed['name'],changed['venue'],changed['address']),('수정 예식','수정홀','서울 수정로 20'))
    def test_shared_sales_and_delete_gates_are_unchanged_by_text_edits(self):
        row=library.catalog(self.worker,self.root)['items'][0]
        state=library.state(self.root);state[row['id']]['salesStopped']=True;library.save(self.root,state)
        edits.update(self.root,self.payload());self.assertFalse(checkout.available(checkout.read(self.root,self.job,'93500')))
        row=library.state(self.root)[self.job];library.update(self.root,self.job,{'action':'delete','revision':row['revision']},'검증 직원')
        self.assertFalse(checkout.available(checkout.read(self.root,self.job,'93500')));self.assertEqual(management.products(self.worker,self.root),[])
    def test_routes_return_detail_and_stale_conflict_without_other_mutation(self):
        replies=[]
        body=json.dumps(self.payload()).encode()
        handler=SimpleNamespace(path='/erp/invitations/product-update',headers={'Content-Length':str(len(body))},rfile=io.BytesIO(body),send_json=lambda value,status=200:replies.append((status,value)))
        self.assertTrue(management.dispatch(handler,'POST','/erp/invitations/product-update',self.root,self.worker));self.assertEqual(replies[-1][0],200)
        handler.rfile=io.BytesIO(body);management.dispatch(handler,'POST','/erp/invitations/product-update',self.root,self.worker);self.assertEqual(replies[-1][0],409)
        handler.path='/erp/invitations/product-detail?productId=own:'+self.job+':93500'
        management.dispatch(handler,'GET','/erp/invitations/product-detail',self.root,self.worker);self.assertEqual(replies[-1][1]['revision'],1)

if __name__=='__main__':unittest.main()
