import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import agent
import delivery_counts


DAY = datetime(2026, 11, 14, 12, tzinfo=agent.SEOUL)
SERIAL = (DAY.date() - delivery_counts.EPOCH).days
DATES = "'액자화환'!B4:B"
CHANNELS = "'액자화환'!I4:I"
VIEW = "'배송관리'!A3:B"
FLAGS = "'배송관리'!K3:K"


def fixture():
    return {
        DATES: [['배송일'], [SERIAL], [], [SERIAL], [SERIAL + 1], [SERIAL + 2]],
        CHANNELS: [['배송 채널'], ['구로센터'], [], ['수원센터'], ['구로센터'], ['구로센터']],
        VIEW: [['주문번호', '배송일'], [7, SERIAL], [], ['5', SERIAL], [8, SERIAL + 1], [9, SERIAL + 2],
               [10, SERIAL], [8, SERIAL]],
        FLAGS: [['배송 완료'], [False], [], [True], [False], [True], [True], [True]],
    }


class DeliveryCountTests(unittest.TestCase):
    def test_live_dispatch_separates_metrics_and_reads_again(self):
        data = fixture()

        def dispatch(message):
            result = agent.dispatch({'userRequest': {'user': {'id': 'fixture-staff'}, 'utterance': message}})
            return result['template']['outputs'][0]['simpleText']['text']

        with tempfile.TemporaryDirectory() as tmp, patch.object(agent, 'ROOT', Path(tmp)), \
             patch.object(agent, 'datetime', wraps=datetime) as clock, patch.object(agent, 'choose') as choose, \
             patch.object(agent, 'read_delivery_ranges', side_effect=lambda ranges: [data[key] for key in ranges]) as read:
            clock.now.return_value = DAY
            with agent.db() as c:
                c.execute('INSERT INTO staff VALUES (?,?,0)', ('fixture-staff', 'staff'))
            dispatch('초대장')
            scenarios = [
                ('배송완료 체크된건 몇개야?', '11월 14일 전체 배송완료는 1건'),
                ('오늘 배송 몇 건이야?', '11월 14일 전체 배송 미완료는 1건'),
                ('오늘 전체 배송은 몇 건이야?', '11월 14일 전체 배송은 2건'),
                ('오늘 배송완료 처리되지 않은 건은 몇 개?', '11월 14일 전체 배송 미완료는 1건'),
                ('내일 배송 안 된 건 몇 개야?', '11월 15일 전체 배송 미완료는 1건'),
                ('모레 배송완료 건수 알려줘', '11월 16일 전체 배송완료는 1건'),
                ('구로센터 오늘 배송 미완료 몇 건?', '11월 14일 구로 배송 미완료는 0건'),
            ]
            for question, expected in scenarios:
                with self.subTest(question=question):
                    self.assertIn(expected, dispatch(question))
            self.assertIn(([DATES],), [call.args for call in read.call_args_list])
            data[FLAGS][1] = [True]
            self.assertIn('배송완료는 2건', dispatch('배송완료 체크된건 몇개야?'))
            read.side_effect = TimeoutError
            self.assertIn('읽지 못했어요', dispatch('배송완료 체크된건 몇개야?'))
            choose.assert_not_called()
            with agent.db() as c:
                self.assertIsNotNone(c.execute('SELECT * FROM invitation_input').fetchone())

    def test_unmatched_duplicate_or_invalid_flags_do_not_count_as_pending(self):
        data = fixture()
        data[VIEW][1] = [5, SERIAL]
        text = delivery_counts.count(DAY.date(), None, 'pending', lambda ranges: [data[key] for key in ranges])
        self.assertIn('확인된 건은 0건', text)
        self.assertIn('2건은 배송관리에서 상태 확인', text)
        data[VIEW][1] = [7, SERIAL]
        data[FLAGS][1] = []
        text = delivery_counts.count(DAY.date(), None, 'pending', lambda ranges: [data[key] for key in ranges])
        self.assertIn('1건은 배송관리에서 상태 확인', text)

    def test_smart_routing_preserves_non_count_intents(self):
        for question in ('배송완료 몇 건인지 알려주는 기능 개발해', '오늘 제작 배송 몇 건', '다음 주 배송 몇 건?',
                         '오늘 구로 배송 몇 건?', '배송완료 처리해줘 몇 건'):
            self.assertIsNone(delivery_counts.request(question), question)
