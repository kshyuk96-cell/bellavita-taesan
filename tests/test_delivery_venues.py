import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import agent
import delivery_venues


DAY = datetime(2026, 11, 14, 12, tzinfo=agent.SEOUL)
SERIAL = (DAY.date() - delivery_venues.EPOCH).days
QUESTION = '오늘 설치 일정 중에 같은 곳 중 가장 많은 주문이 들어온 곳 어디야'
DATES = "'액자화환'!B4:B"
VENUES = "'액자화환'!F4:F"
CENTERS = "'액자화환'!I4:I"


def fixture():
    return {
        DATES: [['배송일'], [SERIAL], [SERIAL], [SERIAL], [SERIAL], [SERIAL + 1], [SERIAL], [SERIAL]],
        VENUES: [['설치장소'], ['가상 예식장'], [' 가상　예식장 '], ['다른 행사장'], [], ['다른 행사장'],
                 ['다른 행사장'], ['#REF!']],
        CENTERS: [['배송 채널'], ['구로센터'], ['구로센터'], ['수원센터'], ['구로센터'], ['수원센터'],
                  ['수원센터'], ['수원센터']],
    }


class DeliveryVenueTests(unittest.TestCase):
    def test_rank_counts_ties_dates_and_missing_values(self):
        data = fixture()
        read = lambda ranges: [data[key] for key in ranges]
        total = delivery_venues.rank(DAY.date(), None, read)
        self.assertIn('가상 예식장, 다른 행사장', total)
        self.assertIn('각 2건', total)
        self.assertIn('확인해야 하는 주문 2건', total)
        self.assertIn('가상 예식장', delivery_venues.rank(DAY.date(), '구로', read))
        self.assertIn('다른 행사장', delivery_venues.rank(DAY.date(), '수원', read))
        self.assertIn('11월 15일', delivery_venues.rank(DAY.date().replace(day=15), None, read))
        self.assertIn('설치 주문이 없어요', delivery_venues.rank(DAY.date().replace(day=16), None, read))

    def test_routes_exact_question_and_confirmation_to_sheet_instead_of_delivery_count(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as folder, patch.object(agent, 'ROOT', Path(folder)), \
             patch.object(agent, 'datetime', wraps=datetime) as clock, \
             patch.object(agent, 'choose') as choose, patch.object(agent, 'delivery_count') as delivery, \
             patch.object(agent, 'read_delivery_ranges', side_effect=lambda ranges, **kwargs: [data[key] for key in ranges]):
            clock.now.return_value = DAY
            with agent.db() as c:
                c.execute('INSERT INTO staff VALUES (?,?,0)', ('test-staff', 'staff'))

            def dispatch(message):
                body = {'userRequest': {'user': {'id': 'test-staff'}, 'utterance': message}}
                return agent.dispatch(body)['template']['outputs'][0]['simpleText']['text']

            self.assertIn('공동 1위', dispatch(QUESTION))
            self.assertIn('다른 날짜나 센터', dispatch('ㅇㅇ'))
            self.assertIn('공동 1위', agent.handle('ㅇㅇ', [{
                'input': {'message': QUESTION},
                'result': {'summary': '센터 전체 기준으로 장소별 주문이 가장 많은 곳을 찾아 드릴까요?'},
            }], 'test-staff')['summary'])
            choose.assert_not_called()
            delivery.assert_not_called()

    def test_model_can_select_supported_read_without_providing_a_count(self):
        with patch.object(agent, 'choose', return_value={'action': 'venue_rank', 'date_offset': 1,
                                                        'center': '구로', 'message': '88건입니다.'}), \
             patch.object(agent, 'venue_rank', return_value={'summary': '운영 시트 집계 2건'}) as lookup:
            self.assertEqual(agent.handle('내일 구로에 주문이 한 장소에 집중된 데가 어딘지 알려줘', [], 'test-staff'),
                             {'summary': '운영 시트 집계 2건'})
            lookup.assert_called_once_with(1, '구로')

    def test_write_and_unrelated_queries_do_not_trigger_ranking(self):
        for message in ('오늘 배송 미완료 몇 건이야?', '설치 장소별 최다 조회 기능 개발해줘', '오늘 설치장소별 미완료 주문이 가장 많은 곳'):
            with self.subTest(message=message):
                self.assertIsNone(delivery_venues.request(message))
