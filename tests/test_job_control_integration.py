"""Synthetic concurrency tests through real authenticated dispatch/job storage."""
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import agent


TODAY = '오늘 전체 배송 몇 건?'
TOMORROW = '내일 전체 배송 몇 건?'
URL = 'https://secure.kakaocdn.net/dna/fixture-a.jpg'


def payload(message, owner='employee', url=None):
    result = {'userRequest': {'user': {'id': owner}, 'utterance': message}}
    if url:
        result['action'] = {'params': {'secureimage': {'privacyAgreement': 'Y', 'secureUrls': [url]}}}
    return result


class JobControlIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(agent, 'ROOT', Path(directory)))
        self.enterContext(patch.object(agent, 'ACTIVE', {}))
        self.enterContext(patch.object(agent, 'LOCK', threading.Lock()))
        self.workers = self.enterContext(ThreadPoolExecutor(max_workers=4))
        self.enterContext(patch.object(agent, 'POOL', self.workers))
        self.callers = self.enterContext(ThreadPoolExecutor(max_workers=4))
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        with agent.db() as c:
            c.executemany('INSERT INTO staff VALUES (?,?,0)', [('employee', 'staff'), ('other', 'staff')])

    def rows(self):
        with agent.db() as c:
            return [dict(row) for row in c.execute('SELECT * FROM jobs ORDER BY created,id').fetchall()]

    def start_blocked(self):
        started = threading.Event()
        def read(message, history, owner):
            started.set()
            if not self.release.wait(8):
                raise RuntimeError('synthetic barrier was not released')
            return {'summary': '모의 읽기 최종 결과'}
        provider = self.enterContext(patch.object(agent, 'handle', side_effect=read))
        response = self.callers.submit(agent.dispatch, payload(TODAY))
        self.assertTrue(started.wait(3))
        job = self.rows()[0]['id']
        worker = agent.ACTIVE[job]
        return response, worker, job, provider

    def test_concurrent_duplicate_reuses_one_running_job_but_completed_query_reads_again(self):
        response, worker, job, provider = self.start_blocked()
        second = agent.dispatch(payload(TODAY))
        self.assertIn('같은 요청을 이미', str(second))
        self.assertIn('결과 ' + job, str(second))
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(len(self.rows()), 1)
        self.release.set()
        worker.result(timeout=3)
        response.result(timeout=3)
        self.assertIn('모의 읽기 최종 결과', str(agent.dispatch(payload(TODAY))))
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row['state'] == 'done' for row in self.rows()))

    def test_different_read_condition_runs_separately_while_first_waits(self):
        started = threading.Event()
        def read(message, history, owner):
            if message == TODAY:
                started.set()
                if not self.release.wait(8):
                    raise RuntimeError('synthetic barrier was not released')
            return {'summary': '첫 조회' if message == TODAY else '다른 조건 조회'}
        with patch.object(agent, 'handle', side_effect=read) as provider:
            first = self.callers.submit(agent.dispatch, payload(TODAY))
            self.assertTrue(started.wait(3))
            second = agent.dispatch(payload(TOMORROW))
            self.assertIn('다른 조건 조회', str(second))
            self.assertNotIn('앞에서 말씀하신 일', str(second))
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(len(self.rows()), 2)
            self.release.set()
            first.result(timeout=3)

    def test_cancelled_read_allows_next_and_late_result_cannot_overwrite_or_leak_callback(self):
        response, worker, job, _ = self.start_blocked()
        answer = agent.dispatch(payload('작업 취소 ' + job))
        self.assertIn('조회 결과 전달을 중단', str(answer))
        self.assertEqual(self.rows()[0]['state'], 'cancelled')
        with patch.object(agent, 'handle', return_value={'summary': '새 조회 결과'}):
            self.assertIn('새 조회 결과', str(agent.dispatch(payload(TOMORROW))))
        self.release.set()
        self.assertNotIn('모의 읽기 최종 결과', worker.result(timeout=3)['summary'])
        self.assertNotIn('모의 읽기 최종 결과', str(response.result(timeout=3)))
        saved = next(row for row in self.rows() if row['id'] == job)
        self.assertEqual(saved['state'], 'cancelled')
        self.assertNotIn('모의 읽기 최종 결과', saved['result'])
        sent = []
        def fake_open(request, timeout):
            sent.append(json.loads(request.data))
            return BytesIO(b'{"status":"SUCCESS"}')
        opener = Mock()
        opener.open.side_effect = fake_open
        with patch.object(agent, 'build_opener', return_value=opener):
            agent.send_callback('https://bot-api.kakao.com/fixture-only', job, worker, 'employee')
        self.assertEqual(len(sent), 1)
        self.assertIn('조회 결과 전달을 중단', str(sent[0]))
        self.assertNotIn('모의 읽기 최종 결과', str(sent[0]))

    def test_cross_owner_and_unapproved_account_cannot_cancel_or_read_another_job(self):
        response, worker, job, provider = self.start_blocked()
        self.assertIn('직원 신청', str(agent.dispatch(payload('작업 취소 ' + job, 'unapproved'))))
        self.assertIn('이 계정의 진행 중인 작업을 찾지 못',
                      str(agent.dispatch(payload('작업 취소 ' + job, 'other'))))
        self.assertNotIn('모의 읽기 최종 결과', str(agent.dispatch(payload('결과 ' + job, 'other'))))
        self.assertEqual(self.rows()[0]['state'], 'running')
        self.assertEqual(provider.call_count, 1)
        agent.dispatch(payload('작업 취소 ' + job))
        self.release.set()
        worker.result(timeout=3)
        response.result(timeout=3)

    def test_queued_read_cancellation_returns_saved_cancellation_instead_of_future_error(self):
        # Occupy each worker so the business Future is canceled before it starts.
        blockers = [self.workers.submit(self.release.wait, 8) for _ in range(4)]
        submitted = threading.Event()
        def submit(*args, **kwargs):
            future = self.workers.submit(*args, **kwargs)
            submitted.set()
            return future
        proxy = Mock()
        proxy.submit.side_effect = submit
        with patch.object(agent, 'POOL', proxy), patch.object(agent, 'handle') as read:
            response = self.callers.submit(agent.dispatch, payload(TODAY))
            self.assertTrue(submitted.wait(3))
            with agent.LOCK:
                job = self.rows()[0]['id']
                worker = agent.ACTIVE[job]
            answer = agent.dispatch(payload('작업 취소 ' + job))
            self.assertIn('조회 결과 전달을 중단', str(answer))
            self.assertTrue(worker.cancelled())
            self.assertIn('조회 결과 전달을 중단', str(response.result(timeout=3)))
            self.assertEqual(self.rows()[0]['state'], 'cancelled')
            read.assert_not_called()
        self.release.set()
        for blocker in blockers:
            blocker.result(timeout=3)

    def test_invitation_order_and_legacy_route_cancellation_decline_without_touching_work(self):
        for number, kind in enumerate(('invitation', 'order', 'route')):
            job = f'{number:012x}'
            with agent.db() as c:
                c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,NULL)',
                          (job, 'employee', str(number) * 64, number, 'running', json.dumps({'kind': kind})))
            future = Mock()
            agent.ACTIVE[job] = future
            with self.subTest(kind=kind):
                answer = agent.dispatch(payload('작업 취소 ' + job))
                self.assertIn('여기서 취소하지 않았어요', str(answer))
                self.assertEqual(next(row for row in self.rows() if row['id'] == job)['state'], 'running')
                future.cancel.assert_not_called()

    def test_guarded_route_cancellation_keeps_previous_saved_session_and_discards_late_result(self):
        previous = {'days': ['2026-11-14'], 'nearGap': 120, 'delay': 0,
                    'excluded': [], 'skipOrders': [], 'manualTimes': {}}
        with agent.db() as c:
            c.execute('INSERT INTO route_sessions VALUES (?,?,?)',
                      ('employee', time.time(), json.dumps(previous)))
        started = threading.Event()
        def calculate(settings, today, clock, read, db):
            started.set()
            if not self.release.wait(8):
                raise RuntimeError('synthetic route barrier was not released')
            return {day: {'routes': [], 'missed': [], 'held': 0} for day in settings['days']}
        now = datetime(2026, 11, 14, 12, tzinfo=agent.SEOUL)
        with patch.object(agent, 'datetime', wraps=datetime) as clock, \
             patch.object(agent.delivery_routes, 'calculate', side_effect=calculate):
            clock.now.return_value = now
            response = self.callers.submit(agent.dispatch, payload('2026-11-15 구로 배송루트 이동시간 10분 추가'))
            self.assertTrue(started.wait(3))
            row = self.rows()[0]
            self.assertIs(json.loads(row['input'])['cancel_safe'], True)
            worker = agent.ACTIVE[row['id']]
            answer = agent.dispatch(payload('작업 취소 ' + row['id']))
            self.assertIn('새 배정안으로 반영하지 않아요', str(answer))
            self.assertEqual(agent.delivery_routes.session(agent.db, 'employee'), previous)
            self.release.set()
            result = worker.result(timeout=3)
            response.result(timeout=3)
        self.assertTrue(result['cancelled'])
        self.assertNotIn('route_report', result)
        self.assertEqual(agent.delivery_routes.session(agent.db, 'employee'), previous)
        self.assertEqual(self.rows()[0]['state'], 'cancelled')
        self.assertNotIn('_route_settings', self.rows()[0]['result'])

    def test_completed_guarded_route_commits_session_and_cannot_be_undone_by_late_cancel(self):
        now = datetime(2026, 11, 14, 12, tzinfo=agent.SEOUL)
        def calculate(settings, today, clock, read, db):
            return {day: {'routes': [], 'missed': [], 'held': 0} for day in settings['days']}
        with patch.object(agent, 'datetime', wraps=datetime) as clock, \
             patch.object(agent.delivery_routes, 'calculate', side_effect=calculate):
            clock.now.return_value = now
            answer = agent.dispatch(payload('2026-11-15 구로 배송루트 이동시간 10분 추가'))
        self.assertIn('배송루트', str(answer))
        saved = agent.delivery_routes.session(agent.db, 'employee')
        self.assertEqual(saved['days'], ['2026-11-15'])
        self.assertEqual(saved['delay'], 10)
        row = self.rows()[0]
        self.assertEqual(row['state'], 'done')
        self.assertNotIn('_route_settings', row['result'])
        late = agent.dispatch(payload('작업 취소 ' + row['id']))
        self.assertIn('이미 끝난 작업', str(late))
        self.assertEqual(agent.delivery_routes.session(agent.db, 'employee'), saved)

    def test_photo_fingerprints_distinguish_question_and_url_without_persisting_sources(self):
        entered = [threading.Event() for _ in range(3)]
        count_lock = threading.Lock()
        call_count = 0
        def photo(urls, question):
            nonlocal call_count
            with count_lock:
                index = call_count
                call_count += 1
            entered[index].set()
            if not self.release.wait(8):
                raise RuntimeError('synthetic photo barrier was not released')
            return {'summary': '모의 사진 조회 결과'}
        bodies = [payload('완료만', url=URL), payload('미완료만', url=URL),
                  payload('완료만', url=URL.replace('fixture-a', 'fixture-b'))]
        with patch.object(agent, 'photo_order_status', side_effect=photo):
            responses = []
            for index, body in enumerate(bodies):
                responses.append(self.callers.submit(agent.dispatch, body))
                self.assertTrue(entered[index].wait(3))
            duplicate = agent.dispatch(bodies[0])
            self.assertIn('같은 요청을 이미', str(duplicate))
            rows = self.rows()
            self.assertEqual(call_count, 3)
            self.assertEqual(len(rows), 3)
            self.assertEqual(len({row['fingerprint'] for row in rows}), 3)
            stored = json.dumps([(row['input'], row['result']) for row in rows], ensure_ascii=False)
            for private in (URL, 'fixture-b.jpg', '완료만', '미완료만'):
                self.assertNotIn(private, stored)
            workers = list(agent.ACTIVE.values())
            self.release.set()
            for future in workers:
                future.result(timeout=3)
            for future in responses:
                future.result(timeout=3)
            for row in self.rows():
                self.assertEqual(row['state'], 'done')
                self.assertNotIn('kakaocdn', row['result'])
