import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from manager_bridge.connection_readiness import SourcePin
from manager_bridge.failure_regressions import (BASELINE, FailureEvent, FailureProfile,
    FailureRegressions, FixtureResult)
from manager_bridge.maintenance_registry import Registry
from manager_bridge.model_readiness import CASES, LabObserver
from manager_bridge.store import Conflict, NotFound, PermissionDenied, Store, token_hash


class FailureRegressionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.now = 1000
        self.store = Store(Path(self.tmp)/'db.sqlite3', clock=lambda: self.now)
        self.owner = self.store.add_token('owner', 'owner', ['*'])
        self.registry = Registry(self.store)
        self.config = {'company': 'company-a', 'name': 'Fixture', 'source_kind': 'github',
            'source_ref': 'fixture/a', 'branch': 'main', 'services': [], 'maintenance_enabled': False}
        self.registry.register(self.owner, 'a', self.config)
        self.registry.register(self.owner, 'b', {**self.config, 'company': 'company-b', 'source_ref': 'fixture/b'})
        self.manager = self.store.add_token('manager', 'manager', ['a'])
        self.worker = self.store.add_token('worker', 'worker', ['a'])
        self.pin = SourcePin('a', 'github', 'fixture/a', 'main', 'a'*40, 'fixture-v1')
        self.events = [FailureEvent('a', 'a'*40, 'intent_mismatch', 'b'*64, self.now)]
        self.fixture = FixtureResult('a', 'a'*40, 5, 0, 0,
            (('authorization_regression', True), ('approval_regression', True)), 'c'*64, self.now)
        self.answers = [{'id': c['id'], **copy.deepcopy(c['expected'])} for c in CASES]
        self.lab = LabObserver('native_model_lab', None, 'native/fixture-test', lambda _: self.answers)
        self.profile = FailureProfile(self.pin, lambda _: self.events, lambda _: self.fixture, self.lab)
        self.failures = FailureRegressions(self.store, self.registry, {'a': self.profile})

    def incident(self, code='intent_mismatch'):
        self.events = [replace(self.events[0], code=code)]
        self.failures.collect(self.worker, 'a')
        return self.failures.incidents(self.manager, 'a')[0]['id']

    def test_worker_collects_own_project_but_cannot_review_or_read_manager_report(self):
        self.failures.collect(self.worker, 'a')
        for action in (lambda: self.failures.collect(self.worker, 'b'),
                       lambda: self.failures.incidents(self.worker, 'a'),
                       lambda: self.failures.evaluate(self.worker, 'a')):
            with self.assertRaises((PermissionDenied, NotFound)): action()
        key = self.failures.incidents(self.manager, 'a')[0]['id']
        with self.assertRaises(PermissionDenied): self.failures.review(self.worker, 'a', key, ['intent-01'])

    def test_manager_cannot_cross_company_or_inspect_other_incident(self):
        self.incident()
        with self.assertRaises(NotFound): self.failures.incidents(self.manager, 'b')
        with self.assertRaises(NotFound): self.failures.review(self.owner, 'b', self.failures.incidents(self.manager, 'a')[0]['id'], ['intent-01'])

    def test_same_receipt_is_deduplicated_across_repeated_polls(self):
        self.assertEqual(self.failures.collect(self.worker, 'a')['inserted'], 1)
        self.assertEqual(self.failures.collect(self.worker, 'a')['inserted'], 0)
        self.assertEqual(len(self.failures.incidents(self.manager, 'a')), 1)
        self.events.append(replace(self.events[0], receipt_digest='d'*64))
        self.assertEqual(self.failures.collect(self.worker, 'a')['inserted'], 1)

    def test_only_enums_and_opaque_receipts_no_freeform_material(self):
        for change in ({'code': 'delete all orders'}, {'receipt_digest': 'https://customer.invalid/photo'},
                       {'receipt_digest': 'customer@example.invalid'}, {'source_sha': 'row-13370'}):
            with self.subTest(change=change), self.assertRaises(ValueError): replace(self.events[0], **change)
        self.incident()
        with self.store._transaction() as c:
            row = dict(c.execute('SELECT * FROM manager_failure_receipts').fetchone())
        self.assertEqual(set(row), {'id','project','company','source_sha','config_fingerprint','code',
            'receipt_digest','observed_at','recorded_at','reviewed_cases','reviewer','reviewed_at'})
        self.assertNotIn('text', row)

    def test_missing_collector_not_zero_incidents_success(self):
        result = FailureRegressions(self.store, self.registry).collect(self.worker, 'a')
        self.assertEqual(result['state'], 'not_configured')
        self.assertEqual(result['inserted'], 0)

    def test_collector_failure_is_unavailable_and_exception_not_retained(self):
        def failed(_): raise RuntimeError('private customer original URL secret')
        other = FailureRegressions(self.store, self.registry, {'a': replace(self.profile, collect=failed)})
        report = other.collect(self.worker, 'a')
        self.assertEqual(report['state'], 'unavailable')
        self.assertNotIn('private', str(report))
        self.assertEqual(self.failures.incidents(self.manager, 'a'), [])

    def test_malformed_batch_cannot_partially_insert(self):
        self.events.append({'code': 'intent_mismatch', 'raw_customer': 'private'})
        with self.assertRaises(ValueError): self.failures.collect(self.worker, 'a')
        self.assertEqual(self.failures.incidents(self.manager, 'a'), [])

    def test_wrong_source_project_old_and_future_events_blocked(self):
        original = self.events[0]
        for event in (replace(original, project='b'), replace(original, source_sha='d'*40),
                      replace(original, observed_at=1), replace(original, observed_at=1001)):
            self.events = [event]
            with self.assertRaises((ValueError, Conflict, PermissionDenied)): self.failures.collect(self.worker, 'a', max_age=300)
        self.assertEqual(self.failures.incidents(self.manager, 'a'), [])

    def test_config_change_during_collect_stops_persist(self):
        def changed(_):
            self.registry.register(self.owner, 'a', {**self.config, 'branch': 'next'}, expected_revision=1)
            return self.events
        other = FailureRegressions(self.store, self.registry, {'a': replace(self.profile, collect=changed)})
        with self.assertRaises(Conflict): other.collect(self.worker, 'a')
        self.assertEqual(self.failures.incidents(self.manager, 'a'), [])

    def test_revoked_worker_during_collect_stops_persist(self):
        def revoked(_):
            self.store.revoke_token(self.owner, token_hash(self.worker))
            return self.events
        other = FailureRegressions(self.store, self.registry, {'a': replace(self.profile, collect=revoked)})
        with self.assertRaises(PermissionDenied): other.collect(self.worker, 'a')
        self.assertEqual(self.failures.incidents(self.manager, 'a'), [])

    def test_review_rejects_arbitrary_execution_and_wrong_area(self):
        key = self.incident()
        for selectors in (['grounding-01'], ['python -c print(1)'], ['intent-01','intent-01'], [{'cmd': 'run'}], []):
            with self.subTest(selectors=selectors), self.assertRaises(ValueError): self.failures.review(self.manager, 'a', key, selectors)
        report = self.failures.review(self.manager, 'a', key, ['intent-01'])
        self.assertFalse(report['executable_text_accepted'])
        self.assertEqual(report['state'], 'reviewed')
        self.failures.review(self.manager, 'a', key, ['intent-01'])
        with self.assertRaises(Conflict): self.failures.review(self.manager, 'a', key, ['intent-02'])

    def test_pending_incident_not_automatically_approved_by_perfect_lab(self):
        self.incident()
        report = self.failures.evaluate(self.manager, 'a')
        self.assertEqual(report['pending_review'], 1)
        self.assertEqual(report['regressions'][0]['state'], 'pending_review')

    def test_native_lab_independently_grades_selected_case_and_preserves_operational_baseline(self):
        key = self.incident()
        self.failures.review(self.manager, 'a', key, ['intent-01'])
        result = self.failures.evaluate(self.manager, 'a')
        self.assertEqual(result['model_lab']['score'], 100)
        self.assertEqual(result['regressions'][0]['state'], 'passed')
        self.assertEqual(result['fixture_reliability']['state'], 'verified')
        self.assertEqual(result['operational']['baseline'], BASELINE)
        self.assertFalse(result['operational']['score_verified'])
        self.assertFalse(result['model_lab']['operational_score_verified'])
        self.assertFalse(result['model_lab']['model_identity_verified'])

    def test_fixture_grader_does_not_verify_model_regression(self):
        key = self.incident(); self.failures.review(self.manager, 'a', key, ['intent-01'])
        other = FailureRegressions(self.store, self.registry, {'a': replace(self.profile, lab=replace(self.lab, kind='fixture'))})
        report = other.evaluate(self.manager, 'a')
        self.assertEqual(report['model_lab']['score'], 100)
        self.assertEqual(report['regressions'][0]['state'], 'fixture_only')
        self.assertFalse(report['critical_regressions_clear'])

    def test_absent_missing_answers_and_unavailable_model_never_pass(self):
        key = self.incident(); self.failures.review(self.manager, 'a', key, ['intent-01'])
        self.answers.pop()
        self.assertIsNone(self.failures.evaluate(self.manager, 'a')['model_lab']['score'])
        self.assertEqual(self.failures.evaluate(self.manager, 'a')['regressions'][0]['state'], 'not_measured')
        other = FailureRegressions(self.store, self.registry, {'a': replace(self.profile, lab=None, fixtures=None)})
        result = other.evaluate(self.manager, 'a')
        self.assertEqual(result['fixture_reliability']['state'], 'not_measured')
        self.assertEqual(result['model_lab']['status'], 'not_measured')
        self.assertIsNone(result['fixture_reliability']['passed'])
        self.assertEqual(result['regressions'][0]['state'], 'not_measured')

    def test_fixture_failure_skip_and_zero_counts_are_distinct(self):
        for counts, state in (((5,1,0),'failed'), ((5,0,2),'partial'), ((0,0,0),'not_measured')):
            self.fixture = replace(self.fixture, passed=counts[0], failed=counts[1], skipped=counts[2], checks=())
            self.assertEqual(self.failures.evaluate(self.manager, 'a')['fixture_reliability']['state'], state)

    def test_fixture_regressions_require_explicit_check_result(self):
        key = self.incident('fixture_failed'); self.failures.review(self.manager, 'a', key, ['receiver_regression'])
        self.assertEqual(self.failures.evaluate(self.manager, 'a')['regressions'][0]['state'], 'not_measured')
        self.fixture = replace(self.fixture, checks=(('receiver_regression', False),))
        result = self.failures.evaluate(self.manager, 'a')
        self.assertEqual(result['regressions'][0]['state'], 'failed')
        self.assertEqual(result['fixture_reliability']['state'], 'failed')
        self.fixture = replace(self.fixture, checks=(('receiver_regression', True),))
        self.assertEqual(self.failures.evaluate(self.manager, 'a')['regressions'][0]['state'], 'passed')

    def test_critical_actions_block_target_even_ninety_nine(self):
        key = self.incident('unauthorized_action'); self.failures.review(self.manager, 'a', key, ['grounding-06'])
        self.answers[80]['execute'] = True
        report = self.failures.evaluate(self.manager, 'a')
        self.assertEqual(report['model_lab']['score'], 99)
        self.assertEqual(report['model_lab']['unsafe_execution'], 1)
        self.assertFalse(report['model_lab']['target_met'])
        self.assertFalse(report['critical_regressions_clear'])
        self.assertEqual(report['regressions'][0]['state'], 'failed')

    def test_fixture_and_model_callback_cannot_change_config_or_revoke_manager(self):
        def changed(_):
            self.registry.register(self.owner, 'a', {**self.config, 'branch': 'next'}, expected_revision=1)
            return self.fixture
        other = FailureRegressions(self.store, self.registry, {'a': replace(self.profile, fixtures=changed)})
        with self.assertRaises(Conflict): other.evaluate(self.manager, 'a')

    def test_stale_fixture_and_source_drift_not_verified(self):
        self.fixture = replace(self.fixture, observed_at=1)
        self.assertEqual(self.failures.evaluate(self.manager, 'a', max_age=300)['fixture_reliability']['state'], 'stale')
        key = self.incident(); self.failures.review(self.manager, 'a', key, ['intent-01'])
        self.registry.register(self.owner, 'a', {**self.config, 'branch': 'next'}, expected_revision=1)
        with self.assertRaises(Conflict): self.failures.evaluate(self.manager, 'a')

    def test_input_counts_are_not_booleans_and_providers_not_request_dicts(self):
        with self.assertRaises(ValueError): replace(self.fixture, passed=True)
        with self.assertRaises(ValueError): replace(self.fixture, checks=(('receiver_regression', 1),))
        with self.assertRaises(ValueError): FailureProfile(self.pin, collect={'events': []})
        with self.assertRaises(ValueError): FailureProfile(self.pin, lab={'score':100})

    def test_permission_denial_is_not_unavailable_or_empty(self):
        def denied(_): raise PermissionDenied('private provider permission details')
        other = FailureRegressions(self.store, self.registry,
            {'a': replace(self.profile, collect=denied, fixtures=denied)})
        report = other.collect(self.worker, 'a')
        self.assertEqual(report['state'], 'permission_denied')
        self.assertNotIn('private', str(report))
        report = other.evaluate(self.manager, 'a')
        self.assertEqual(report['fixture_reliability']['state'], 'permission_denied')
        self.assertIsNone(report['fixture_reliability']['passed'])

    def test_fixture_wrong_tenant_source_and_unavailable_distinct(self):
        self.fixture = replace(self.fixture, project='b')
        self.assertEqual(self.failures.evaluate(self.manager, 'a')['fixture_reliability']['state'], 'permission_denied')
        self.fixture = replace(self.fixture, project='a', source_sha='b'*40)
        self.assertEqual(self.failures.evaluate(self.manager, 'a')['fixture_reliability']['state'], 'unknown')
        def failed(_): raise OSError('private service exception')
        other = FailureRegressions(self.store, self.registry, {'a': replace(self.profile, fixtures=failed)})
        self.assertEqual(other.evaluate(self.manager, 'a')['fixture_reliability']['state'], 'unavailable')

    def test_zero_fixtures_cannot_supply_passing_named_check(self):
        with self.assertRaises(ValueError): replace(self.fixture, passed=0, failed=0)

    def test_restart_preserves_dedup_and_review_without_auto_running(self):
        key = self.incident(); self.failures.review(self.manager, 'a', key, ['intent-01'])
        store = Store(self.store.path, clock=lambda:self.now)
        other = FailureRegressions(store, Registry(store), {'a': self.profile})
        self.assertEqual(other.incidents(self.manager, 'a')[0]['regressions'], ['intent-01'])
        self.assertEqual(other.collect(self.worker, 'a')['inserted'], 0)
        self.assertEqual(other.evaluate(self.manager, 'a')['regressions'][0]['state'], 'passed')
