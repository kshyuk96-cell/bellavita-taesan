import copy
import sqlite3
import unittest
from unittest.mock import Mock, patch

import failure_regressions
import order_locations
import read_resilience
import regression_runners
from staff_read_guard import guarded_read


class StaffReadGuardTests(unittest.TestCase):
    def test_precheck_denial_does_not_read_and_static_error_hides_auth_exception(self):
        read = Mock()
        for authorized in (lambda: None, lambda: False, Mock(side_effect=RuntimeError('private key'))):
            with self.subTest(authorized=authorized), self.assertRaises(PermissionError) as error:
                guarded_read(read, authorized=authorized)
            self.assertNotIn('private', str(error.exception))
        read.assert_not_called()

    def test_actual_read_and_fresh_postcheck_return_result_only_if_authorized(self):
        read = Mock(return_value=['synthetic'])
        authorize = Mock(side_effect=['staff', 'staff'])
        self.assertEqual(guarded_read(read, authorized=authorize), ['synthetic'])
        self.assertEqual(authorize.call_count, 2)
        read.assert_called_once_with()
        authorize.side_effect = ['staff', None]
        with self.assertRaises(PermissionError):
            guarded_read(read, authorized=authorize)
        self.assertEqual(read.call_count, 2)

    def test_read_error_stays_error_not_fabricated_empty_success(self):
        with self.assertRaises(TimeoutError):
            guarded_read(Mock(side_effect=TimeoutError()), authorized=lambda: 'staff')
        for read, authorize in ((None, lambda: True), (lambda: [], None)):
            with self.assertRaises(ValueError):
                guarded_read(read, authorized=authorize)


class RegressionRunnerTests(unittest.TestCase):
    def case(self, fixture_id):
        return {'case_id': fixture_id, **copy.deepcopy(failure_regressions.DEFAULT_CASES[fixture_id])}

    def test_all_four_catalog_cases_call_actual_local_code_and_pass(self):
        c = sqlite3.connect(':memory:')
        self.addCleanup(c.close)
        failure_regressions.setup(c)
        failure_regressions.seed_defaults(c)
        with patch.object(order_locations, 'compare_locations', wraps=order_locations.compare_locations) as locations, \
             patch.object(read_resilience, 'classify_read_error', wraps=read_resilience.classify_read_error) as classifier, \
             patch.object(regression_runners.staff_read_guard, 'guarded_read', wraps=guarded_read) as guard:
            result = failure_regressions.replay_cases(c, regression_runners.build_runners())
        self.assertEqual([result[k] for k in ('passed', 'failed', 'errors', 'unsupported')], [4, 0, 0, 0])
        self.assertEqual(locations.call_count, 2)
        self.assertEqual(classifier.call_count, 1)
        self.assertEqual(guard.call_count, 1)

    def test_expected_value_changes_do_not_change_actual_outcome(self):
        runners = regression_runners.build_runners()
        cases = [('basement-F-vs-above', 'same', 'different'),
                 ('hall-suffix-review', 'same', 'needs_review'),
                 ('permission-after-revocation', 'unavailable', 'permission_denied'),
                 ('timeout-is-not-zero', 'permission_denied', 'unavailable')]
        for fixture_id, misleading_expected, actual in cases:
            case = self.case(fixture_id)
            case['expected'] = misleading_expected
            with self.subTest(fixture_id=fixture_id):
                self.assertEqual(runners[fixture_id](case), actual)

    def test_changed_real_function_outcome_fails_regression(self):
        c = sqlite3.connect(':memory:')
        self.addCleanup(c.close)
        failure_regressions.setup(c)
        failure_regressions.seed_defaults(c)
        with patch.object(order_locations, 'compare_locations', return_value={'status': 'same'}):
            result = failure_regressions.replay_cases(c, regression_runners.build_runners())
        self.assertEqual(result['passed'], 2)
        self.assertEqual(result['failed'], 2)

    def test_unsupported_or_private_parameter_cases_are_rejected(self):
        runners = regression_runners.build_runners()
        malformed = []
        one = self.case('timeout-is-not-zero'); one['parameters']['failure'] = 'network'; malformed.append(one)
        one = self.case('permission-after-revocation'); one['parameters']['failure'] = 'timeout'; malformed.append(one)
        one = self.case('hall-suffix-review'); one['parameters']['address'] = 'private'; malformed.append(one)
        one = self.case('basement-F-vs-above'); one['category'] = 'intent'; malformed.append(one)
        one = self.case('basement-F-vs-above'); one['parameters']['left_floor'] = 0; malformed.append(one)
        for case in malformed:
            with self.subTest(case=case), self.assertRaises(ValueError):
                runners[case['case_id']](case)
        self.assertNotIn('unknown-fixture', runners)
