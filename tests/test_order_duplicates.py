import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import agent
import order_duplicates


ROAD = '서울특별시 강남구 테헤란로 10'
A = ROAD + ' 가상센터 3층 그랜드홀'
B = '그랜드홀 3층 가상센터 서울 강남구 역삼동 123'


def evidence(query):
    return dict(status='unique', verified=True, provider='fixture', query=query,
                aliases=[ROAD, '서울 강남구 역삼동 123'], canonical_road_address=ROAD,
                canonical_building_name='가상센터')


def ledger():
    return [
        [['배송일', '행사시간'], [46369, .5], [], [46369, '오후 12:00'],
         [46369, '12:00:01'], [46369, .5], [46370, .5], [46369, .5], [46369, .5]],
        [['설치장소'], [A], [], [B], [B], [ROAD + ' 4층 그랜드홀'], [B], [B], [ROAD]],
        [['보내는분 요약'], ['가상 업체'], [], ['가상 업체 '], ['가상 업체'],
         ['가상 업체'], ['가상 업체'], ['다른 업체'], ['가상 업체']],
    ]


class OrderDuplicateTests(unittest.TestCase):
    def test_road_jibun_same_real_hall_is_duplicate_but_missing_details_stays_review(self):
        read = Mock(return_value=ledger())
        result = order_duplicates.check(5, read, resolver=evidence)
        self.assertIn('중복 의심', result)
        self.assertIn('1건', result)
        self.assertIn('원장 7번', result)
        self.assertIn('장소 확인 필요 · 원장 12번', result)
        self.assertNotIn('원장 8번', result)  # seconds differ, no minute rounding
        self.assertNotIn('가상 업체', result)
        self.assertNotIn(ROAD, result)
        read.assert_called_once_with(order_duplicates.RANGES)

    def test_missing_provider_cannot_report_no_duplicates(self):
        result = order_duplicates.check(5, Mock(return_value=ledger()))
        self.assertIn('장소 확인 필요', result)
        self.assertNotIn('중복 의심', result)
        self.assertNotIn('모두 같은 주문은 확인되지', result)

    def test_empty_reused_row_is_missing_and_headers_or_missing_fields_are_not_no_matches(self):
        data = ledger()
        self.assertIn('찾지 못했어요', order_duplicates.check(6, Mock(return_value=data)))
        data[0][1][1] = ''
        self.assertIn('읽을 수 없어요', order_duplicates.check(5, Mock(return_value=data)))
        data[0][0][1] = '배송시각'
        self.assertIn('제목이 달라', order_duplicates.check(5, Mock(return_value=data)))

    def test_different_floor_and_sender_date_time_excluded_before_provider(self):
        data = ledger()
        data[1][8] = [ROAD + ' 4층 그랜드홀']
        provider = Mock(side_effect=evidence)
        result = order_duplicates.check(5, Mock(return_value=data), resolver=provider)
        self.assertIn('1건', result)
        self.assertNotIn('장소 확인 필요', result)
        self.assertEqual(provider.call_count, 6)  # only 3 same-time/sender candidates

    def test_invalid_times_and_exact_seconds(self):
        for clock in (True, float('nan'), '오후 0:00', '오전 13:00', '24:00', '12:60', '12:00:60'):
            with self.subTest(clock=clock):
                self.assertIsNone(order_duplicates.clock_seconds(clock))
        self.assertEqual(order_duplicates.clock_seconds(.5), 43200)
        self.assertEqual(order_duplicates.clock_seconds('오후 12:00:01'), 43201)
        self.assertEqual(order_duplicates.clock_seconds('오전 12:00'), 0)

    def test_candidate_limit_prevents_large_geocoder_batches(self):
        data = [[['배송일', '행사시간']] + [[46369, .5]] * 42,
                [['설치장소']] + [[A]] * 42,
                [['보내는분 요약']] + [['가상 업체']] * 42]
        provider = Mock(side_effect=evidence)
        self.assertIn('많아', order_duplicates.check(5, Mock(return_value=data), resolver=provider))
        provider.assert_not_called()

    def test_revoked_permission_during_lookup_hides_result(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(agent, 'ROOT', Path(temporary)):
            with agent.db() as c:
                c.execute("INSERT INTO staff VALUES ('employee','staff',0)")
            def revoke(*_):
                with agent.db() as c:
                    c.execute("DELETE FROM staff WHERE owner='employee'")
                return ledger()
            with patch.object(agent, 'read_delivery_ranges', side_effect=revoke), \
                 patch.object(agent.order_location_provider, 'make_resolver', return_value=evidence):
                answer = agent.run_job('fixture', '5', 'employee', 'order_duplicate')
            self.assertIn('권한이 변경', answer['summary'])
            self.assertNotIn('원장 7번', answer['summary'])

    def test_dispatch_fresh_read_authenticated_scope_and_invitation_preserved(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(agent, 'ROOT', Path(temporary)):
            with agent.db() as c:
                c.execute('INSERT INTO staff VALUES (?,?,?)', ('employee', 'staff', time.time()))
            def send(message, owner='employee'):
                return str(agent.dispatch({'userRequest': {'user': {'id': owner}, 'utterance': message}}))
            send('초대장')
            data = ledger()
            with patch.object(agent, 'read_delivery_ranges', return_value=data) as read, \
                 patch.object(agent.order_location_provider, 'make_resolver', return_value=evidence), \
                 patch.object(agent, 'write_order') as write, patch.object(agent, 'choose') as model:
                self.assertIn('직원 신청', send('중복 확인 5번', 'outsider'))
                read.assert_not_called()
                self.assertIn('중복 의심', send('주문 5번 중복인지 확인해줘'))
                data[2][3] = ['바뀐 업체']
                self.assertNotIn('중복 의심', send('주문 5번 중복인지 확인해줘'))
                read.side_effect = TimeoutError('private sheet')
                self.assertIn('원장을 읽지 못해', send('중복 확인 5번'))
                self.assertEqual(read.call_count, 3)
                write.assert_not_called()
                model.assert_not_called()
            with agent.db() as c:
                self.assertIsNotNone(c.execute('SELECT owner FROM invitation_input').fetchone())
                self.assertEqual(c.execute('SELECT COUNT(*) FROM order_operations').fetchone()[0], 0)
                self.assertEqual(c.execute('SELECT COUNT(*) FROM alert_outbox').fetchone()[0], 0)
