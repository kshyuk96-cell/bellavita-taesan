"""Authenticated runtime checks with synthetic, injected read providers only."""
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

import agent
import order_location_provider
import read_resilience
import regression_runners
from test_delivery_counts import fixture


NOW = datetime(2026, 11, 14, 12, tzinfo=agent.SEOUL)
ROAD = '서울 강남구 테헤란로 10'


class KakaoResponse:
    status_code = 200
    url = order_location_provider.ENDPOINT
    history = ()
    content = b'{}'

    def json(self):
        return {'meta': {'total_count': 1, 'pageable_count': 1, 'is_end': True},
                'documents': [{'address': {'address_name': '서울 강남구 역삼동 123'},
                               'road_address': {'address_name': ROAD, 'building_name': '가상센터'}}]}

    def close(self):
        pass


def private_http_error(code):
    return HTTPError('https://private.invalid/private-key', code, 'PRIVATE_EXCEPTION', {}, None)


class BatchImprovementsIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(agent, 'ROOT', Path(directory)))
        self.enterContext(patch.dict(os.environ, {
            'TAESAN_DATA': directory, 'TAESAN_MODEL': 'fixture-model',
            'OPENAI_API_KEY': 'fixture-only-not-a-real-key',
            'TAESAN_WEBHOOK_SECRET': 'fixture-only-secret-32-characters',
            'TAESAN_DELIVERY_READ_SHEET_ID': 'fixture-copy-only',
            'TAESAN_SHEET_ID': 'fixture-copy-only',
        }, clear=True))
        self.enterContext(patch.object(agent, 'ACTIVE', {}))
        self.now = NOW
        clock = self.enterContext(patch.object(agent, 'datetime', wraps=datetime))
        clock.now.side_effect = lambda tz=None: self.now if tz else self.now.replace(tzinfo=None)
        self.enterContext(patch('agent.time.time', side_effect=lambda: self.now.timestamp()))
        self.choose = self.enterContext(patch.object(agent, 'choose', side_effect=AssertionError('unexpected model call')))
        self.network = self.enterContext(patch('requests.sessions.Session.request',
                                               side_effect=AssertionError('unexpected network call')))
        self.model = self.enterContext(patch('openai.OpenAI', side_effect=AssertionError('unexpected paid model')))
        self.sheets = self.enterContext(patch.object(agent, 'sheets_service',
                                                    side_effect=AssertionError('unexpected Sheets connection')))
        with agent.db() as connection:
            connection.executemany('INSERT INTO staff VALUES (?,?,0)',
                                   [('manager', 'admin'), ('employee', 'staff'), ('other', 'staff')])

    def tearDown(self):
        self.choose.assert_not_called()
        self.network.assert_not_called()
        self.model.assert_not_called()
        self.sheets.assert_not_called()

    def send(self, text, owner='employee'):
        return agent.dispatch({'userRequest': {'user': {'id': owner}, 'utterance': text}})

    def latest(self, owner='employee'):
        with agent.db() as connection:
            row = connection.execute('SELECT id,result FROM jobs WHERE owner=? ORDER BY rowid DESC LIMIT 1',
                                     (owner,)).fetchone()
        return row['id'], json.loads(row['result'])

    def synthetic_read(self, ranges, **kwargs):
        data = fixture()
        return [data[key] for key in ranges]

    def test_authenticated_diagnostics_run_real_injected_probes_only_for_admin(self):
        with patch.dict(os.environ, {'KAKAO_REST_API_KEY': 'fixture-map-key',
                                     'TAESAN_SCROLL_DIR': '/private/fixture-pc-path'}), \
             patch.object(agent, 'read_staff_delivery', return_value=[
                 [['배송일', '행사시간']], [['보내는분 요약']]]) as sheets, \
             patch('openai.OpenAI') as model, \
             patch.object(order_location_provider, 'make_resolver') as factory:
            model.return_value.models.retrieve.return_value = SimpleNamespace(id='fixture-model')
            provider = Mock(return_value={'status': 'unique', 'verified': True})
            factory.return_value = provider
            response = str(self.send('연결 점검', 'manager'))
            self.assertEqual(response.count('실제 읽기 응답을 확인했어요.'), 3)
            self.assertIn('제작 PC', response)
            self.assertIn('미확인', response)
            self.assertNotIn('/private/', response)
            self.assertNotIn('fixture-map-key', response)
            sheets.assert_called_once()
            model.return_value.models.retrieve.assert_called_once_with('fixture-model')
            provider.assert_called_once_with('서울특별시 중구 세종대로 110')
            sheets.reset_mock()
            model.reset_mock()
            factory.reset_mock()
            restricted = str(self.send('연결 점검'))
            self.assertIn('대표 계정', restricted)
            self.assertNotIn('제작 PC', restricted)
            sheets.assert_not_called()
            model.assert_not_called()
            factory.assert_not_called()

    def test_failed_reads_keep_scope_but_never_metadata_or_fabricated_zero(self):
        errors = [(private_http_error(403), 'permission'), (private_http_error(404), 'not_found'),
                  (private_http_error(429), 'rate_limit'), (TimeoutError('PRIVATE_EXCEPTION'), 'timeout'),
                  (read_resilience.SchemaReadError('PRIVATE_EXCEPTION'), 'schema')]
        for error, expected in errors:
            with self.subTest(expected=expected), patch.object(agent, 'read_delivery_ranges', side_effect=error):
                response = str(self.send('내일 구로센터 배송 미완료 몇 건?'))
                _, result = self.latest()
                self.assertEqual(result['read_failed'], expected)
                self.assertEqual(result['read_query'], ['delivery_count', 1, '구로', 'pending'])
                self.assertNotIn('read_metadata', result)
                self.assertNotIn('read_performed', result)
                self.assertNotIn('0건', response)
                self.assertNotIn('PRIVATE_EXCEPTION', response)
                self.assertNotIn('private.invalid', response)
                self.assertNotIn('조회조건', response)
        with agent.db() as connection:
            rows = connection.execute('SELECT * FROM failure_observations').fetchall()
        self.assertEqual({row['error_code'] for row in rows}, {kind for _, kind in errors})
        self.assertTrue(all(row['task_kind'] == 'delivery_count' for row in rows))
        self.assertNotIn('PRIVATE_EXCEPTION', str([tuple(row) for row in rows]))

    def test_actual_range_reader_retries_429_once_but_never_permission_or_schema(self):
        failed = Mock()
        failed.raise_for_status.side_effect = private_http_error(429)
        successful = Mock()
        successful.json.return_value = {'valueRanges': [{'values': [['배송일'], [123]]}]}
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.side_effect = [failed, successful]
        with patch.object(agent, 'sheets_service', return_value=session) as factory, \
             patch('read_resilience.time.sleep') as wait:
            actual = agent.read_delivery_ranges(["'액자화환'!B4:B5"], spreadsheet='fixture-copy-only')
        self.assertEqual(actual, [[['배송일'], [123]]])
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(session.get.call_count, 2)
        wait.assert_called_once_with(0.1)
        self.assertTrue(all('fixture-copy-only' in call.args[0] for call in session.get.call_args_list))
        for category in ('permission', 'schema'):
            session.reset_mock()
            response = Mock()
            if category == 'permission':
                response.raise_for_status.side_effect = private_http_error(403)
                expected = HTTPError
            else:
                response.json.return_value = {'valueRanges': []}
                expected = read_resilience.SchemaReadError
            session.get.side_effect = None
            session.get.return_value = response
            with patch.object(agent, 'sheets_service', return_value=session) as factory, \
                 patch('read_resilience.time.sleep') as wait, self.assertRaises(expected):
                agent.read_delivery_ranges(["'액자화환'!B4:B5"], spreadsheet='fixture-copy-only')
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(session.get.call_count, 1)
            wait.assert_not_called()

    def test_admin_regression_command_runs_four_actual_local_runners_staff_denied(self):
        import order_locations
        import staff_read_guard
        with patch.object(order_locations, 'compare_locations', wraps=order_locations.compare_locations) as locations, \
             patch.object(staff_read_guard, 'guarded_read', wraps=staff_read_guard.guarded_read) as guard, \
             patch.object(read_resilience, 'classify_read_error', wraps=read_resilience.classify_read_error) as classifier:
            response = str(self.send('회귀 검증 실행', 'manager'))
        self.assertIn('통과 4', response)
        self.assertIn('실제 운영 점수·배포 완료를 뜻하지 않아요.', response)
        self.assertEqual(locations.call_count, 2)
        self.assertEqual(guard.call_count, 1)
        self.assertEqual(classifier.call_count, 1)
        with agent.db() as connection:
            row = connection.execute('SELECT * FROM synthetic_replays').fetchone()
            self.assertEqual((row['passed'], row['failed'], row['errors'], row['unsupported']), (4, 0, 0, 0))
            self.assertEqual(len(json.loads(row['outcomes'])), 4)
        self.assertIn('대표 계정', str(self.send('회귀 검증 실행')))
        with agent.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM synthetic_replays').fetchone()[0], 1)

    def test_runtime_location_cache_warm_expiry_and_failed_refresh_invalidate_success(self):
        query = ROAD + ' 가상센터 3층 그랜드홀 PRIVATE_QUERY_MARKER'
        get = Mock(side_effect=[KakaoResponse(), KakaoResponse(), TimeoutError('PRIVATE_EXCEPTION')])
        original_factory = order_location_provider.make_resolver
        with patch.dict(os.environ, {'KAKAO_REST_API_KEY': 'fixture-map-key'}), \
             patch.object(order_location_provider, 'make_resolver',
                          side_effect=lambda key: original_factory(key, get=get)):
            first = agent.location_resolver()(query)
            second = agent.location_resolver()(query)
            self.assertEqual(first['status'], 'unique')
            self.assertEqual(second['query'], query)
            self.assertEqual(get.call_count, 1)
            with agent.db() as connection:
                row = connection.execute('SELECT * FROM duplicate_location_evidence').fetchone()
                self.assertEqual(row['query_hash'], hashlib.sha256(query.encode()).hexdigest())
                self.assertNotIn(query, str(tuple(row)))
                self.assertNotIn('PRIVATE_QUERY_MARKER', row['evidence'])
                self.assertNotIn('"query"', row['evidence'])
                connection.execute('UPDATE duplicate_location_evidence SET expires=?',
                                   (self.now.timestamp() - 1,))
            self.assertEqual(agent.location_resolver()(query)['status'], 'unique')
            self.assertEqual(get.call_count, 2)
            failed = agent.location_resolver(refresh=True)(query)
            self.assertEqual(failed['status'], 'unresolved')
            self.assertEqual(get.call_count, 3)
            self.assertNotIn('PRIVATE_EXCEPTION', str(failed))
            with agent.db() as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM duplicate_location_evidence').fetchone()[0], 0)

    def test_success_metadata_and_buttons_use_actual_changed_date_center_status_query(self):
        with patch.object(agent, 'read_delivery_ranges', side_effect=self.synthetic_read) as read:
            response = self.send('내일 전체 배송 몇 건?')
            job, initial = self.latest()
            self.assertEqual(initial['read_query'], ['delivery_count', 1, None, 'total'])
            self.assertEqual(initial['read_metadata']['resolved_day'], '2026-11-15')
            self.assertIn('원장 배송일', str(response))
            self.assertTrue(any(button['messageText'] == f'조회조건 {job} 센터 선택'
                                for button in response['template']['quickReplies']))
            self.assertIn('11/15', str(self.send(f'조회 조건 {job}')))
            before = read.call_count
            self.send(f'조회조건 {job} 센터 구로')
            changed_job, changed = self.latest()
            self.assertGreater(read.call_count, before)
            self.assertEqual(changed['read_query'], ['delivery_count', 1, '구로', 'total'])
            self.assertEqual(changed['read_metadata']['center'], '구로')
            before = read.call_count
            self.send(f'조회조건 {changed_job} 상태 complete')
            _, status = self.latest()
            self.assertGreater(read.call_count, before)
            self.assertEqual(status['read_query'], ['delivery_count', 1, '구로', 'complete'])
            self.assertIn('배송완료는 0건', status['summary'])

    def test_condition_buttons_are_owner_scoped_and_expire_without_reading(self):
        with patch.object(agent, 'read_delivery_ranges', side_effect=self.synthetic_read) as read:
            self.send('내일 전체 배송 몇 건?')
            job, _ = self.latest()
            before = read.call_count
            self.assertIn('없거나 만료', str(self.send(f'조회조건 {job} 센터 구로', 'other')))
            self.assertEqual(read.call_count, before)
            self.now += timedelta(seconds=901)
            self.assertIn('없거나 만료', str(self.send(f'조회조건 {job} 센터 구로')))
            self.assertEqual(read.call_count, before)

    def test_condition_change_after_midnight_preserves_original_absolute_day(self):
        self.now = datetime(2026, 11, 14, 23, 59, 58, tzinfo=agent.SEOUL)
        with patch.object(agent, 'read_delivery_ranges', side_effect=self.synthetic_read) as read:
            self.send('내일 전체 배송 몇 건?')
            job, first = self.latest()
            self.assertEqual(first['read_metadata']['resolved_day'], '2026-11-15')
            self.now = datetime(2026, 11, 15, 0, 0, 5, tzinfo=agent.SEOUL)
            before = read.call_count
            self.send(f'조회조건 {job} 센터 구로')
            _, changed = self.latest()
            self.assertGreater(read.call_count, before)
            self.assertEqual(changed['read_query'], ['delivery_count', 0, '구로', 'total'])
            self.assertEqual(changed['read_metadata']['resolved_day'], '2026-11-15')
            self.assertIn('11월 15일', changed['summary'])


if __name__ == '__main__':
    unittest.main()
