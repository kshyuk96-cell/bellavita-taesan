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
    def test_gateway_uses_existing_key_without_exposing_it_or_allowing_tools(self):
        data={'model':'gpt-test','input':'connection check','max_output_tokens':64,'store':True}
        with patch.dict(os.environ,{'ERP_INVITATION_TOKEN':'test-only','OPENAI_API_KEY':'fake-provider-key','TAESAN_MODEL':'gpt-test'}):
            response=io.BytesIO(json.dumps({'status':'completed','output':[]}).encode())
            with patch.object(erp_ai,'urlopen',return_value=response) as call:
                h=Handler(json.dumps(data).encode());bridge.dispatch(h,'POST','/erp/ai/responses','unused','https://example.org',None,None);self.assertEqual(h.status,200);request=call.call_args.args[0];self.assertFalse(json.loads(request.data)['store']);self.assertEqual(request.get_header('Authorization'),'Bearer fake-provider-key');self.assertNotIn('fake-provider-key',json.dumps(h.result))
            bad=Handler(json.dumps({**data,'tools':[{'type':'web_search'}]}).encode());bridge.dispatch(bad,'POST','/erp/ai/responses','unused','https://example.org',None,None);self.assertEqual(bad.status,400)

if __name__=='__main__':unittest.main()
