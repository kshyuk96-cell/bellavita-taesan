import base64
import io
import json
import os
import unittest
from unittest.mock import patch

import order_sms


class SendonTransport(unittest.TestCase):
    settings={'INVITATION_SMS_PROVIDER':'sendon','SENDON_ID':'test-business-id','SENDON_API_KEY':'test-api-key','SENDON_FROM':'031-000-0000'}
    def transport(self,result,calls):
        class Provider:
            def open(self,request,timeout):
                calls.append(request)
                return io.BytesIO(json.dumps(result).encode())
        return Provider()
    def test_official_v2_basic_auth_lms_payload_and_group_acceptance(self):
        calls=[]
        with patch.dict(os.environ,self.settings),patch('order_sms.build_opener',return_value=self.transport({'code':200,'message':'성공','data':{'groupId':'sendon-group'}},calls)):
            self.assertTrue(order_sms.configured())
            receipt=order_sms.send('01000000001','벨라비타 결제 완료 주문 안내입니다. '*10,'outbox-key')
        self.assertEqual(receipt,'sendon-group')
        request=calls[0];body=json.loads(request.data)
        self.assertEqual(request.full_url,'https://api.sendon.io/v2/messages/sms')
        auth=request.get_header('Authorization');self.assertEqual(base64.b64decode(auth.split(' ',1)[1]).decode(),'test-business-id:test-api-key')
        self.assertEqual(body['type'],'LMS');self.assertEqual(body['title'],'벨라비타 주문 알림')
        self.assertEqual(body['from'],'0310000000');self.assertEqual(body['to'],['01000000001'])
        self.assertIs(body['isAd'],False);self.assertIs(body['useCredit'],False)
        self.assertNotIn('reference',body)
    def test_http_200_with_sendon_error_code_is_not_provider_acceptance(self):
        calls=[]
        with patch.dict(os.environ,self.settings),patch('order_sms.build_opener',return_value=self.transport({'code':401,'message':'Whitelist IP 등록 후 이용해주세요.'},calls)):
            with self.assertRaisesRegex(ValueError,'센드온 문자 접수'):order_sms.send('01000000001','테스트','key')
    def test_success_without_group_id_is_not_acceptance(self):
        with patch.dict(os.environ,self.settings),patch('order_sms.build_opener',return_value=self.transport({'code':200,'data':{}},[])):
            with self.assertRaises(ValueError):order_sms.send('01000000001','테스트','key')
    def test_short_transactional_sms_omits_lms_title(self):
        calls=[]
        with patch.dict(os.environ,self.settings),patch('order_sms.build_opener',return_value=self.transport({'code':200,'data':{'groupId':'sendon-group'}},calls)):
            order_sms.send('01000000001','결제 확인','key')
        body=json.loads(calls[0].data);self.assertEqual(body['type'],'SMS');self.assertNotIn('title',body)
    def test_selected_sendon_never_silently_falls_back_to_other_provider(self):
        env={**self.settings,'SENDON_API_KEY':'','SOLAPI_API_KEY':'other','SOLAPI_API_SECRET':'other','SOLAPI_FROM':'0310000000'}
        with patch.dict(os.environ,env):self.assertFalse(order_sms.configured());self.assertEqual(order_sms.provider_name(),'sendon')
    def test_overlong_message_is_rejected_before_network_request(self):
        with patch.dict(os.environ,self.settings),patch('order_sms.build_opener') as transport:
            with self.assertRaises(ValueError):order_sms.send('01000000001','긴 문구'*1000,'key')
            transport.assert_not_called()
    def test_staff_and_customer_use_separate_approved_alimtalk_templates(self):
        calls=[]
        env={**self.settings,'SENDON_ALIM_PROFILE_ID':'bellavita-channel',
             'SENDON_CUSTOMER_ALIM_TEMPLATE_ID':'customer-template',
             'SENDON_STAFF_ALIM_TEMPLATE_ID':'staff-template'}
        accepted={'code':200,'data':{'groupId':'sendon-alim-group'}}
        with patch.dict(os.environ,env),patch('order_sms.build_opener',return_value=self.transport(accepted,calls)):
            variables={'#{주문번호}':'1234'}
            order_sms.send_sendon_alimtalk('01000000001',variables,'고객 대체 문자','customer-key','customer')
            order_sms.send_sendon_alimtalk('01000000002',variables,'직원 대체 문자','staff-key','staff')
        customer,staff=(json.loads(call.data) for call in calls)
        self.assertEqual(customer['templateId'],'customer-template')
        self.assertEqual(staff['templateId'],'staff-template')
        self.assertEqual(customer['sendProfileId'],'bellavita-channel')
        self.assertEqual(staff['sendProfileId'],'bellavita-channel')
        self.assertEqual(customer['fallback']['custom']['type'],'LMS')
        self.assertEqual(staff['fallback']['custom']['type'],'LMS')
        self.assertTrue(all(call.full_url.endswith('/messages/kakao/alim-talk') for call in calls))

if __name__=='__main__':unittest.main()
