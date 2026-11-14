import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import agent
import order_duplicates


def ledger():
    return (
        [['배송일', '행사시간'], [46369, .5], [], [46369, '오후 12:00'], [46369, .625],
         [46369, .5], [46370, .5], [46369, .5], [], [46369, .5]],
        [['설치장소'], ['가상  예식장'], [], ['가상 예식장'], ['가상 예식장'], ['다른 예식장'],
         ['가상 예식장'], ['가상 예식장'], ['가상 예식장'], ['가상 예식장']],
        [['보내는분 요약'], ['가상 업체'], [], ['가상 업체 '], ['가상 업체'], ['가상 업체'],
         ['가상 업체'], ['다른 업체'], ['가상 업체'], ['#REF!']],
    )


class OrderDuplicateTests(unittest.TestCase):
    def test_exact_match_preserves_row_positions_and_hides_fields(self):
        read = Mock(return_value=ledger())
        result = order_duplicates.check(5, read)
        self.assertIn('중복 의심', result)
        self.assertIn('1건', result)
        self.assertIn('원장 7번', result)
        self.assertNotIn('가상 업체', result)
        self.assertNotIn('가상 예식장', result)
        read.assert_called_once_with(("'액자화환'!B4:C", "'액자화환'!F4:F", "'액자화환'!Q4:Q"))

    def test_no_match_missing_order_incomplete_rows_and_changed_header_differ(self):
        data = ledger()
        read = Mock(return_value=data)
        self.assertIn('모두 같은 주문은 없어요', order_duplicates.check(8, read))
        self.assertIn('찾지 못했어요', order_duplicates.check(6, read))
        self.assertIn('읽을 수 없어요', order_duplicates.check(12, read))
        self.assertIn('읽을 수 없어요', order_duplicates.check(13, read))
        data[2][0] = ['원문 보내는 분']
        self.assertIn('제목이 달라', order_duplicates.check(5, read))

    def test_dispatch_reads_fresh_without_consuming_invitation_or_writing_order(self):
        staff = 'qa-ledger-duplicate'

        def dispatch(message, owner=staff):
            return agent.dispatch({'userRequest': {'user': {'id': owner}, 'utterance': message}})

        with tempfile.TemporaryDirectory() as temporary, patch.object(agent, 'ROOT', Path(temporary)):
            with agent.db() as connection:
                connection.execute('INSERT INTO staff VALUES (?,?,?)', (staff, 'staff', time.time()))
            dispatch('초대장')
            data = ledger()
            with patch.object(agent, 'read_delivery_ranges', return_value=data) as read, \
                 patch.object(agent, 'write_order') as write, patch.object(agent, 'choose') as choose:
                self.assertIn('직원 신청', str(dispatch('주문 5번 중복 확인', 'qa-other')))
                read.assert_not_called()
                self.assertIn('중복 의심', str(dispatch('주문 5번 중복인지 확인해줘')))
                data[2][3] = ['바뀐 업체']
                self.assertIn('모두 같은 주문은 없어요', str(dispatch('주문 5번 중복인지 확인해줘')))
                read.side_effect = TimeoutError
                self.assertIn('원장을 읽지 못해', str(dispatch('중복 확인 5번')))
                self.assertEqual(read.call_count, 3)
                write.assert_not_called()
                choose.assert_not_called()
            with agent.db() as connection:
                self.assertIsNotNone(connection.execute('SELECT owner FROM invitation_input WHERE owner=?', (staff,)).fetchone())
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM order_operations').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM alert_outbox').fetchone()[0], 0)
