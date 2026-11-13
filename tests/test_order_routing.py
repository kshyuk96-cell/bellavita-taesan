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
                dispatch('초대장')
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
