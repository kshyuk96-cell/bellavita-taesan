import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path

import job_control


FIRST = 'a' * 12
SECOND = 'b' * 12
FINGERPRINT = 'c' * 64


class JobControlTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.addCleanup(self.c.close)
        self.c.execute('''CREATE TABLE jobs (id TEXT PRIMARY KEY,owner TEXT,fingerprint TEXT,
            created REAL,state TEXT,input TEXT,result TEXT)''')

    def add(self, job=FIRST, owner='employee', kind='delivery_count', state='running',
            created=1, fingerprint=FINGERPRINT):
        self.c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,NULL)',
            (job, owner, fingerprint, created, state, json.dumps({'kind': kind})))

    def test_exact_active_request_merges_only_for_same_employee(self):
        self.add()
        self.assertEqual(job_control.find_active(self.c, 'employee', FINGERPRINT)['id'], FIRST)
        self.assertIsNone(job_control.find_active(self.c, 'other', FINGERPRINT))
        self.assertIsNone(job_control.find_active(self.c, 'employee', 'd' * 64))
        job_control.request_cancel(self.c, 'employee', FIRST)
        self.assertIsNone(job_control.find_active(self.c, 'employee', FINGERPRINT))

    def test_cancel_only_owned_job_is_idempotent_and_preserves_cancellation(self):
        self.add()
        self.assertEqual(job_control.request_cancel(self.c, 'other', FIRST)['status'], 'not_found')
        first = job_control.request_cancel(self.c, 'employee', FIRST)
        self.assertEqual(first['status'], 'cancelled')
        self.assertIn('읽기는 끝날 수', first['summary'])
        self.assertEqual(job_control.request_cancel(self.c, 'employee', FIRST)['status'], 'already_cancelled')
        self.assertFalse(job_control.store_result(self.c, 'employee', FIRST, {'summary': 'late success'}))
        saved = self.c.execute('SELECT state,result FROM jobs').fetchone()
        self.assertEqual(saved[0], 'cancelled')
        self.assertNotIn('late success', saved[1])

    def test_known_side_effect_and_unknown_jobs_cannot_be_cancelled(self):
        for index, kind in enumerate(('invitation', 'order', 'stock', 'stock_write', 'development',
                                      'payment', 'route', '', None)):
            job = f'{index:012x}'
            self.add(job=job, kind=kind)
            with self.subTest(kind=kind):
                self.assertEqual(job_control.request_cancel(self.c, 'employee', job)['status'], 'not_cancelable')
                self.assertEqual(self.c.execute('SELECT state FROM jobs WHERE id=?', (job,)).fetchone()[0], 'running')

    def test_route_requires_exact_persisted_safe_marker_but_invitation_order_never_opt_in(self):
        self.assertNotIn('route', job_control.SAFE_KINDS)
        for index, marker in enumerate((False, 0, 1, 'true', [], {}, None, True)):
            job = f'{index:012x}'
            self.add(job=job, kind='route')
            self.c.execute('UPDATE jobs SET input=? WHERE id=?',
                           (json.dumps({'kind': 'route', 'cancel_safe': marker}), job))
            result = job_control.request_cancel(self.c, 'employee', job)
            with self.subTest(marker=marker):
                self.assertEqual(result['status'], 'cancelled' if marker is True else 'not_cancelable')
                if marker is True:
                    self.assertIn('새 배정안으로 반영하지 않아요', result['summary'])
                    self.assertFalse(job_control.store_result(self.c, 'employee', job, {'summary': 'late route'}))
        for index, kind in enumerate(('invitation', 'order'), 100):
            job = f'{index:012x}'
            self.add(job=job, kind=kind)
            self.c.execute('UPDATE jobs SET input=? WHERE id=?',
                           (json.dumps({'kind': kind, 'cancel_safe': True}), job))
            self.assertEqual(job_control.request_cancel(self.c, 'employee', job)['status'], 'not_cancelable')

    def test_all_safe_kinds_can_discard_results_without_changing_other_work(self):
        self.add(job=SECOND, owner='other')
        for index, kind in enumerate(sorted(job_control.SAFE_KINDS)):
            job = f'{index:012x}'
            self.add(job=job, kind=kind)
            self.assertEqual(job_control.request_cancel(self.c, 'employee', job)['status'], 'cancelled')
        self.assertFalse(job_control.is_cancelled(self.c, 'other', SECOND))

    def test_future_cancellation_requires_persisted_owner_cancellation(self):
        self.add()
        queued = Future()
        self.assertFalse(job_control.cancel_queued(self.c, 'employee', FIRST, {FIRST: queued}))
        self.assertFalse(queued.cancelled())
        job_control.request_cancel(self.c, 'employee', FIRST)
        self.assertFalse(job_control.cancel_queued(self.c, 'employee', FIRST, {FIRST: queued}))
        self.assertFalse(queued.cancelled())
        self.c.commit()
        self.assertFalse(job_control.cancel_queued(self.c, 'other', FIRST, {FIRST: queued}))
        self.assertTrue(job_control.cancel_queued(self.c, 'employee', FIRST, {FIRST: queued}))
        self.assertTrue(queued.cancelled())

    def test_running_future_is_not_killed_or_removed(self):
        self.add()
        running = Future()
        running.set_running_or_notify_cancel()
        active = {FIRST: running}
        job_control.request_cancel(self.c, 'employee', FIRST)
        self.c.commit()
        self.assertFalse(job_control.cancel_queued(self.c, 'employee', FIRST, active))
        self.assertIs(active[FIRST], running)
        running.set_result({'summary': 'late'})
        self.assertFalse(job_control.store_result(self.c, 'employee', FIRST, running.result()))

    def test_finish_wins_race_and_only_one_finish_is_accepted(self):
        self.add()
        self.assertFalse(job_control.store_result(self.c, 'other', FIRST, {'summary': 'wrong owner'}))
        self.assertTrue(job_control.store_result(self.c, 'employee', FIRST, {'summary': 'finished'}))
        self.assertFalse(job_control.store_result(self.c, 'employee', FIRST, {'summary': 'overwritten'}))
        self.assertEqual(job_control.request_cancel(self.c, 'employee', FIRST)['status'], 'finished')
        self.assertIn('finished', self.c.execute('SELECT result FROM jobs').fetchone()[0])

    def test_no_id_uses_latest_owned_active_job_but_never_falls_back_past_unsafe_work(self):
        self.add()
        self.add(job=SECOND, kind='invitation', created=2)
        self.assertEqual(job_control.request_cancel(self.c, 'employee')['status'], 'not_cancelable')
        self.assertFalse(job_control.is_cancelled(self.c, 'employee', FIRST))

    def test_malformed_job_input_is_not_guessed_safe(self):
        self.add()
        for raw in ('not json', '[]', 'null', 'true', '{"kind":[]}', '{"kind":{}}'):
            self.c.execute('UPDATE jobs SET input=? WHERE id=?', (raw, FIRST))
            self.assertEqual(job_control.request_cancel(self.c, 'employee', FIRST)['status'], 'not_cancelable')

    def test_restart_retains_active_dedup_and_canceled_result(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'jobs.sqlite3'
            with sqlite3.connect(path) as c:
                c.executescript('''CREATE TABLE jobs (id TEXT PRIMARY KEY,owner TEXT,fingerprint TEXT,
                    created REAL,state TEXT,input TEXT,result TEXT);''')
                c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,NULL)',
                    (FIRST, 'employee', FINGERPRINT, 1, 'running', '{"kind":"photo"}'))
            with sqlite3.connect(path) as c:
                self.assertEqual(job_control.find_active(c, 'employee', FINGERPRINT)['id'], FIRST)
                job_control.request_cancel(c, 'employee', FIRST)
            with sqlite3.connect(path) as c:
                self.assertTrue(job_control.is_cancelled(c, 'employee', FIRST))
                self.assertFalse(job_control.store_result(c, 'employee', FIRST, {'summary': 'late'}))

    def test_queued_job_can_be_canceled_but_cannot_be_reused_or_completed_as_running(self):
        self.add(state='queued')
        self.assertIsNone(job_control.find_active(self.c, 'employee', FINGERPRINT))
        self.assertFalse(job_control.store_result(self.c, 'employee', FIRST, {'summary': 'not started'}))
        self.assertEqual(job_control.request_cancel(self.c, 'employee', FIRST)['status'], 'cancelled')

    def test_invalid_identifiers_are_rejected_without_including_input(self):
        for callback in (
            lambda: job_control.find_active(self.c, '', FINGERPRINT),
            lambda: job_control.find_active(self.c, 'employee', 'private'),
            lambda: job_control.request_cancel(self.c, 'employee', 'private'),
        ):
            with self.assertRaises(ValueError) as error:
                callback()
            self.assertNotIn('private', str(error.exception))
