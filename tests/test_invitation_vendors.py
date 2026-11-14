import json,unittest
from unittest.mock import Mock,patch
from lxml import html
import invitation,invitation_vendors,order_intake
from invitation_data import LiteralReader,nuxt_bindings,named_values

class LiteralDataTests(unittest.TestCase):
    def test_no_javascript_execution(self):
        for value in ['process.exit(0)','require("fs")','(()=>1)()','new Function("x")()']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    reader=LiteralReader(value);parsed=reader.value()
                    from invitation_data import resolve
                    resolve(parsed,{})
    def test_guestbook_text_cannot_supply_event_keys(self):
        text="{manName:a,comment:\"manName:'악의적이름'\",other:'manName:fake'}"
        self.assertEqual(named_values(text,'manName',{'a':'가상남'}),['가상남'])
        self.assertEqual(named_values('{\"groomName\":\"가상남\",\"comment\":\"groomName: fake\"}','groomName',quoted=True),['가상남'])

    def test_limits_and_decimal_literals(self):
        self.assertEqual(LiteralReader('.5').value(),.5)
        with self.assertRaises(ValueError):LiteralReader('Array(99999999)').value()
        with self.assertRaises(ValueError):LiteralReader('['*120+'0'+']'*120).value()
    def test_nuxt_reads_literals_without_running_prelude(self):
        script='window.__NUXT__=(function(a,b){evil();return {manName:a,womanName:b,date:{year:2026,month:11,date:21,hour:16,minute:30}}}("가상남","가상녀"));'
        binding=nuxt_bindings(script)
        self.assertEqual(named_values(script,'manName',binding),['가상남'])

class VendorTests(unittest.TestCase):
    def test_barunson_uses_local_display_clock_not_template_iso_clock(self):
        fields={'groomName':'가상남','brideName':'가상녀','groomFatherName':'가상부','groomMotherName':'가상모','brideFatherName':'모의부','brideMotherName':'모의모','groomFamilyRole':'장남','brideFamilyRole':'장녀','venueName':'가상홀','venueHall':'2층','venueAddress':'경기도 광명시 검증로 10','weddingDate':'2026-11-21T12:00:00.000Z','weddingTime':'오후 1시 40분'}
        flight='1:'+json.dumps(fields,ensure_ascii=False)+'\n'
        script='self.__next_f.push('+json.dumps([1,flight],ensure_ascii=False)+');'
        page='<html><meta property="og:description" content="2026년 11월 21일 토요일 오후 1시 40분"><script>'+script+'</script></html>'
        data=invitation.parse_page('https://letterb.barunsoncard.com/mock-card',page)
        self.assertEqual(data['when'].strftime('%H:%M'),'13:40')
        self.assertEqual(data['address'],fields['venueAddress']);self.assertEqual(data['families'][0][0],'가상부·가상모')
    def test_makedear_resolves_aliases_without_evaluating_code(self):
        script='window.__NUXT__=(function(a,b){return {manName:a,womanName:b,place:"가상홀",address:"충북 청주시 검증로 1",date:{year:2026,month:11,date:21,hour:16,minute:30}}}("가상남","가상녀"));'
        data=invitation.parse_page('https://makedear.com/mcard/mock','<html><script>'+script+'</script></html>')
        self.assertEqual(data['when'].strftime('%H:%M'),'16:30');self.assertEqual(data['families'][1][2],'가상녀')
    def test_iris_funeral_does_not_infer_shipping_from_departure(self):
        page='<html><meta property="og:description" content="故가상인님께서 별세하셨기에"><p class="obituary-funeral-info">모의장례식장 301호</p><font class="company-address">부산 사상구 검증로 1</font><section class="obituary-funeral-date">발인 11월 21일 10:30</section></html>'
        data=order_intake.iris_bugo('https://e-iris.co.kr/mock',page)
        self.assertEqual(data['event_type'],'근조');self.assertIn('발인',data['note']);self.assertNotIn('delivery_time',data)
    def test_itscard_noon_and_source_binding(self):
        page=html.fromstring('<html><script>const data = [null,{"data":{"strUID":"mock123"}}];</script></html>')
        read=Mock(return_value=[{'WDate':'2026-11-21','WDateTime':'낮:12시 30분','WPlace':'가상홀','WAddr1':'경기도 수원시 검증로 1','GNameK':'가상남','BNameK':'가상녀'}])
        data=invitation_vendors.itscard('https://mcard.itscard.co.kr/mock123',page,invitation.parse_time,read)
        self.assertEqual(data['when'].strftime('%H:%M'),'12:30');read.assert_called_once_with('mock123')
        read.reset_mock()
        with self.assertRaises(ValueError):invitation_vendors.itscard('https://mcard.itscard.co.kr/other',page,invitation.parse_time,read)
        read.assert_not_called()
    def test_itscard_view_request_cannot_target_arbitrary_endpoint(self):
        with patch.object(invitation,'build_opener') as opener:
            for value in ['../write','https://other.test','a/b','']:
                with self.assertRaises(ValueError):invitation.read_itscard_view(value)
            opener.assert_not_called()

class InputRegressionTests(unittest.TestCase):
    def test_copy_paste_spaces_bullets_and_joined_particles(self):
        data=order_intake.extract('주문접수\n📍 설치\u00a0장소：부산광역시 해운대구 모의홀\n• 보내는\u00a0분은가상 회사\n• 받는분은모의 수신자')
        self.assertEqual(data['sender'],'가상 회사');self.assertEqual(data['recipient'],'모의 수신자')
    def test_company_line_and_role_line_are_one_sender(self):
        data=order_intake.extract('주문접수\n설치장소: 모의홀\n보내는사람\n친환경 모의회사\nCMO 홍길동')
        self.assertEqual(data['sender'],'친환경 모의회사\nCMO 홍길동')
    def test_unlabelled_final_signature_separate_from_host_greeting(self):
        event={'event_type':'결혼','venue':'가상홀','address':'','recipient':'가상남 가상녀'}
        with patch.object(order_intake,'wedding',return_value=event):
            data=order_intake.extract('주문접수\nhttps://salondeletter.com/w/mock\n저희 아이의 결혼식에 초대합니다.\n\n가상 혼주 올림\n\n모의 동문회 회장 홍길동')
        self.assertEqual(data['sender'],'모의 동문회 회장 홍길동')
    def test_placeholder_sender_is_held(self):
        for sender in ['?','미정','확인 필요','없음']:
            with self.assertRaises(ValueError):order_intake.extract('주문접수\n설치장소: 가상홀\n보내는분: '+sender)
    def test_regional_alert_precedes_long_inscription(self):
        report=order_intake.report(1001,{'event_type':'결혼','venue':'부산광역시 해운대구','sender':'가상 회사 '*100,'recipient':'가상 수신자'})
        self.assertIn('지방 배송 확인 필요',report[:200])

if __name__=='__main__':unittest.main()
