import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent
import duplicate_batch_service
import order_duplicates
from test_duplicate_batch_service import Factory, ledger


class DuplicateWorkflowIntegration(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(agent, 'ROOT', Path(self.folder)))
        self.enterContext(patch.dict(os.environ, {'TAESAN_IMMUTABLE_ORDER_ID_HEADER': '불변표식'}))
        with agent.db() as c:
            c.executemany('INSERT INTO staff VALUES (?,?,0)', [('one', 'staff'), ('two', 'staff')])
        self.data = ledger(2)
        self.factory = Factory()
        self.enterContext(patch.object(agent, 'location_resolver', new=lambda **kw: self.factory()))
        self.enterContext(patch.object(agent, 'read_delivery_ranges', side_effect=self.read))

    def read(self, ranges, **kwargs):
        if ranges == order_duplicates.RANGES:
            return copy.deepcopy(self.data[:3])
        if ranges == duplicate_batch_service.RANGES:
            return copy.deepcopy(self.data)
        if len(ranges) == 9 and ranges[0] == "'액자화환'!AC4:AC4":
            result = [[['불변표식']]]
            for start in (1, 5):
                row = int(ranges[start].split('!B')[1].split(':')[0]) - 4
                result.extend([[self.data[group][row]] for group in (0, 1, 2, 3)])
            return result
        raise AssertionError('Unexpected test range')

    def send(self, message, owner='one'):
        return agent.dispatch({'userRequest': {'user': {'id': owner}, 'utterance': message}})

    def test_personal_review_uses_current_immutable_markers_and_does_not_hide_other_staff_warning(self):
        self.assertIn('중복 의심', str(self.send('중복 확인 5번')))
        self.assertIn('24시간', str(self.send('중복 아님 5 6 별도 신청 확인')))
        self.assertIn('경고에서 제외', str(self.send('중복 확인 5번')))
        self.assertIn('중복 의심', str(self.send('중복 확인 5번', 'two')))
        self.data[3][2] = ['new-order-in-same-row']
        self.assertIn('중복 의심', str(self.send('중복 확인 5번')))
        with agent.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM order_operations').fetchone()[0], 0)

    def test_unverified_identity_source_or_blank_marker_cannot_suppress(self):
        with patch.dict(os.environ, {'TAESAN_IMMUTABLE_ORDER_ID_HEADER': ''}):
            self.assertIn('미확인', str(self.send('중복 아님 5 6 별도 신청 확인')))
        self.data[3][2] = []
        self.assertNotIn('24시간', str(self.send('중복 아님 5 6 별도 신청 확인')))
        self.assertIn('중복 의심', str(self.send('중복 확인 5번')))

    def test_expired_review_does_not_suppress_and_failed_review_read_keeps_warning(self):
        self.send('중복 아님 5 6 별도 신청 확인')
        with agent.db() as c:
            c.execute('UPDATE duplicate_review_decisions SET expires=0')
        self.assertIn('중복 의심', str(self.send('중복 확인 5번')))
        with patch.object(agent, 'duplicate_is_reviewed', side_effect=TimeoutError):
            self.assertIn('중복 의심', str(self.send('중복 확인 5번')))

    def test_large_scan_dispatch_runs_background_chunks_with_only_row_results(self):
        self.data = ledger(48)
        self.send('중복 나눠 확인 5번')
        with agent.db() as c:
            batch = c.execute('SELECT id FROM duplicate_batch_jobs WHERE owner=?', ('one',)).fetchone()[0]
        agent.BATCH_ACTIVE[batch].result(timeout=10)
        result = self.send('분할점검 ' + batch)
        self.assertIn('분할 점검 완료', str(result))
        self.assertIn('중복 의심 47건', str(result))
        self.assertNotIn('합성발신자', str(result))
        self.assertIn('이 계정', str(self.send('분할점검 ' + batch, 'two')))
        with agent.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM order_operations').fetchone()[0], 0)
