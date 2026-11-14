import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent


class StockMenuTests(unittest.TestCase):
    def test_shortcut_reads_fresh_stock_without_consuming_an_invitation_draft(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(agent, 'ROOT', Path(temporary)), \
             patch.object(agent, 'read_values', side_effect=[
                 [['작품번호', '', '구로'], [36, '', 3]],
                 [['작품번호', '', '구로'], [36, '', 2]],
             ]) as read, patch.object(agent, 'run_invitation') as invitation:
            def dispatch(message):
                return agent.dispatch({'userRequest': {'user': {'id': 'qa-stock'}, 'utterance': message}})

            self.assertNotIn('구로 재고 조회', str(dispatch('재고 조회')))
            with agent.db() as connection:
                connection.execute('INSERT INTO staff VALUES (?,?,?)', ('qa-stock', 'staff', time.time()))
            dispatch('초대장')
            menu = dispatch('재고 조회')['template']
            choice = next(button for button in menu['quickReplies'] if button['label'] == '구로')
            read.assert_not_called()
            self.assertIn('별사진 3개', str(dispatch(choice['messageText'])))
            self.assertIn('별사진 2개', str(dispatch(choice['messageText'])))
            invitation.assert_not_called()
            self.assertEqual(read.call_count, 2)
            with agent.db() as connection:
                self.assertIsNotNone(connection.execute(
                    'SELECT 1 FROM invitation_input WHERE owner=?', ('qa-stock',)
                ).fetchone())
