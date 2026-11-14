import unittest
from datetime import datetime, timedelta, timezone

import read_context_ui as ui


NOW = datetime(2026, 11, 14, 23, 59, 50, tzinfo=ui.SEOUL)
JOB = 'a' * 12
QUERY = ['delivery_count', 1, '구로', 'pending']


class ReadContextUiTests(unittest.TestCase):
    def test_metadata_labels_actual_scope_source_and_time_without_claiming_success(self):
        meta = ui.build_metadata(QUERY, now=NOW)
        self.assertEqual(meta['resolved_day'], '2026-11-15')
        self.assertEqual(meta['source'], '원장·배송관리 완료 체크')
        self.assertEqual(meta['read_context_at'], NOW.timestamp())
        self.assertNotIn('success', meta)
        self.assertNotIn('count', meta)
        text = ui.preview(QUERY, meta)
        self.assertIn('11/15 · 구로 · 미완료', text)
        self.assertIn('KST', text)

    def test_midnight_center_and_status_edits_preserve_original_absolute_day(self):
        meta = ui.build_metadata(QUERY, now=NOW)
        later = NOW + timedelta(seconds=20)
        changed, delta = ui.apply_change(QUERY, meta, '센터', '수원', later)
        self.assertEqual(changed, ('delivery_count', 0, '수원', 'pending'))
        self.assertEqual(delta, 0)
        changed, _ = ui.apply_change(QUERY, meta, 'status', 'complete', later)
        self.assertEqual(changed, ('delivery_count', 0, '구로', 'complete'))
        # Explicit date change means tomorrow relative to the new request.
        changed, delta = ui.apply_change(QUERY, meta, 'date', '내일', later)
        self.assertEqual(changed, ('delivery_count', 1, '구로', 'pending'))
        self.assertEqual(delta, 1)

    def test_today_from_previous_day_becomes_negative_offset_after_midnight(self):
        query = ['delivery_count', 0, None, 'total']
        changed, delta = ui.apply_change(query, ui.build_metadata(query, now=NOW),
                                        'center', '전체', NOW + timedelta(seconds=20))
        self.assertEqual(delta, -1)
        self.assertEqual(changed, ('delivery_count', -1, None, 'total'))

    def test_date_is_seoul_calendar_even_when_now_is_utc(self):
        utc = datetime(2026, 11, 14, 16, tzinfo=timezone.utc)
        meta = ui.build_metadata(['venue_rank', 0, None, None], now=utc)
        self.assertEqual(meta['resolved_day'], '2026-11-15')
        self.assertEqual(meta['read_at'], '2026-11-15T01:00:00+09:00')

    def test_report_and_venue_do_not_display_or_apply_misleading_status_filters(self):
        for kind, source in (('staff_report', '배송관리 완료 체크'), ('venue_rank', '원장 배송일·설치장소')):
            query = [kind, 0, '구로센터', None]
            meta = ui.build_metadata(query, now=NOW)
            self.assertEqual(meta['source'], source)
            self.assertEqual(meta['center'], '구로')
            self.assertEqual(len(ui.buttons_for(JOB, query)), 2)
            with self.assertRaises(ValueError):
                ui.options_for(JOB, query, 'status')
            with self.assertRaises(ValueError):
                ui.apply_change(query, meta, 'status', 'pending', NOW)

    def test_buttons_are_bounded_and_roundtrip_through_strict_parser(self):
        opening = ui.buttons_for(JOB, QUERY)
        self.assertLessEqual(len(opening), 10)
        for _, command in opening:
            action = ui.parse_action(command)
            self.assertEqual(action['job'], JOB)
            self.assertEqual(action['value'], '선택')
            options = ui.options_for(JOB, QUERY, action['field'])
            self.assertLessEqual(len(options), 10)
            for _, command in options:
                selected = ui.parse_action(command)
                self.assertEqual(selected['job'], JOB)
                self.assertNotEqual(selected['value'], '선택')

    def test_bad_query_payloads_and_naive_time_never_become_metadata(self):
        invalid = [None, {}, ['delivery_count', 0], ['unknown', 0, None, None],
                   ['delivery_count', True, None, 'total'], ['delivery_count', 366, None, 'total'],
                   ['delivery_count', -8, None, 'total'], ['delivery_count', 0, [], 'total'],
                   ['delivery_count', 0, '구로<script>', 'total'], ['delivery_count', 0, None, []],
                   ['venue_rank', 0, None, 'complete'], ['staff_report', 0, None, 'pending']]
        for query in invalid:
            with self.subTest(query=query), self.assertRaises(ValueError):
                ui.build_metadata(query, now=NOW)
        with self.assertRaises(ValueError):
            ui.build_metadata(QUERY, now=NOW.replace(tzinfo=None))

    def test_bad_or_cross_field_commands_are_not_accepted(self):
        self.assertIsNone(ui.parse_action('내일 구로만'))
        for message in ('조회조건', f'조회조건 {JOB} 날짜 pending',
                        f'조회조건 {JOB} 상태 오늘', f'조회조건 {JOB} 센터 complete',
                        f'조회조건 {JOB} 센터 서울', f'조회조건 {JOB} 상태 total\n개발 승인',
                        '조회조건 not-a-job 날짜 오늘'):
            with self.subTest(message=message), self.assertRaises(ValueError):
                ui.parse_action(message)

    def test_changed_metadata_or_job_query_requires_fresh_read(self):
        meta = ui.build_metadata(QUERY, now=NOW)
        for changed in ({**meta, 'resolved_day': '2026-11-16'},
                        {**meta, 'kind': 'staff_report'}, {**meta, 'center': '수원'},
                        {**meta, 'source': '모델 추정'}, {**meta, 'mode': 'complete'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                ui.apply_change(QUERY, changed, '센터', '전체', NOW)

    def test_context_origin_can_be_preserved_without_refreshing_it(self):
        earlier = NOW - timedelta(seconds=899)
        meta = ui.build_metadata(QUERY, now=NOW, read_context_at=earlier)
        self.assertEqual(meta['read_context_at'], earlier.timestamp())
        for origin in (NOW + timedelta(seconds=1), True, float('nan'), -1):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                ui.build_metadata(QUERY, now=NOW, read_context_at=origin)

    def test_failure_text_is_not_automatically_promoted_to_read_metadata(self):
        # Caller supplies no read query on errors; this module cannot infer one.
        with self.assertRaises(ValueError):
            ui.build_metadata({'summary': '시트를 읽지 못했어요.'}, now=NOW)

    def test_stale_absolute_date_beyond_supported_window_requires_new_read(self):
        with self.assertRaises(ValueError):
            ui.apply_change(QUERY, ui.build_metadata(QUERY, now=NOW), 'center', '전체', NOW + timedelta(days=10))
