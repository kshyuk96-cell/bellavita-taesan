"""Regression reproductions found during batch integration review; no providers."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import agent
import read_context_ui


class BatchReviewRegressions(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(agent, 'ROOT', Path(directory)))
        self.enterContext(patch.dict(os.environ, {
            'TAESAN_MODEL': 'fixture', 'OPENAI_API_KEY': 'fixture-only',
            'TAESAN_WEBHOOK_SECRET': 'fixture-only-secret-32-characters',
            'TAESAN_SHEET_ID': 'fixture-copy-only',
        }, clear=True))
        self.now = datetime(2026, 11, 15, 0, 0, 2, tzinfo=agent.SEOUL)
        clock = self.enterContext(patch.object(agent, 'datetime', wraps=datetime))
        clock.now.side_effect = lambda tz=None: self.now if tz else self.now.replace(tzinfo=None)
        self.enterContext(patch('agent.time.time', side_effect=lambda: self.now.timestamp()))
        self.enterContext(patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network')))
        self.enterContext(patch.object(agent, 'choose', side_effect=AssertionError('unexpected model')))
        with agent.db() as c:
            c.executemany('INSERT INTO staff VALUES (?,?,0)', [('manager', 'admin'), ('employee', 'staff')])

    def seed(self, owner, result, *, state='done', kind='delivery_count', message='가상 조회'):
        job = 'abcd1234abcd'
        with agent.db() as c:
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', (
                job, owner, 'b' * 64, self.now.timestamp() - 1, state,
                json.dumps({'kind': kind, 'message': message}), json.dumps(result)))
        return job

    def read_result(self):
        query = ['delivery_count', 1, '구로', 'pending']
        return dict(summary='가상 배송 조회', read_performed=True, read_query=query,
                    read_day='2026-11-16', read_metadata=read_context_ui.build_metadata(query, now=self.now))

    def send(self, message, owner='employee'):
        return agent.dispatch({'userRequest': {'user': {'id': owner}, 'utterance': message}})

    def test_read_completed_after_midnight_metadata_keeps_actual_read_day(self):
        job = self.seed('employee', {}, state='running')
        real_read = dict(summary='11월 14일 가상 결과', read_performed=True,
                         read_day='2026-11-14', read_query=['delivery_count', 0, '구로', 'pending'])
        with patch.object(agent, 'delivery_count', return_value=real_read):
            result = agent.run_job(job, '조회 조건 변경', 'employee', 'delivery_count',
                                   forced_query=['delivery_count', 0, '구로', 'pending'])
        self.assertEqual(result['read_day'], '2026-11-14')
        self.assertEqual(result['read_query'], ['delivery_count', -1, '구로', 'pending'])
        self.assertEqual(result['read_metadata']['resolved_day'], '2026-11-14')
        self.assertTrue(result['read_metadata']['read_at'].startswith('2026-11-15T00:00:02'))
        # The displayed source and persisted relative query must agree too.
        self.assertIn('11/14', read_context_ui.preview(result['read_query'], result['read_metadata']))

    def test_direct_other_task_stops_implicit_delivery_context_reuse(self):
        self.seed('manager', self.read_result())
        self.assertIsNotNone(agent.current_read_query(agent.recent('manager')))
        self.send('회귀 검증 현황', 'manager')
        self.assertIsNone(agent.current_read_query(agent.recent('manager')))

    def test_direct_cancel_stops_implicit_photo_number_context_reuse(self):
        self.seed('employee', {'summary': '주문번호만 알려주세요.', 'photo_lookup': 'pending'}, kind='photo')
        self.assertEqual(agent.photo_reply_mode(agent.recent('employee')), 'pending')
        self.send('조회 취소')
        self.assertIsNone(agent.photo_reply_mode(agent.recent('employee')))

    def test_new_explicit_read_reestablishes_context_after_direct_command_at_same_clock_tick(self):
        self.seed('manager', self.read_result())
        self.send('회귀 검증 현황', 'manager')
        latest = dict(summary='새 가상 배송 조회', read_performed=True, delivery_count=True,
                      read_day='2026-11-15', read_query=['delivery_count', 0, '수원', 'pending'])
        with patch.object(agent, 'delivery_count', return_value=latest):
            self.send('오늘 수원센터 배송 미완료 몇 건?', 'manager')
        self.assertEqual(agent.current_read_query(agent.recent('manager')),
                         ['delivery_count', 0, '수원', 'pending'])

    def test_explicit_condition_preview_remains_available_without_resetting_read_context(self):
        job = self.seed('employee', self.read_result())
        reply = self.send('조회 조건 ' + job)
        self.assertIn('센터 바꾸기', str(reply))
        self.assertEqual(agent.current_read_query(agent.recent('employee')), ['delivery_count', 1, '구로', 'pending'])

    def test_revoked_account_before_batch_execution_does_not_read_or_start_background(self):
        job = self.seed('employee', {}, state='running', kind='duplicate_batch_start')
        with agent.db() as c:
            c.execute("DELETE FROM staff WHERE owner='employee'")
        with patch.object(agent.duplicate_batch_service, 'start', return_value={
                'job_id': None, 'summary': 'PRIVATE_ROWS', 'status': 'completed'}) as service, \
             patch.object(agent, 'start_batch_worker') as worker:
            result = agent.run_job(job, '5', 'employee', 'duplicate_batch_start')
        service.assert_not_called()
        worker.assert_not_called()
        self.assertIn('권한', result['summary'])
        self.assertNotIn('PRIVATE_ROWS', result['summary'])

    def test_revoked_during_batch_start_hides_result_and_does_not_start_worker(self):
        job = self.seed('employee', {}, state='running', kind='duplicate_batch_start')
        batch_id = 'c' * 32
        def revoke(*args, **kwargs):
            with agent.db() as c:
                c.execute("DELETE FROM staff WHERE owner='employee'")
            return {'job_id': batch_id, 'summary': 'PRIVATE_ROWS', 'status': 'queued'}
        with patch.object(agent.duplicate_batch_service, 'start', side_effect=revoke), \
             patch.object(agent.duplicate_batch_service, 'cancel') as cancel, \
             patch.object(agent, 'start_batch_worker') as worker:
            result = agent.run_job(job, '5', 'employee', 'duplicate_batch_start')
        self.assertIn('권한', result['summary'])
        self.assertNotIn('PRIVATE_ROWS', result['summary'])
        worker.assert_not_called()
        cancel.assert_called_once()

    def test_revoked_during_diagnostics_hides_result(self):
        job = self.seed('manager', {}, state='running', kind='diagnostics')
        def revoke(_):
            with agent.db() as c:
                c.execute("DELETE FROM staff WHERE owner='manager'")
            return 'PRIVATE_DIAGNOSTICS'
        with patch.object(agent, 'diagnose_staff_connections', side_effect=revoke):
            result = agent.run_job(job, '연결 점검', 'manager', 'diagnostics')
        self.assertIn('권한', result['summary'])
        self.assertNotIn('PRIVATE_DIAGNOSTICS', result['summary'])
