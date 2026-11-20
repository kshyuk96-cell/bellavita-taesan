import contextlib,datetime,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import erp_manual_blogpay as m
import erp_invitation_library as library
import erp_invitations as bridge

class ManualBlogpay(unittest.TestCase):
 def pair(self):return {'blogpay93500Url':'https://testshop.shop.blogpay.co.kr/v/g/abc1','blogpay99000Url':'https://testshop.shop.blogpay.co.kr/v/g/abc2'}
 def test_pair_urls_reject_other_stores_credentials_ports_private_hosts_and_duplicates(self):
  with patch.dict(os.environ,{'BLOGPAY_SHOP_ID':'testshop'}):
   self.assertEqual(len(m.validate_pair(self.pair())),2)
   for bad in ['https://other.shop.blogpay.co.kr/v/g/a','https://testshop.shop.blogpay.co.kr:444/v/g/a','https://u:p@testshop.shop.blogpay.co.kr/v/g/a','http://testshop.shop.blogpay.co.kr/v/g/a','https://127.0.0.1/v/g/a','https://testshop.shop.blogpay.co.kr/v/g/a#x','https://evil.test/v/g/a']:
    with self.subTest(bad=bad),self.assertRaises(ValueError):m.validate_pair({**self.pair(),'blogpay93500Url':bad})
   with self.assertRaises(ValueError):m.validate_pair({**self.pair(),'blogpay99000Url':self.pair()['blogpay93500Url']})
   with self.assertRaises(ValueError):m.validate_pair({'blogpay93500Url':self.pair()['blogpay93500Url']})
 def test_targeted_verification_never_lists_or_creates_products_and_preserves_urls(self):
  detail=lambda n:{'goodNum':n,'goodPrice':93500 if n=='101' else 99000,'isShow':'1','isSoldout':'0'}
  client=SimpleNamespace(detail=detail)
  reader=lambda url,shop:'<input name="goodNum" value="'+('101' if url.endswith('1') else '102')+'">'
  with patch.dict(os.environ,{'BLOGPAY_SHOP_ID':'testshop'}):
   items=m.products(self.pair(),client,reader)
  self.assertEqual([x['orderUrl'] for x in items],list(self.pair().values()))
  self.assertEqual([x['productId'] for x in items],['101','102'])
  self.assertTrue(all(x['verified'] for x in items))
 def test_wrong_price_and_ambiguous_ids_fail_closed(self):
  with self.assertRaises(ValueError):m.product_number('<input name="goodNum" value="101"><script>goodNum="102";</script>')
  with patch.dict(os.environ,{'BLOGPAY_SHOP_ID':'testshop'}),self.assertRaises(ValueError):m.products(self.pair(),SimpleNamespace(detail=lambda n:{'goodNum':n,'goodPrice':1}),lambda u,s:'<input name="goodNum" value="101">')
 def test_photo_validation_failure_creates_no_checkout_or_blogpay_products(self):
  from PIL import Image
  worker=bridge.Engine.__new__(bridge.Engine);worker.image=lambda value:Image.new('RGB',(20,20),'red')
  values=bridge.clean_values({'name':'가상 예식','date':'2099-11-28','time':'11:00','venue':'가상홀','address':'가상주소','groomName':'가상신랑','brideName':'가상신부','groomParents':'가상부모','brideParents':'가상혼주','groomRelation':'장남','brideRelation':'장녀','hero':'https://photo.test/a','frame':'https://photo.test/b','scrollLeft':'https://photo.test/c','scrollRight':'https://photo.test/d'})
  with patch('erp_invitation_auto.components',side_effect=ValueError('face crop invalid')),patch('erp_local_checkout.build') as own,patch.object(worker,'products') as products,patch('erp_manual_blogpay.products') as manual:
   for mode in ('invitation-checkout','blogpay-direct','blogpay-manual'):
    with self.assertRaisesRegex(ValueError,'face crop'):worker.prepare({'kind':'wedding','orderMode':mode,'values':values})
   own.assert_not_called();products.assert_not_called();manual.assert_not_called()
 def test_clean_values_accepts_manual_pair_without_changing_own_checkout_default(self):
  with patch.dict(os.environ,{'BLOGPAY_SHOP_ID':'testshop'}):self.assertEqual(bridge.clean_values(self.pair())['blogpay99000Url'],self.pair()['blogpay99000Url'])
  self.assertEqual(bridge.clean_values({})['blogpay99000Url'],'')

class ExternalPair(unittest.TestCase):
 def payload(self):return {'action':'register-external-pair','kind':'wedding','title':'혼주 한 분의 장녀 예식','date':'2099-11-28','values':{'brideParents':'혼주 한 분','brideName':'가상신부','hostSide':'bride'},'items':[{'price':93500,'url':'https://example.chatgpt.site/93500.html','orderUrl':'https://testshop.shop.blogpay.co.kr/v/g/a1'},{'price':99000,'url':'https://example.chatgpt.site/99000.html','orderUrl':'https://testshop.shop.blogpay.co.kr/v/g/a2'}]}
 def empty_worker(self,root):
  dbpath=root/'db.sqlite';c=sqlite3.connect(dbpath);c.execute('CREATE TABLE jobs(input,result,public_id,created_at,action,state)');c.commit();c.close()
  @contextlib.contextmanager
  def db():
   c=sqlite3.connect(dbpath);c.row_factory=sqlite3.Row
   try:yield c
   finally:c.close()
  return SimpleNamespace(root=root/'erp-invitations',origin='https://api.test',db=db)
 def test_registration_is_idempotent_preserves_two_original_urls_and_counts_one_invitation(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'BLOGPAY_SHOP_ID':'testshop'}):
   root=Path(tmp);p=self.payload();first=library.register_external_pair(root,p,'staff');second=library.register_external_pair(root,p,'staff')
   self.assertEqual(first['id'],second['id']);self.assertTrue(second['replayed'])
   data=library.catalog(self.empty_worker(root),root);self.assertEqual(data['summary']['operating'],1);self.assertEqual(data['summary']['links'],2)
   row=data['items'][0];self.assertEqual([x['url'] for x in row['links']],[x['url'] for x in p['items']]);self.assertTrue(row['external'])
   self.assertEqual(row['values']['brideParents'],'혼주 한 분')
   self.assertTrue(library.detail(root,row)['readOnly'])
   for action in ['sales','edit','preview']:
    with self.assertRaisesRegex(ValueError,'외부 발행'):library.update(root,row['id'],{'revision':1,'action':action,'stopped':True},'staff')
   self.assertIsNone(library.public(root,row['id'],'93500.html'))
 def test_registration_requires_distinct_price_urls_and_safe_public_host(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'BLOGPAY_SHOP_ID':'testshop'}):
   for mutate in [lambda p:p['items'][1].update(price=93500),lambda p:p['items'][1].update(url=p['items'][0]['url']),lambda p:p['items'][0].update(url='https://127.0.0.1/a'),lambda p:p.update(values=['bad']),lambda p:p.update(date='20991128')]:
    p=self.payload();mutate(p)
    with self.assertRaises(ValueError):library.register_external_pair(Path(tmp),p,'staff')
 def test_manual_publication_is_not_rewritten_to_own_checkout(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);w=self.empty_worker(root);key='f'*32;folder=w.root/'public'/key;folder.mkdir(parents=True)
   items=self.payload()['items'];result={'jobId':key,'fingerprint':'e'*64,'items':items};job={'orderMode':'blogpay-manual','kind':'wedding','values':{'name':'가상예식','date':'2099-11-28'}}
   for price in library.PRICES:(folder/f'{price}.html').write_text('<html>existing</html>')
   (folder/'result.json').write_text(json.dumps(result))
   with w.db() as c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(json.dumps(job),json.dumps(result),key,library.now().isoformat(),'publish','done'));c.commit()
   with patch('erp_local_checkout.build',side_effect=AssertionError('manual links must not be replaced')):
    a=library.catalog(w,root);b=library.catalog(w,root)
   self.assertEqual(a['items'][0]['links'],items);self.assertEqual(b['items'][0]['links'],items)

if __name__=='__main__':unittest.main()
