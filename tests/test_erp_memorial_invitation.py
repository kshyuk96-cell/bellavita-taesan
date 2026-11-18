import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import erp_memorial_invitation as memorial
import erp_invitation_render as renderer
import erp_invitations as bridge
import erp_local_checkout as checkout_products

SOURCE='고인: 가상고인\n장례식장: 검증장례식장\n빈소: 2층 201호\n주소: 서울시 검증로 123\n상주: 가상상주1 · 가상상주2\n대표상주: 가상상주1\n발인: 2099-01-02 오전 09:30\n입관: 2099-01-01 15:00\n장지: 검증추모공원'
class Memorial(unittest.TestCase):
 def test_text_and_html_evidence_and_missing_link_never_invent(self):
  v,w=memorial.extract(SOURCE);self.assertEqual(v['date'],'2099-01-02');self.assertEqual(v['time'],'09:30');self.assertEqual(v['deceasedName'],'가상고인');self.assertEqual(v['funeralRoom'],'2층 201호');self.assertEqual(bridge.missing('memorial',bridge.clean_values(v)),[])
  v,w=memorial.extract('https://example.org/obituary/test',lambda _:(_ for _ in ()).throw(ValueError('서버 응답 503')));self.assertEqual(v['deceasedName'],'');self.assertTrue(any('503' in x for x in w))
  v,w=memorial.extract('<div>고인</div><p>가상고인</p><div>발인</div><p>2099-01-02 09:30</p>');self.assertEqual(v['deceasedName'],'가상고인')
 def test_funeral_page_banner_and_messages_have_no_wedding_or_celebration(self):
  v,_=memorial.extract(SOURCE);image,boxes=renderer.composite(v,'memorial',{});self.assertEqual(image.size,(1200,760));self.assertTrue(boxes)
  page=renderer.page(v,'memorial',93500,'https://example.org/i/'+'a'*32+'/93500/order','https://example.org/sample.webp','','https://example.org/thumb.jpg','https://example.org/page')
  self.assertIn('2층 201호',page);self.assertIn('발인',page);self.assertNotIn('결혼',page);self.assertNotIn('축하',page);self.assertIn('93,500',page)
  message=renderer.message(v,'memorial',93500,'https://example.org/page');self.assertNotIn('축하',message);self.assertIn('삼가',message)
 def test_own_products_use_condolence_choices_and_existing_price_options(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);folder=root/'invitations'/checkout_products.TEMPLATE;folder.mkdir(parents=True)
   baseline={'images':[],'groups':[{'label':label,'choices':{'1':{'label':'기존 선택','amount':0,'custom':False}}} for label in ['축하 문구','받으시는 분','상품 선택']]}
   for price in checkout_products.PRICES:(folder/f'{price}.order.json').write_text(json.dumps(baseline))
   v,_=memorial.extract(SOURCE);_,items,products=checkout_products.build(root,'https://example.org',{'kind':'memorial','invitationId':'memorial-test','version':1,'values':v})
   self.assertEqual(len(items),2);self.assertEqual([p['price'] for p in products.values()],[93500,99000]);self.assertIn('근조 문구',str(products));self.assertNotIn('결혼',str(products));self.assertNotIn('축하',str(products));self.assertEqual(products[93500]['kind'],'memorial')
 def test_end_to_end_parse_banner_publish_and_memorial_checkout(self):
  import checkout
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'TAESAN_DATA':tmp}):
   root=Path(tmp);folder=root/'invitations'/checkout_products.TEMPLATE;folder.mkdir(parents=True)
   baseline={'images':['https://example.org/product.jpg'],'groups':[{'label':label,'choices':{'1':{'label':'기존 선택','amount':0,'custom':False}}} for label in ['축하 문구','받으시는 분','상품 선택']]}
   for price in checkout_products.PRICES:(folder/f'{price}.order.json').write_text(json.dumps(baseline))
   engine=bridge.Engine(root,'https://example.org',SimpleNamespace(checkout=checkout),None,async_jobs=False)
   request={'requestId':'memorial-parse-100','invitationId':'memorial-record-100','version':1,'kind':'memorial','source':SOURCE,'action':'parse','values':{},'orderMode':'invitation-checkout'}
   parsed=engine.submit(request);self.assertEqual(parsed['status'],'done');self.assertEqual(parsed['missing'],[])
   preview=engine.submit({**request,'requestId':'memorial-preview-100','action':'preview','values':parsed['values']});self.assertEqual(preview['status'],'done')
   result=engine.submit({**request,'requestId':'memorial-publish-100','action':'publish','values':parsed['values'],'previewRequestId':'memorial-preview-100','fingerprint':preview['fingerprint']});self.assertEqual(result['status'],'done');self.assertEqual(len(result['items']),2)
   for item in result['items']:
    self.assertNotIn('축하',item['message']);key=item['orderUrl'].split('/i/')[1].split('/')[0];product=checkout.read(root,key,str(item['price']));self.assertEqual(product['kind'],'memorial');page=checkout.render(product,'#',False);self.assertNotIn('웨딩',page);self.assertNotIn('축하',page);self.assertIn('근조 문구',page);self.assertIn('/sample.webp',product['images'][0])
   engine.executor.shutdown()
