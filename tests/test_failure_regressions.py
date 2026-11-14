import copy
import sqlite3
import unittest

import failure_regressions as regressions


class FailureRegressionTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.addCleanup(self.c.close)
        regressions.setup(self.c)

    def test_actual_safe_observations_aggregate_without_source_payload(self):
        regressions.record_observation(self.c, 'timeout', 'order_duplicate', now=20)
        regressions.record_observation(self.c, 'timeout', 'order_duplicate', now=10)
        regressions.record_observation(self.c, 'permission', 'photo', now=30)
        rows = regressions.observations(self.c)
        self.assertEqual(rows[1], dict(error_code='timeout', task_kind='order_duplicate', occurrences=2,
                                       first_seen=10, last_seen=20))
        self.assertEqual(set(rows[0]), {'error_code', 'task_kind', 'occurrences', 'first_seen', 'last_seen'})

    def test_unknown_or_private_observation_values_and_times_are_rejected(self):
        for args in [('private https://secret', 'photo'), ('timeout', 'employee-123'),
                     ({'message': 'private'}, 'photo'), ('timeout', None)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                regressions.record_observation(self.c, *args)
        for stamp in (True, -1, float('nan'), float('inf'), '2026'):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                regressions.record_observation(self.c, 'timeout', 'photo', now=stamp)
        self.assertEqual(regressions.observations(self.c), [])

    def test_reviewed_synthetic_cases_deduplicate_by_safe_content(self):
        case = dict(category='context', parameters=dict(date_offset=1, center='guro', status='pending',
                    elapsed_seconds=10, same_owner=True), expected='preserved')
        original = copy.deepcopy(case)
        first = regressions.register_case(self.c, case)
        self.assertEqual(regressions.register_case(self.c, case), first)
        case['parameters']['date_offset'] = 2
        self.assertNotEqual(regressions.register_case(self.c, case), first)
        self.assertEqual(regressions.cases(self.c)[0]['parameters']['date_offset'], original['parameters']['date_offset'])
        self.assertEqual(len(regressions.cases(self.c)), 2)

    def test_source_fields_free_text_and_malformed_parameters_are_rejected(self):
        base = dict(category='grounding', parameters={'failure': 'timeout'}, expected='unavailable')
        invalid = [{**base, 'message': 'customer'}, {**base, 'fixture_id': 'private'},
                   {**base, 'parameters': {'failure': 'timeout', 'url': 'https://private'}},
                   {**base, 'parameters': {'failure': 'customer name'}},
                   {**base, 'parameters': {'failure': []}}, {**base, 'expected': 'private address'},
                   {**base, 'category': 'messages'}, None, [],
                   dict(category='context', parameters=dict(date_offset=True, center='guro', status='pending',
                        elapsed_seconds=10, same_owner=True), expected='preserved')]
        for case in invalid:
            with self.subTest(case=case), self.assertRaises(ValueError):
                regressions.register_case(self.c, case)
        self.assertEqual(regressions.cases(self.c), [])

    def test_default_catalog_is_repeatable_and_does_not_claim_a_test_pass(self):
        regressions.seed_defaults(self.c)
        regressions.seed_defaults(self.c)
        self.assertEqual({case['case_id'] for case in regressions.cases(self.c)}, set(regressions.DEFAULT_CASES))
        self.assertIn('아직 실행하지', regressions.summary(self.c))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM synthetic_replays').fetchone()[0], 0)

    def test_replay_distinguishes_actual_pass_fail_error_and_unsupported(self):
        regressions.seed_defaults(self.c)
        def raises(_):
            raise RuntimeError('private customer https://secret.example')
        result = regressions.replay_cases(self.c, {
            'basement-F-vs-above': lambda _: 'different',
            'hall-suffix-review': lambda _: 'same',
            'permission-after-revocation': raises,
        }, now=100)
        self.assertEqual([result[k] for k in ('passed', 'failed', 'errors', 'unsupported')], [1, 1, 1, 1])
        self.assertEqual(len(result['outcomes']), 4)
        stored = str(self.c.execute('SELECT * FROM synthetic_replays').fetchall())
        self.assertNotIn('private', stored)
        self.assertNotIn('secret.example', stored)
        self.assertIn('미연결 1', regressions.summary(self.c))
        self.assertIn('실제 운영 점수', regressions.summary(self.c))

    def test_missing_runner_and_empty_registry_never_manufacture_a_pass(self):
        empty = regressions.replay_cases(self.c, {}, now=100)
        self.assertEqual(empty['passed'], 0)
        regressions.seed_defaults(self.c)
        unsupported = regressions.replay_cases(self.c, {}, now=101)
        self.assertEqual(unsupported['unsupported'], 4)
        self.assertEqual(unsupported['passed'], 0)

    def test_runner_mutation_does_not_change_registry_or_expectations(self):
        regressions.seed_defaults(self.c)
        def mutate(case):
            case['parameters']['left_floor'] = 99
            case['expected'] = 'same'
            return 'same'
        result = regressions.replay_cases(self.c, {'basement-F-vs-above': mutate})
        self.assertEqual(result['failed'], 1)
        saved = next(case for case in regressions.cases(self.c) if case['case_id'] == 'basement-F-vs-above')
        self.assertEqual(saved['parameters']['left_floor'], -3)
        self.assertEqual(saved['expected'], 'different')

    def test_intent_and_location_synthetic_parameter_bounds(self):
        for case in (
            dict(category='intent', parameters={'requested_action': 'change', 'authorized': False}, expected='permission_denied'),
            copy.deepcopy(regressions.DEFAULT_CASES['hall-suffix-review']),
        ):
            self.assertTrue(regressions.register_case(self.c, case).startswith('case-'))
        bad = copy.deepcopy(regressions.DEFAULT_CASES['hall-suffix-review'])
        bad['parameters']['left_floor'] = 0
        with self.assertRaises(ValueError):
            regressions.register_case(self.c, bad)
