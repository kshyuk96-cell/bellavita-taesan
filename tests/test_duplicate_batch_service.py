import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import duplicate_batch
import duplicate_batch_service as service


ROAD = '서울특별시 강남구 테헤란로 10'
A = ROAD + ' 가상센터 3층 그랜드홀'
B = '그랜드홀 3층 가상센터 서울 강남구 역삼동 123'


def resolver(query):
    return dict(status='unique', verified=True, provider='fixture', query=query,
                aliases=[ROAD, '서울 강남구 역삼동 123'], canonical_road_address=ROAD,
                canonical_building_name='가상센터')


class Factory:
    registry_version = 'fixture-registry-v1'

    def __init__(self, provider=resolver):
        self.provider, self.calls = provider, 0

    def __call__(self):
        self.calls += 1
        return self.provider


def ledger(count=2):
    return [[['배송일', '행사시간']] + [[46369, .5] for _ in range(count)],
            [['설치장소']] + [[A if i % 2 == 0 else B] for i in range(count)],
            [['보내는분 요약']] + [['합성발신자'] for _ in range(count)],
            [['표식']] + [[f'fixture-{i}'] for i in range(count)]]


class DuplicateBatchServiceTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'queue.sqlite3'
        self.db = lambda: sqlite3.connect(self.path)
        self.data = ledger()
        self.read = Mock(side_effect=lambda ranges: copy.deepcopy(self.data))
        self.factory = Factory()

    def start(self, target=5, owner='staff', factory=True):
        return service.start(self.db, owner, target, self.read, self.factory if factory else None)

    def next(self, job, owner='staff', factory=True):
        return service.process_next(self.db, owner, job, self.read, self.factory if factory else None)

    def test_more_than_forty_candidates_complete_in_chunks_and_persist_only_safe_metadata(self):
        self.data = ledger(47)
        state = self.start()
        job = state['job_id']
        self.assertEqual(state['candidate_count'], 46)
        self.assertEqual(self.start()['job_id'], job)
        iterations = 0
        while state['status'] != 'completed':
            before = state['checked_rows']
            state = self.next(job)
            self.assertLessEqual(state['checked_rows'] - before, 10)
            iterations += 1
        self.assertEqual(iterations, 5)
        self.assertEqual(state['result_counts'], {'duplicate': 46, 'review': 0, 'different': 0})
        self.assertEqual(self.factory.calls, 5)
        self.assertNotIn('합성발신자', state['summary'])
        self.assertNotIn(ROAD, state['summary'])
        for call in self.read.call_args_list:
            self.assertEqual(call.args[0], service.RANGES)
        with self.db() as connection:
            dump = '\n'.join(connection.iterdump())
        self.assertNotIn('합성발신자', dump)
        self.assertNotIn(ROAD, dump)
        self.assertNotIn('fixture-0', dump)

    def test_missing_place_is_review_and_different_floor_is_different(self):
        self.data = ledger(4)
        self.data[1][2] = []
        self.data[1][3] = [ROAD + ' 4층 그랜드홀']
        state = self.next(self.start()['job_id'])
        self.assertEqual(state['result_counts'], {'duplicate': 1, 'review': 1, 'different': 1})
        self.assertIn('장소 확인 필요', state['summary'])
        self.assertEqual(state['results']['review'], [[5, 6]])
        self.data[1][1] = []
        state = self.next(self.start()['job_id'])
        self.assertEqual(state['result_counts']['review'], 3)

    def test_target_outside_first_chunk_keeps_actual_sheet_row_numbers(self):
        self.data = ledger(13)
        state = self.start(target=17)
        first = self.next(state['job_id'])
        self.assertEqual(first['status'], 'running')
        self.assertEqual(first['result_counts']['duplicate'], 10)
        self.assertTrue(all(17 in pair for pair in first['results']['duplicate']))
        state = self.next(state['job_id'])
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['result_counts']['duplicate'], 12)
        self.assertEqual(state['target_row'], 17)

    def test_missing_key_or_provider_failure_never_reports_different_or_no_duplicates(self):
        state = self.start(factory=False)
        state = self.next(state['job_id'], factory=False)
        self.assertEqual(state['result_counts'], {'duplicate': 0, 'review': 1, 'different': 0})
        self.assertIn('근거가 부족', state['summary'])
        self.factory.provider = Mock(side_effect=TimeoutError('private api details'))
        state = self.next(self.start()['job_id'])
        self.assertEqual(state['result_counts']['review'], 1)
        self.assertNotIn('private api details', state['summary'])

    def test_zero_candidates_does_not_enqueue_and_uses_exact_date_time_sender_filter(self):
        self.data = ledger(4)
        self.data[0][2] = [46370, .5]
        self.data[0][3] = [46369, '12:00:01']
        self.data[2][4] = ['다른합성발신자']
        state = self.start()
        self.assertIsNone(state['job_id'])
        self.assertEqual(state['candidate_count'], 0)
        self.assertIn('실제 장소 대조는 실행하지 않았어요', state['summary'])
        self.assertEqual(self.factory.calls, 0)

    def test_snapshot_change_rejects_results_and_stops_old_job(self):
        state = self.start()
        self.data[3][1] = ['changed-stable-marker']
        with self.assertRaises(duplicate_batch.SnapshotChanged):
            self.next(state['job_id'])
        old = service.show(self.db, 'staff', state['job_id'])
        self.assertEqual(old['status'], 'canceled')
        self.assertIn('자료가 바뀌어', old['summary'])
        self.assertEqual(old['checked_rows'], 0)
        self.assertNotEqual(self.start()['job_id'], state['job_id'])

    def test_changes_during_provider_lookup_cannot_be_published(self):
        def mutate(query):
            self.data[1][2] = ['바뀐 실제 장소']
            return resolver(query)
        self.factory.provider = mutate
        state = self.start()
        with self.assertRaises(duplicate_batch.SnapshotChanged):
            self.next(state['job_id'])
        self.assertEqual(service.show(self.db, 'staff', state['job_id'])['checked_rows'], 0)

    def test_registry_version_changes_and_candidate_membership_changes_are_stale(self):
        state = self.start()
        self.factory.registry_version = 'fixture-registry-v2'
        with self.assertRaises(duplicate_batch.SnapshotChanged):
            self.next(state['job_id'])
        state = self.start()
        self.data[2][2] = ['changed-sender']
        with self.assertRaises(duplicate_batch.SnapshotChanged):
            self.next(state['job_id'])

    def test_cancel_and_other_owner_never_read_private_source(self):
        state = self.start()
        self.read.reset_mock()
        for call in [lambda: service.show(self.db, 'other', state['job_id']),
                     lambda: service.cancel(self.db, 'other', state['job_id']),
                     lambda: self.next(state['job_id'], owner='other')]:
            with self.assertRaises(duplicate_batch.NotFound):
                call()
        self.read.assert_not_called()
        canceled = service.cancel(self.db, 'staff', state['job_id'])
        self.assertEqual(canceled['status'], 'canceled')
        self.assertEqual(self.next(state['job_id'])['status'], 'canceled')
        self.read.assert_not_called()

    def test_source_errors_are_safe_and_header_or_limits_block_before_provider(self):
        self.read.side_effect = TimeoutError('private customer address url')
        with self.assertRaises(service.SourceReadError) as caught:
            self.start()
        self.assertNotIn('private', str(caught.exception))
        self.read.side_effect = lambda ranges: copy.deepcopy(self.data)
        self.data[0][0][1] = '배송시각'
        with self.assertRaises(service.SourceDataError):
            self.start()
        self.data = ledger(5002)
        with self.assertRaises(service.SourceDataError):
            self.start()
        self.assertEqual(self.factory.calls, 0)


if __name__ == '__main__':
    unittest.main()
