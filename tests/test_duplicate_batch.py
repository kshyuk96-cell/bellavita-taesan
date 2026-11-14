import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duplicate_batch as batch


SNAPSHOT = 'a' * 64
CHANGED = 'b' * 64


class DuplicateBatchTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.folder) / 'queue.sqlite3'
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.addCleanup(self.connection.close)
        batch.setup(self.connection)

    def job(self, rows=(5, 6, 7, 8), size=2, owner='employee'):
        return batch.enqueue(self.connection, owner, SNAPSHOT, list(rows), chunk_size=size)

    def claim(self, job, **kwargs):
        return batch.claim(self.connection, 'employee', job, SNAPSHOT, **kwargs)

    def submit(self, claim, results=None, **overrides):
        args = dict(owner='employee', job_id=claim['job_id'], chunk_index=claim['chunk_index'],
                    lease_token=claim['lease_token'], current_snapshot_hash=SNAPSHOT,
                    checked_rows=claim['row_numbers'], results=results or {})
        args.update(overrides)
        return batch.submit(self.connection, **args)

    def test_deduplicates_active_snapshot_and_resumes_after_reopening_database(self):
        job = self.job()
        self.assertEqual(job, self.job(rows=(8, 7, 6, 5), size=1))
        self.assertNotEqual(job, self.job(owner='other'))
        first = self.claim(job)
        self.submit(first, {'duplicate': [[5, 6]], 'review': [[5, 7]]})
        with sqlite3.connect(self.path) as reopened:
            state = batch.progress(reopened, 'employee', job)
            self.assertEqual((state['status'], state['checked_rows'], state['cursor']), ('running', 2, 2))
            second = batch.claim(reopened, 'employee', job, SNAPSHOT)
            self.assertEqual(second['row_numbers'], [7, 8])

    def test_only_all_checked_chunks_complete_and_results_are_row_numbers_only(self):
        job = self.job()
        first, second = self.claim(job), self.claim(job)
        self.assertIsNone(self.claim(job))
        with self.assertRaises(ValueError):
            self.submit(first, checked_rows=[5])
        self.submit(second, {'different': [[7, 8]], 'review': [[6, 7]]})
        state = batch.progress(self.connection, 'employee', job)
        self.assertEqual((state['checked_rows'], state['cursor'], state['status']), (2, 0, 'running'))
        state = self.submit(first, {'duplicate': [[6, 5]], 'review': [[7, 6]]})
        self.assertEqual((state['checked_rows'], state['cursor'], state['status']), (4, 4, 'completed'))
        self.assertEqual(state['result_counts'], {'duplicate': 1, 'review': 1, 'different': 1})
        self.assertIsNone(self.claim(job))
        self.assertEqual(batch.get_results(self.connection, 'employee', job, category='duplicate'),
                         [{'category': 'duplicate', 'rows': [5, 6]}])
        self.assertEqual(len(batch.get_results(self.connection, 'employee', job, offset=1, limit=1)), 1)
        self.assertNotEqual(job, self.job())

    def test_other_owner_cannot_read_claim_submit_or_cancel(self):
        job = self.job()
        lease = self.claim(job)
        calls = [lambda: batch.progress(self.connection, 'other', job),
                 lambda: batch.get_results(self.connection, 'other', job),
                 lambda: batch.claim(self.connection, 'other', job, SNAPSHOT),
                 lambda: self.submit(lease, owner='other'),
                 lambda: batch.cancel(self.connection, 'other', job)]
        for call in calls:
            with self.subTest(call=call), self.assertRaises(batch.NotFound):
                call()

    def test_snapshot_changes_refuse_claim_and_results_without_mutation(self):
        job = self.job()
        with self.assertRaises(batch.SnapshotChanged):
            batch.claim(self.connection, 'employee', job, CHANGED)
        lease = self.claim(job)
        with self.assertRaises(batch.SnapshotChanged):
            self.submit(lease, {'duplicate': [[5, 6]]}, current_snapshot_hash=CHANGED)
        state = batch.progress(self.connection, 'employee', job)
        self.assertEqual(state['checked_rows'], 0)
        self.assertEqual(batch.get_results(self.connection, 'employee', job), [])

    def test_expired_lease_is_retried_but_old_token_cannot_publish(self):
        job = self.job()
        with patch.object(batch.time, 'time', return_value=100):
            original = self.claim(job, lease_seconds=10)
        with patch.object(batch.time, 'time', return_value=110):
            with self.assertRaises(batch.StaleLease):
                self.submit(original)
            retry = self.claim(job, lease_seconds=10)
            self.assertEqual(retry['chunk_index'], original['chunk_index'])
            self.assertEqual(retry['attempt'], 2)
            self.assertNotEqual(retry['lease_token'], original['lease_token'])
            with self.assertRaises(batch.StaleLease):
                self.submit(original)
            self.submit(retry)

    def test_repeated_expiry_has_bounded_attempts_and_is_never_completed(self):
        job = self.job(rows=(5, 6))
        for attempt in range(batch.MAX_ATTEMPTS):
            with patch.object(batch.time, 'time', return_value=100 + attempt * 10):
                lease = self.claim(job, lease_seconds=10)
                self.assertEqual(lease['attempt'], attempt + 1)
        with patch.object(batch.time, 'time', return_value=100 + batch.MAX_ATTEMPTS * 10):
            self.assertIsNone(self.claim(job))
        self.assertEqual(batch.progress(self.connection, 'employee', job)['status'], 'failed')

    def test_cancellation_stops_new_claims_and_old_results(self):
        job = self.job()
        lease = self.claim(job)
        self.assertEqual(batch.cancel(self.connection, 'employee', job)['status'], 'canceled')
        self.assertIsNone(self.claim(job))
        with self.assertRaises(batch.StaleLease):
            self.submit(lease)
        self.assertEqual(batch.cancel(self.connection, 'employee', job)['checked_rows'], 0)

    def test_concurrent_workers_receive_disjoint_claims_and_enqueue_is_deduplicated(self):
        def enqueue(_):
            with sqlite3.connect(self.path, timeout=5) as connection:
                return batch.enqueue(connection, 'employee', SNAPSHOT, list(range(5, 25)), chunk_size=2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            jobs = list(workers.map(enqueue, range(8)))
        self.assertEqual(len(set(jobs)), 1)
        def claim(_):
            with sqlite3.connect(self.path, timeout=5) as connection:
                return batch.claim(connection, 'employee', jobs[0], SNAPSHOT)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            claims = list(workers.map(claim, range(8)))
        self.assertEqual(len({item['chunk_index'] for item in claims}), 8)
        self.assertEqual(len({item['lease_token'] for item in claims}), 8)

    def test_invalid_results_are_atomic_and_conflicting_category_is_rejected(self):
        job = self.job()
        first = self.claim(job)
        invalids = [dict(duplicate=[[5, 5000]]), dict(duplicate=[[7, 8]]), dict(duplicate=[[5, 5]]),
                    dict(duplicate=[[True, 5]]), dict(duplicate=[['고객원문', 5]]),
                    dict(duplicate=[[5, 6]], review=[[6, 5]]), dict(unknown=[[5, 6]])]
        for results in invalids:
            with self.subTest(results=results), self.assertRaises(ValueError):
                self.submit(first, results)
            self.assertEqual(batch.get_results(self.connection, 'employee', job), [])
        self.submit(first, {'duplicate': [[5, 7]]})
        second = self.claim(job)
        with self.assertRaises(ValueError):
            self.submit(second, {'different': [[5, 7]]})
        self.assertEqual(batch.progress(self.connection, 'employee', job)['checked_rows'], 2)
        self.submit(second, {'duplicate': [[7, 5]]})
        self.assertEqual(len(batch.get_results(self.connection, 'employee', job)), 1)

    def test_row_chunk_and_result_limits_and_no_order_table_changes(self):
        self.connection.execute('CREATE TABLE orders (id INTEGER PRIMARY KEY, value TEXT)')
        self.connection.execute("INSERT INTO orders VALUES (1,'unchanged')")
        self.connection.commit()
        for rows, size in [([], 2), ([5, 5], 2), ([True], 2), ([5], 0), ([5], 501), ([5] * 50001, 2)]:
            with self.subTest(rows=len(rows), size=size), self.assertRaises(ValueError):
                self.job(rows=rows, size=size)
        job = self.job()
        lease = self.claim(job)
        with patch.object(batch, 'MAX_CHUNK_RESULTS', 1), self.assertRaises(ValueError):
            self.submit(lease, {'duplicate': [[5, 6], [5, 7]]})
        with patch.object(batch, 'MAX_RESULTS', 1), self.assertRaises(ValueError):
            self.submit(lease, {'duplicate': [[5, 6], [5, 7]]})
        self.assertEqual(batch.get_results(self.connection, 'employee', job), [])
        self.assertEqual(self.connection.execute('SELECT * FROM orders').fetchall(), [(1, 'unchanged')])

    def test_existing_caller_transaction_remains_under_caller_control(self):
        self.connection.execute('BEGIN')
        job = self.job()
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        with self.assertRaises(batch.NotFound):
            batch.progress(self.connection, 'employee', job)


if __name__ == '__main__':
    unittest.main()
