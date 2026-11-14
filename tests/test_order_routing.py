import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent


class OrderRoutingTests(unittest.TestCase):
    def test_partial_release_keeps_order_writes_and_sms_off_even_with_credentials(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(agent, 'ROOT', Path(temporary)):
            with patch.dict(agent.os.environ, {'TAESAN_ORDER_SHEET_ID': 'fixture', 'SOLAPI_API_KEY': 'fixture',
                                               'SOLAPI_API_SECRET': 'fixture', 'SOLAPI_FROM': '01000000000'}), \
                 patch.object(agent, 'sheets_service') as sheets, patch.object(agent.threading, 'Thread') as worker:
                reply = agent.run_order('주문 접수\n보내는 분: 모의 직원', 'qa-order')
                self.assertIn('원장에는 아직 등록되지 않았어요', reply['summary'])
                with agent.db() as connection:
                    self.assertEqual(agent.order_operations.latest_intake(connection, 'qa-order')['state'], 'pending')
                with self.assertRaisesRegex(RuntimeError, '원장 자동 등록'):
                    agent.write_order('fixture', {}, '2026-11-14T09:00:00+09:00')
                agent.start_order_queue()
                sheets.assert_not_called()
                worker.assert_not_called()

    def test_explicit_order_keeps_invitation_draft_open(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(agent, 'ROOT', Path(temporary)):
            owner = 'qa-order-routing'

            def dispatch(message):
                return agent.dispatch({'userRequest': {'user': {'id': owner}, 'utterance': message}})

            with agent.db() as connection:
                connection.execute('INSERT INTO staff VALUES (?,?,?)', (owner, 'staff', time.time()))
            with patch.object(agent, 'run_order', return_value={'summary': '모의 주문'}) as order, \
                 patch.object(agent, 'run_invitation', return_value={'summary': '모의 초대장'}) as invitation:
                self.assertIn('주문 접수', [item['label'] for item in dispatch('메뉴')['template']['quickReplies']])
                dispatch('초대장')
                for prompt in ('주문 접수', '주문 등록:', '주문 입력'):
                    with self.subTest(prompt=prompt):
                        self.assertIn('설치장소:', str(dispatch(prompt)))
                order.assert_not_called()
                invitation.assert_not_called()
                with agent.db() as connection:
                    self.assertEqual(connection.execute('SELECT COUNT(*) FROM order_operations').fetchone()[0], 0)
                    self.assertEqual(connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
                message = '주문 접수\n설치장소: 가상 예식장\n보내는 분: 테스트 직원'
                self.assertIn('모의 주문', str(dispatch(message)))
                order.assert_called_once_with(message, owner)
                invitation.assert_not_called()

                with agent.db() as connection:
                    draft = connection.execute('SELECT owner FROM invitation_input WHERE owner=?', (owner,)).fetchone()
                self.assertIsNotNone(draft)

                link = 'https://salondeletter.com/w/qa-example'
                self.assertIn('모의 초대장', str(dispatch(link)))
                invitation.assert_called_once_with(link, owner)
