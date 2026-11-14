import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent


class OrderRoutingTests(unittest.TestCase):
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


class LifecycleRoutingTests(unittest.TestCase):
    def test_staff_cancellation_uses_lifecycle_without_new_job_or_sheet_write(self):
        import order_operations
        with tempfile.TemporaryDirectory() as temporary, patch.object(agent, 'ROOT', Path(temporary)):
            owner='synthetic-staff'
            with agent.db() as c:
                c.execute('INSERT INTO staff VALUES (?,?,?)',(owner,'staff',time.time()))
                order_operations.stage(c,'synthetic-parent',owner,'kakao','synthetic',{'sender':'모의회사'},order_operations.now())
                c.execute("UPDATE order_operations SET state='saved',order_number=21380 WHERE id='synthetic-parent'")
            with patch.object(agent,'write_order',side_effect=AssertionError('must not write')):
                response=agent.dispatch({'userRequest':{'user':{'id':owner},'utterance':'주문 취소 21380'}})
            self.assertIn('취소 요청',str(response));self.assertIn('미확인',str(response))
            status=agent.dispatch({'userRequest':{'user':{'id':owner},'utterance':'주문 접수 상태'}})
            self.assertIn('취소 요청 상태',str(status))
            with agent.db() as c:
                self.assertEqual(c.execute("SELECT state FROM order_operations WHERE id='synthetic-parent'").fetchone()[0],'cancel_requested')
                self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)
