import unittest
from unittest.mock import patch
import order_intake

class IntakeHardeningTests(unittest.TestCase):
    def test_split_job_titles_preserved(self):
        data=order_intake.extract('주문 접수\n설치 장소: 모의 예식장\n보내는사람\n모의협회 회장 홍길동\n모의회사 대표이사\n규격: 2호')
        self.assertEqual(data['sender'],'모의협회 회장 홍길동\n모의회사 대표이사')
    def test_multiple_invitations_ask_instead_of_merging(self):
        with self.assertRaises(ValueError):order_intake.extract('주문 접수\nhttps://salondeletter.com/w/one\nhttps://salondeletter.com/w/two\n보내는분: 모의회사')
    def test_compact_fullwidth_and_heading_labels(self):
        data=order_intake.extract('주문등록\n설치장소\n부산광역시 해운대구 모의홀\n받는분：모의 수신자\n보내시는분：모의 회사\n축하문구\n첫째 줄\n둘째 줄')
        self.assertEqual(data['sender'],'모의 회사');self.assertEqual(data['message'],'첫째 줄\n둘째 줄')
    def test_conflicting_fields_do_not_silently_use_last(self):
        with self.assertRaises(ValueError):order_intake.extract('주문 접수\n주소: 서울특별시 강남구\n주소: 부산광역시 해운대구\n보내는 분: 모의 회사')
    def test_printed_link_is_not_fetched_as_invitation(self):
        with patch.object(order_intake,'wedding',side_effect=AssertionError('unexpected network')):
            data=order_intake.extract('주문 접수\n설치장소: 모의홀\n보내는 분: 모의 회사\n축하문구: https://unrecognized.example/print-this')
        self.assertEqual(data['message'],'https://unrecognized.example/print-this')
    def test_event_date_not_shipping_date(self):
        event={'event_type':'결혼','venue':'모의홀','address':'부산광역시 해운대구','recipient':'모의 신랑 신부','event_date':'2026-11-21','event_time':'13:00'}
        with patch.object(order_intake,'wedding',return_value=event):
            data=order_intake.extract('주문 접수\nhttps://salondeletter.com/w/example\n보내는 분: 모의 회사')
        self.assertEqual(data['delivery_date'],'');self.assertEqual(data['delivery_time'],'')
        self.assertIn('지방 배송 확인 필요',order_intake.report(1001,data))
    def test_greeting_does_not_hide_unlabelled_sender(self):
        event={'event_type':'결혼','venue':'모의홀','address':'','recipient':'모의 신랑 신부'}
        with patch.object(order_intake,'wedding',return_value=event):
            result=order_intake.extract('주문 접수\nhttps://salondeletter.com/w/example\n안녕하세요 ~\n\n신부측\n\n모의 회사 대표 홍길동')
        self.assertEqual(result['sender'],'모의 회사 대표 홍길동')

    def test_inline_known_labels(self):
        data=order_intake.extract('주문 접수 / 설치장소: 모의홀, 받는분은 모의 수신자, 보내는분은 모의회사, 규격: 2호')
        self.assertEqual(data['sender'],'모의회사');self.assertEqual(data['venue'],'모의홀')

if __name__=='__main__':unittest.main()
