import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import erp_local_checkout as local
import checkout
class LocalCheckout(unittest.TestCase):
 def test_two_prices_without_any_blogpay_access_and_idempotent_versions(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);folder=root/'invitations'/local.TEMPLATE;folder.mkdir(parents=True)
   original={'groups':[{'label':name,'choices':{'1':{'label':'확인 항목','amount':0,'custom':False}}} for name in ['축하 문구','받으시는 분','상품 선택']],'images':['https://example.org/product.jpg']}
   for price in local.PRICES:(folder/f'{price}.order.json').write_text(json.dumps(original))
   data={'invitationId':'iv-test','version':1,'kind':'wedding','values':{'name':'가상 예식','date':'2099-01-01','time':'12:00','venue':'검증홀','address':'가상로 1'}}
   with patch('blogpay.BlogPay',side_effect=AssertionError('BlogPay must not be called')):
    key,items,products=local.build(root,'https://example.org',data)
   self.assertEqual([i['price'] for i in items],[93500,99000]);self.assertEqual(len({i['productId'] for i in items}),2);self.assertTrue(all('/i/'+key+'/' in i['orderUrl'] for i in items));self.assertEqual(local.build(root,'https://example.org',data)[0],key)
   publication='a'*32;fingerprint='b'*64;local.publish(root,key,products,publication,fingerprint,'https://example.org',True)
   self.assertIsNone(checkout.read(root,key,'93500'))
   manifest=root/'erp-invitations'/'public'/publication;manifest.mkdir(parents=True);(manifest/'result.json').write_text(json.dumps({'fingerprint':fingerprint}))
   self.assertEqual(checkout.read(root,key,'93500')['price'],93500);self.assertEqual(checkout.read(root,key,'99000')['price'],99000)
 def test_missing_template_fails_instead_of_guessing_option_prices(self):
  with tempfile.TemporaryDirectory() as root:
   with self.assertRaises(ValueError):local.build(root,'https://example.org',{'invitationId':'iv1','version':1,'kind':'event','values':{}})
 def test_engine_preview_publish_creates_two_payapp_forms_without_catalog_requests(self):
  from types import SimpleNamespace
  from PIL import Image
  import erp_invitations as bridge
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);template=root/'invitations'/local.TEMPLATE;template.mkdir(parents=True)
   product={'images':['https://example.org/product.webp'],'groups':[{'label':label,'choices':{'1':{'label':'확인된 선택','amount':0,'custom':False}}} for label in ['축하 문구','받으시는 분','상품 선택']]}
   for price in local.PRICES:(template/f'{price}.order.json').write_text(json.dumps(product))
   legacy=SimpleNamespace();v={'name':'가상 검증 행사','host':'검증 주최','date':'2099-01-01','time':'12:00','venue':'검증홀','address':'가상로 1'}
   with patch.dict('os.environ',{'TAESAN_DATA':str(root),'INVITATION_LOCAL_CHECKOUT_ENABLED':'1'}),patch('erp_invitations.render.composite',return_value=(Image.new('RGB',(2,2)),{})),patch('erp_invitations.render.thumbnail',return_value=b'jpeg'),patch('erp_invitations.render.page',return_value='<html>test</html>'),patch('erp_invitation_auto.components',return_value={}),patch('blogpay.BlogPay',side_effect=AssertionError('catalog forbidden')):
    engine=bridge.Engine(root,'https://example.org',legacy,None,async_jobs=False)
    req={'requestId':'own-preview-1234','invitationId':'own-invite-1234','version':1,'action':'preview','kind':'event','source':'검증 행사 안내','values':v,'orderMode':'invitation-checkout'}
    preview=engine.submit(req);self.assertEqual(preview['status'],'done')
    result=engine.submit({**req,'requestId':'own-publish-1234','action':'publish','previewRequestId':req['requestId'],'fingerprint':preview['fingerprint']});self.assertEqual(result['status'],'done')
    self.assertEqual(len(result['items']),2);key=result['items'][0]['orderUrl'].split('/i/')[1].split('/')[0]
    self.assertEqual(checkout.read(root,key,'93500')['price'],93500);self.assertEqual(checkout.read(root,key,'99000')['price'],99000);self.assertTrue(checkout.payment_enabled(key));self.assertFalse(checkout.payment_enabled('0'*32))
    engine.executor.shutdown()
