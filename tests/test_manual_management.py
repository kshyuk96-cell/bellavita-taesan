"""Published price pairs must appear in product management without creating ERP orders."""
import contextlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import erp_invitation_management as management
import erp_invitation_library as library

class ProductManagement(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'journal.sqlite';self.engine=self.root/'erp-invitations';self.engine.mkdir()
  with sqlite3.connect(self.path) as c:c.execute('CREATE TABLE jobs(input TEXT,result TEXT,created_at TEXT,invitation_id TEXT,action TEXT,state TEXT,public_id TEXT)')
  @contextlib.contextmanager
  def db():
   c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
   try:yield c
   finally:c.close()
  self.worker=SimpleNamespace(root=self.engine,origin='https://erp.example',db=db)
 def tearDown(self):self.tmp.cleanup()
 def query(self):
  with patch.object(management,'notifications',return_value={}):return management.query(self.worker,self.root,{'from':['2099-01-01'],'to':['2099-01-31']})
 def test_native_published_two_prices_dedupe_preview_and_keep_native_order_urls(self):
  key='a'*32;local='b'*32;folder=self.engine/'public'/key;folder.mkdir(parents=True);native=self.root/'invitations'/local;native.mkdir(parents=True)
  values={'name':'혼주 한 분의 장녀 예식','date':'2099-01-28','time':'11:00','brideParents':'가상혼주','brideName':'가상신부','hostSide':'bride'}
  items=[]
  for price,number in [(93500,'101'),(99000,'102')]:
   link=self.worker.origin+f'/i/{local}/{price}/order';page=self.worker.origin+f'/erp/i/{key}/{price}.html'
   items.append({'price':price,'productId':number,'orderUrl':link,'url':page,'verified':True})
   (folder/f'{price}.html').write_text('<html>native published page</html>');(native/f'{price}.order.json').write_text(json.dumps({'localCheckout':True,'title':values['name'],'event_time':'2099-01-28T11:00:00+09:00'}))
  result={'jobId':key,'fingerprint':'f'*64,'items':items};(folder/'result.json').write_text(json.dumps(result));data={'kind':'wedding','orderMode':'invitation-checkout','invitationId':'IV-native-pair','values':values}
  with self.worker.db() as c:
   for action,when in [('preview','2099-01-01T01:00:00+09:00'),('publish','2099-01-01T02:00:00+09:00')]:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',(json.dumps(data),json.dumps(result),when,'IV-native-pair',action,'done',key))
   c.commit()
  result=self.query();self.assertEqual(len(result['products']),2);self.assertEqual(result['orders'],[])
  self.assertEqual({r['price'] for r in result['products']},{93500,99000});self.assertTrue(all(r['source']=='own-checkout' for r in result['products']))
  self.assertEqual({r['orderUrl'] for r in result['products']},{r['orderUrl'] for r in items});self.assertTrue(all(r['saleStatus']=='판매 중' for r in result['products']))
  self.assertEqual({r['brideParents'] for r in result['products']},{'가상혼주'})
  with sqlite3.connect(self.path) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],2)
 def test_external_pair_appears_as_two_read_only_products_without_local_engine_jobs(self):
  links=[{'price':price,'url':f'https://sample.chatgpt.site/{price}.html','orderUrl':f'https://testshop.shop.blogpay.co.kr/v/g/product{price}'} for price in (93500,99000)]
  with patch.dict('os.environ',{'BLOGPAY_SHOP_ID':'testshop'}):library.register_external_pair(self.root,{'kind':'wedding','title':'외부 예식','date':'2099-01-28','values':{'brideParents':'가상혼주'},'items':links},'staff')
  result=self.query();self.assertEqual(len(result['products']),2);self.assertEqual(result['orders'],[])
  self.assertTrue(all(r['external'] and r['readOnly'] for r in result['products']));self.assertEqual({r['orderUrl'] for r in result['products']},{r['orderUrl'] for r in links})
  with sqlite3.connect(self.path) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

if __name__=='__main__':unittest.main()
