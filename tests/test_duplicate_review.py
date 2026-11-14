import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

import duplicate_review as review


QUERY = '서울특별시 강남구 테헤란로 10 가상센터 3층 그랜드홀'
SOURCE = 'fixture-address-provider'
VERSION = 'actual-response-v1-parser-v2'


def snapshot(row=5, order_id='immutable-fixture-1'):
    return dict(row_number=row, delivery_date=46369, event_time=.5,
                location=QUERY, sender_summary='가상 업체', order_id=order_id)


def evidence(query=QUERY):
    return dict(status='unique', verified=True, provider=SOURCE, query=query,
                aliases=['서울특별시 강남구 테헤란로 10', '서울 강남구 역삼동 123'],
                canonical_road_address='서울특별시 강남구 테헤란로 10',
                canonical_building_name='가상센터')


class DuplicateReviewTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.addCleanup(self.c.close)
        review.setup(self.c)
        self.first = review.snapshot_fingerprint(snapshot())
        self.second = review.snapshot_fingerprint(snapshot(7, 'immutable-fixture-2'))

    def decide(self, **kwargs):
        return review.record_decision(self.c, 'staff-a', self.first, self.second,
                                      reason='서로 다른 실제 주문임을 담당자가 확인', now=100, **kwargs)

    def cache(self, **kwargs):
        return review.put_location_evidence(self.c, QUERY, SOURCE, VERSION, evidence(), now=100, **kwargs)

    def test_decision_is_staff_specific_symmetric_and_not_an_order_mutation(self):
        self.decide()
        self.assertTrue(review.is_suppressed(self.c, 'staff-a', self.second, self.first, now=101))
        self.assertFalse(review.is_suppressed(self.c, 'staff-b', self.first, self.second, now=101))
        record = review.get_active_decision(self.c, 'staff-a', self.first, self.second, now=101)
        self.assertIn('담당자가 확인', record['reason'])
        tables = {row[0] for row in self.c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'duplicate_review_decisions', 'duplicate_location_evidence'})

    def test_missing_reason_or_invalid_pair_cannot_hide_warning(self):
        for reason in ('', '   ', None, False, 'x' * 501, '한 줄\n숨겨진 지시'):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                review.record_decision(self.c, 'staff-a', self.first, self.second, reason=reason, now=100)
        for pair in ((self.first, self.first), ('row5', self.second), (None, self.second)):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                review.record_decision(self.c, 'staff-a', *pair, reason='다른 주문', now=100)
        self.assertFalse(review.is_suppressed(self.c, 'staff-a', self.first, self.second, now=101))

    def test_expiry_boundary_and_clock_rollback_never_return_stale_decision(self):
        self.decide(ttl_seconds=10)
        self.assertTrue(review.is_suppressed(self.c, 'staff-a', self.first, self.second, now=109.999))
        self.assertFalse(review.is_suppressed(self.c, 'staff-a', self.first, self.second, now=110))
        self.decide(ttl_seconds=10)
        self.assertFalse(review.is_suppressed(self.c, 'staff-a', self.first, self.second, now=99))

    def test_row_reuse_or_any_changed_condition_invalidates_exact_pair(self):
        self.decide()
        changes = dict(order_id='new-order-in-same-row', row_number=6, delivery_date=46370,
                       event_time=.5 + 1 / 86400, location=QUERY + ' 본관', sender_summary='다른 업체')
        for key, value in changes.items():
            changed = snapshot()
            changed[key] = value
            fingerprint = review.snapshot_fingerprint(changed)
            with self.subTest(key=key):
                self.assertNotEqual(fingerprint, self.first)
                self.assertFalse(review.is_suppressed(self.c, 'staff-a', fingerprint, self.second, now=101))

    def test_row_revision_changes_invalidate_identical_visible_values(self):
        old = snapshot()
        old.pop('order_id')
        old['row_revision'] = 'actual-source-generation-1'
        new = {**old, 'row_revision': 'actual-source-generation-2'}
        self.assertNotEqual(review.snapshot_fingerprint(old), review.snapshot_fingerprint(new))

    def test_row_number_and_contents_without_actual_identity_fail_closed(self):
        old = snapshot()
        old.pop('order_id')
        for extra in ({}, {'order_id': ''}, {'row_revision': ''}, {'order_id': False}, {'order_id': '미확인'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                review.snapshot_fingerprint({**old, **extra})
        for key, value in (('location', ''), ('sender_summary', '#REF!'),
                           ('sender_summary', '확인 필요'), ('location', '미정'), ('event_time', '?'),
                           ('event_time', None), ('delivery_date', float('nan')),
                           ('event_time', True), ('row_number', True)):
            invalid = snapshot()
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                review.snapshot_fingerprint(invalid)

    def test_cache_only_returns_fresh_actual_query_bound_source_version_evidence(self):
        self.assertTrue(self.cache(ttl_seconds=10))
        self.assertEqual(review.get_location_evidence(self.c, QUERY, SOURCE, VERSION, now=109), evidence())
        for query, source, version in ((QUERY + ' ', SOURCE, VERSION), (QUERY, 'other-provider', VERSION),
                                       (QUERY, SOURCE, 'new-response-contract')):
            with self.subTest(query=query, source=source, version=version):
                result = review.get_location_evidence(self.c, query, source, version, now=101)
                self.assertEqual(result['status'], 'needs_review')
        expired = review.get_location_evidence(self.c, QUERY, SOURCE, VERSION, now=110)
        self.assertEqual(expired['status'], 'needs_review')
        self.assertNotIn('verified', expired)

    def test_failed_unverified_or_ambiguous_refresh_removes_previous_success(self):
        for invalid in (None, {}, {'status': 'failed'}, evidence('unrelated-query'),
                        {**evidence(), 'status': 'ambiguous'}, {**evidence(), 'verified': False},
                        {**evidence(), 'verified': 1}, {**evidence(), 'provider': 'other-provider'},
                        {**evidence(), 'aliases': []}, {**evidence(), 'canonical_road_address': None}):
            with self.subTest(invalid=invalid):
                self.cache()
                self.assertFalse(review.put_location_evidence(self.c, QUERY, SOURCE, VERSION, invalid, now=101))
                outcome = review.get_location_evidence(self.c, QUERY, SOURCE, VERSION, now=102)
                self.assertEqual(outcome['status'], 'needs_review')
                self.assertNotIn('verified', outcome)

    def test_coordinate_only_cache_entry_is_not_location_identity(self):
        result = dict(status='unique', verified=True, provider=SOURCE, query=QUERY,
                      aliases=[QUERY], x=127.0, y=37.5)
        self.assertFalse(review.put_location_evidence(self.c, QUERY, SOURCE, VERSION, result, now=100))
        self.assertEqual(review.get_location_evidence(self.c, QUERY, SOURCE, VERSION, now=101)['status'], 'needs_review')

    def test_cache_does_not_persist_raw_query_or_arbitrary_provider_fields(self):
        result = {**evidence(), 'customer_phone': 'fixture-private', 'debug_request': QUERY}
        review.put_location_evidence(self.c, QUERY, SOURCE, VERSION, result, now=100)
        record = self.c.execute('SELECT query_hash,evidence FROM duplicate_location_evidence').fetchone()
        self.assertNotEqual(record[0], QUERY)
        self.assertEqual(len(record[0]), 64)
        self.assertNotIn(QUERY, record[1])
        self.assertNotIn('fixture-private', record[1])
        self.assertNotIn('query', json.loads(record[1]))
        self.assertEqual(review.get_location_evidence(self.c, QUERY, SOURCE, VERSION, now=101), evidence())

    def test_invalidation_and_corrupt_cache_are_explicit_review_not_empty_matches(self):
        self.cache()
        self.assertTrue(review.invalidate_location_evidence(self.c, QUERY, SOURCE, VERSION))
        self.assertEqual(review.get_location_evidence(self.c, QUERY, SOURCE, VERSION, now=101)['status'], 'needs_review')
        self.cache()
        self.c.execute("UPDATE duplicate_location_evidence SET evidence='not json'")
        self.assertEqual(review.get_location_evidence(self.c, QUERY, SOURCE, VERSION, now=101)['status'], 'needs_review')
        self.cache()
        self.assertEqual(review.get_location_evidence(self.c, QUERY, SOURCE, VERSION, now=99)['status'], 'needs_review')

    def test_invalid_ttl_clock_and_owner_do_not_change_records(self):
        for ttl in (0, -1, float('inf'), True, 30 * 86400 + 1):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.decide(ttl_seconds=ttl)
        for ttl in (0, -1, float('nan'), True, 86401):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.cache(ttl_seconds=ttl)
        for now in (True, -1, float('nan'), '100'):
            with self.subTest(now=now), self.assertRaises(ValueError):
                review.get_location_evidence(self.c, QUERY, SOURCE, VERSION, now=now)
        with self.assertRaises(ValueError):
            review.record_decision(self.c, '', self.first, self.second, reason='다른 주문', now=100)

    def test_repeated_local_decision_does_not_duplicate_and_input_remains_unchanged(self):
        original = snapshot()
        before = copy.deepcopy(original)
        review.snapshot_fingerprint(original)
        self.assertEqual(before, original)
        self.decide()
        self.decide()
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM duplicate_review_decisions').fetchone()[0], 1)

    def test_put_purges_unvisited_expired_and_future_clock_evidence_but_keeps_fresh(self):
        # Seed simultaneous stored observations to model a clock rollback.
        for suffix, stamp, ttl in ((' expired', 100, 10), (' fresh', 100, 50), (' future', 200, 50)):
            key = review._cache_key(QUERY + suffix, SOURCE, VERSION)
            payload = {k: v for k, v in evidence().items() if k != 'query'}
            self.c.execute('INSERT INTO duplicate_location_evidence VALUES (?,?,?,?,?,?)',
                           (*key, json.dumps(payload), stamp, stamp + ttl))
        self.decide(ttl_seconds=1000)
        self.cache(ttl_seconds=100)
        # now=110 is an expiry boundary; never-revisited expired query is purged.
        query = QUERY + ' newest'
        review.put_location_evidence(self.c, query, SOURCE, VERSION, evidence(query), now=110)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM duplicate_location_evidence').fetchone()[0], 3)
        self.assertEqual(review.get_location_evidence(self.c, QUERY + ' expired', SOURCE, VERSION, now=110)['status'], 'needs_review')
        self.assertEqual(review.get_location_evidence(self.c, QUERY + ' future', SOURCE, VERSION, now=110)['status'], 'needs_review')
        self.assertEqual(review.get_location_evidence(self.c, QUERY + ' fresh', SOURCE, VERSION, now=110)['status'], 'unique')
        self.assertTrue(review.is_suppressed(self.c, 'staff-a', self.first, self.second, now=110))

    def test_cache_entry_limit_evicts_oldest_without_stale_success_or_staff_decision_loss(self):
        self.decide(ttl_seconds=1000)
        with patch.object(review, 'CACHE_MAX_ENTRIES', 2):
            for suffix, stamp in ((' oldest', 100), (' middle', 101), (' newest', 102)):
                query = QUERY + suffix
                self.assertTrue(review.put_location_evidence(self.c, query, SOURCE, VERSION, evidence(query), now=stamp))
            self.assertEqual(self.c.execute('SELECT COUNT(*) FROM duplicate_location_evidence').fetchone()[0], 2)
            oldest = review.get_location_evidence(self.c, QUERY + ' oldest', SOURCE, VERSION, now=103)
            self.assertEqual(oldest['status'], 'needs_review')
            self.assertNotIn('verified', oldest)
            for suffix in (' middle', ' newest'):
                self.assertEqual(review.get_location_evidence(self.c, QUERY + suffix, SOURCE, VERSION, now=103)['status'], 'unique')
            self.assertTrue(review.is_suppressed(self.c, 'staff-a', self.first, self.second, now=103))

    def test_failed_refresh_still_cleans_unrelated_expired_provider_evidence(self):
        self.cache(ttl_seconds=1)
        query = QUERY + ' failed fresh request'
        self.assertFalse(review.put_location_evidence(self.c, query, SOURCE, VERSION, {'status': 'failed'}, now=101))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM duplicate_location_evidence').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
