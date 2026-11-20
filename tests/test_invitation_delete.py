import contextlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import erp_invitation_library as library
import erp_invitation_management as management
import checkout

class Delete(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.journal=self.root/'journal.sqlite';self.engine=self.root/'erp-invitations';self.engine.mkdir()
  with sqlite3.connect(self.journal) as c:c.execute('CREATE TABLE jobs(input TEXT,result TEXT,created_at TEXT,invitation_id TEXT,action TEXT,state TEXT,public_id TEXT)')
  @contextlib.contextmanager
  def db():
   c=sqlite3.connect(self.journal);c.row_factory=sqlite3.Row
   try:yield c
   finally:c.close()
  self.worker=SimpleNamespace(root=self.engine,origin='https://erp.example',db=db)
  self.key='a'*32;self.local='b'*32;folder=self.engine/'public'/self.key;folder.mkdir(parents=True)
  self.snapshots=self.root/'invitations'/self.local;self.snapshots.mkdir(parents=True)
  self.items=[]
  for price,number in [(93500,'101'),(99000,'102')]:
   order=f'https://erp.example/i/{self.local}/{price}/order';page=f'https://erp.example/erp/i/{self.key}/{price}.html';self.items.append({'price':price,'productId':number,'orderUrl':order,'url':page})
   (folder/f'{price}.html').write_text('<html>published</html>')
   (self.snapshots/f'{price}.order.json').write_text(json.dumps({'number':number,'localCheckout':True,'publicationId':self.key,'title':'기존 예식','event_time':'2099-12-13T13:00:00+09:00','releaseFingerprint':'f'*64}))
  result={'jobId':self.key,'fingerprint':'f'*64,'items':self.items};(folder/'result.json').write_text(json.dumps(result));job={'kind':'wedding','orderMode':'invitation-checkout','invitationId':'IV-test-delete','values':{'name':'기존 예식','date':'2099-12-13','time':'13:00'}}
  with self.worker.db() as c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',(json.dumps(job),json.dumps(result),'2099-01-01T00:00:00+09:00','IV-test-delete','publish','done',self.key));c.commit()
  with sqlite3.connect(self.root/'state.sqlite3') as c:
   c.execute('CREATE TABLE orders(id TEXT,payload TEXT)');c.execute('INSERT INTO orders VALUES(?,?)',('preserved-order','paid-data'));c.execute('CREATE TABLE payments(id TEXT,amount INTEGER)');c.execute('INSERT INTO payments VALUES(?,?)',('preserved-payment',99000));c.commit()
 def tearDown(self):self.tmp.cleanup()
 def test_delete_pair_hides_both_products_blocks_checkout_and_preserves_orders_payments_files(self):
  before=management.products(self.worker,self.root);self.assertEqual(len(before),2);self.assertEqual({p['libraryId'] for p in before},{self.key})
  done=library.update(self.root,self.key,{'action':'delete','revision':1},'staff')
  self.assertTrue(done['deleted']);self.assertFalse(done['operating']);self.assertEqual(management.products(self.worker,self.root),[])
  self.assertEqual(library.catalog(self.worker,self.root)['items'],[])
  self.assertEqual(library.catalog(self.worker,self.root)['summary']['deleted'],1)
  for price in library.PRICES:
   product=checkout.read(self.root,self.local,str(price));self.assertIsNotNone(product);self.assertFalse(checkout.available(product));self.assertTrue((self.snapshots/f'{price}.order.json').is_file())
  self.assertEqual(library.public(self.root,self.key,'93500.html')[0],410)
  with sqlite3.connect(self.root/'state.sqlite3') as c:
   self.assertEqual(c.execute('SELECT * FROM orders').fetchall(),[('preserved-order','paid-data')]);self.assertEqual(c.execute('SELECT * FROM payments').fetchall(),[('preserved-payment',99000)])
  self.assertTrue(library.update(self.root,self.key,{'action':'delete','revision':1},'staff')['replayed'])
  with self.assertRaisesRegex(ValueError,'삭제'):library.update(self.root,self.key,{'action':'sales','revision':2,'stopped':False},'staff')
  self.assertEqual(library.resolve_key(library.state(self.root),{'productId':'101'}),self.key)
 def test_preview_only_draft_does_not_create_phantom_product_rows(self):
  preview_items=[{'price':p,'productId':str(500+p),'orderUrl':f'https://erp.example/i/'+('d'*32)+f'/{p}/order'} for p in library.PRICES]
  with self.worker.db() as c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',(json.dumps({'kind':'wedding','values':{'name':'미발행 시안'}}),json.dumps({'items':preview_items}),'2099-01-01T01:00:00+09:00','IV-preview-only','preview','done','d'*32));c.commit()
  rows=management.products(self.worker,self.root)
  self.assertEqual(len(rows),2);self.assertTrue(all(row['invitationId']!='IV-preview-only' for row in rows));self.assertTrue(all(row['productId'] not in {str(500+p) for p in library.PRICES} for row in rows))
 def test_external_readonly_index_can_be_deleted_without_changing_external_links(self):
  payload={'kind':'wedding','title':'외부 예식','date':'2099-11-28','items':[{'price':p,'url':f'https://example.chatgpt.site/{p}.html','orderUrl':f'https://testshop.shop.blogpay.co.kr/v/g/p{p}'} for p in library.PRICES]}
  with patch.dict('os.environ',{'BLOGPAY_SHOP_ID':'testshop'}):r=library.register_external_pair(self.root,payload,'staff')
  key=r['id'];library.update(self.root,key,{'action':'delete','revision':1},'staff')
  self.assertTrue(library.state(self.root)[key]['deleted']);self.assertEqual([x['url'] for x in library.state(self.root)[key]['links']],[x['url'] for x in payload['items']])
  self.assertNotIn(key,[r['id'] for r in library.catalog(self.worker,self.root)['items']])
 def test_original_order_only_pair_is_discovered_without_invented_invitation_html(self):
  original='7f494a310ca9371914b8863b45732bd5';folder=self.root/'invitations'/original;folder.mkdir()
  for p in library.PRICES:(folder/f'{p}.order.json').write_text(json.dumps({'number':str(p),'title':'원래 자체 주문서','date':'2099년 12월 13일(일)','clock':'오후 1시','event_time':'2099-12-13T13:00:00+09:00','price':p,'localCheckout':False}))
  rows=library.catalog(self.worker,self.root)['items'];legacy=next(r for r in rows if r['id']==original)
  self.assertTrue(legacy['readOnly']);self.assertTrue(legacy['legacyOrderOnly']);self.assertEqual(legacy['date'],'2099-12-13')
  self.assertEqual([x['url'] for x in legacy['links']],[f'https://erp.example/i/{original}/{p}/order' for p in library.PRICES])
  products=[p for p in management.products(self.worker,self.root) if p['libraryId']==original]
  self.assertEqual(len(products),2);self.assertTrue(all(p['url']==p['orderUrl'] and p['readOnly'] for p in products))
  library.update(self.root,original,{'action':'delete','revision':1},'staff')
  self.assertNotIn(original,[r['id'] for r in library.catalog(self.worker,self.root)['items']])
  self.assertTrue(all(p['libraryId']!=original for p in management.products(self.worker,self.root)))
  for p in library.PRICES:self.assertFalse(checkout.available({'_libraryKey':original,'_libraryRoot':str(self.root),'event_time':'2099-12-13T13:00:00+09:00'}))
 def test_legacy_snapshot_date_normalization_uses_event_time_not_display_date(self):
  self.assertEqual(library.snapshot_date({'date':'2026년 12월 13일(일)','event_time':'2026-12-13T13:00:00+09:00'}),'2026-12-13')
  self.assertEqual(library.snapshot_date({'date':'2026년 12월 13일(일)'}),'2026-12-13')
  self.assertEqual(library.snapshot_date({'date':'2026-12-13'}),'2026-12-13')
  self.assertEqual(library.snapshot_date({'date':'2099년 2월 30일(월)'}),'')
  self.assertEqual(library.snapshot_date({'date':'2026-12-13','event_time':'2026-12-12T16:00:00Z'}),'2026-12-13')
 def test_expired_order_only_pair_is_visible_but_not_counted_operating(self):
  key='c'*32;folder=self.root/'invitations'/key;folder.mkdir()
  for p in library.PRICES:(folder/f'{p}.order.json').write_text(json.dumps({'title':'과거 주문서','date':'2001-01-01','price':p,'localCheckout':True}))
  rows=library.catalog(self.worker,self.root)['items'];item=next(r for r in rows if r['id']==key);self.assertFalse(item['operating']);self.assertEqual(item['saleStatus'],'운영 종료')
  self.assertEqual(len(rows),2)

if __name__=='__main__':unittest.main()
