import io
import json
import os
import unittest
from unittest.mock import patch
import erp_ai
import erp_invitations as bridge

class Handler:
    def __init__(self,body=b'',token='test-only'):
        self.headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Content-Length':str(len(body))};self.rfile=io.BytesIO(body);self.status=None;self.result=None
    def send_json(self,value,status=200):self.status=status;self.result=value

class ERPGatewayContract(unittest.TestCase):
    def test_unapproved_calls_cannot_reach_ai_or_templates(self):
        with patch.dict(os.environ,{'ERP_INVITATION_TOKEN':'test-only'}):
            for path in ('/erp/invitations/templates','/erp/invitations/jobs','/erp/ai/responses'):
                h=Handler(token='wrong');self.assertTrue(bridge.dispatch(h,'POST',path,'unused','https://example.org',None,None));self.assertEqual(h.status,401)
    def test_template_path_and_content_are_pinned(self):
        with patch.dict(os.environ,{'ERP_INVITATION_TOKEN':'test-only'}):
            h=Handler(json.dumps({'name':'../outside','data':'eA=='}).encode());bridge.dispatch(h,'POST','/erp/invitations/templates','unused','https://example.org',None,None);self.assertEqual(h.status,422)
    def test_explicit_event_host_survives_a_model_that_omits_the_signature(self):
        import datetime as dt
        from types import SimpleNamespace
        worker=object.__new__(bridge.Engine)
        worker.legacy=SimpleNamespace(parse_source=lambda source,extract:{'name':'가상 행사','when':dt.datetime(2099,12,10,14),'venue':'검증홀','address':'테스트로 1','signature':'','source':'','photos':[]})
        worker.extract=lambda source:{}
        result=worker.parse({'kind':'event','source':'가상 행사. 주최: 검증재단. 일시: 2099년 12월 10일 오후 2시.'})
        self.assertEqual(result['values']['host'],'검증재단');self.assertNotIn('host',result['missing'])

    def test_cached_catalog_still_checks_each_live_product_price(self):
        import threading
        from types import SimpleNamespace
        scans=[];changed=[False]
        rows=[{'goodNum':100+i,'goodName':'검증 행사','goodPrice':price,'isShow':1,'isSoldout':0,'isRecommend':0} for i,price in enumerate(bridge.PRICES)]
        class Client:
            def __init__(self,*args):pass
            def products(self,all_pages=False):scans.append(True);return list(rows)
            def existing_products(self,title,products=None):return {r['goodPrice']:r for r in products}
            def detail(self,number):
                row=dict(next(r for r in rows if r['goodNum']==number))
                if changed[0]:row['goodPrice']=1
                return row
            def normalized_title(self,title):return title
            def order_link(self,row):return 'https://example.shop.blogpay.co.kr/order/'+str(row['goodNum'])
        worker=object.__new__(bridge.Engine);worker.legacy=SimpleNamespace(PRODUCT_LOCK=threading.Lock(),checkout=SimpleNamespace(product_snapshot=lambda client,row:{'price':row['goodPrice'],'number':str(row['goodNum'])}))
        with patch.dict(os.environ,{'BLOGPAY_SHOP_ID':'example','BLOGPAY_API_TOKEN':'test-only'}),patch('blogpay.BlogPay',Client):
            self.assertEqual(len(worker.products('검증 행사')),2);self.assertEqual(len(worker.products('검증 행사')),2);self.assertEqual(len(scans),1)
            changed[0]=True
            with self.assertRaises(ValueError):worker.products('검증 행사')

    def test_no_copy_when_the_source_fingerprint_is_missing_or_changes(self):
        import threading
        from types import SimpleNamespace
        copies=[]
        class Client:
            def __init__(self,*args):pass
            def products(self,all_pages=False):return []
            def existing_products(self,*args):return {}
            def ensure_links(self,*args):copies.append(True)
        worker=object.__new__(bridge.Engine);worker.legacy=SimpleNamespace(PRODUCT_LOCK=threading.Lock())
        worker.template_check=lambda number,client:{'eligible':True,'fingerprint':'changed'}
        with patch.dict(os.environ,{'BLOGPAY_SHOP_ID':'example','BLOGPAY_API_TOKEN':'test-only','BLOGPAY_INVITATION_AUTO_CREATE':'1','BLOGPAY_INVITATION_TEMPLATE_ID':'100','BLOGPAY_INVITATION_TEMPLATE_FINGERPRINT':''}),patch('blogpay.BlogPay',Client):
            with self.assertRaises(ValueError):worker.products('검증 행사')
            with patch.dict(os.environ,{'BLOGPAY_INVITATION_TEMPLATE_FINGERPRINT':'pinned'}):
                with self.assertRaises(ValueError):worker.products('검증 행사')
            self.assertEqual(copies,[])

    def test_gateway_uses_existing_key_without_exposing_it_or_allowing_tools(self):
        data={'model':'gpt-test','input':'connection check','max_output_tokens':64,'store':True}
        with patch.dict(os.environ,{'ERP_INVITATION_TOKEN':'test-only','OPENAI_API_KEY':'fake-provider-key','TAESAN_MODEL':'gpt-test'}):
            response=io.BytesIO(json.dumps({'status':'completed','output':[]}).encode())
            with patch.object(erp_ai,'urlopen',return_value=response) as call:
                h=Handler(json.dumps(data).encode());bridge.dispatch(h,'POST','/erp/ai/responses','unused','https://example.org',None,None);self.assertEqual(h.status,200);request=call.call_args.args[0];self.assertFalse(json.loads(request.data)['store']);self.assertEqual(request.get_header('Authorization'),'Bearer fake-provider-key');self.assertNotIn('fake-provider-key',json.dumps(h.result))
            bad=Handler(json.dumps({**data,'tools':[{'type':'web_search'}]}).encode());bridge.dispatch(bad,'POST','/erp/ai/responses','unused','https://example.org',None,None);self.assertEqual(bad.status,400)

if __name__=='__main__':unittest.main()
