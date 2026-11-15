import unittest
from taesan_core import CoreError
from taesan_core.evaluation import AREAS, Case, Observation, evaluate


class EvaluationTests(unittest.TestCase):
    def cases(self):
        return [Case(f'{area}-{i}', area, 'business observation') for area in AREAS for i in range(25)]

    def test_no_observer_no_score(self):
        self.assertIsNone(evaluate(self.cases())['score'])

    def test_fixture_never_operational_score(self):
        result = evaluate(self.cases(), lambda c: Observation(True, c.id))
        self.assertEqual(result['score'], 100)
        self.assertTrue(result['target_met'])
        self.assertFalse(result['operational_score_verified'])

    def test_missing_observation_is_incomplete(self):
        result = evaluate(self.cases(), lambda c: None if c.id == 'judgment-0' else Observation(True, c.id))
        self.assertEqual(result['observed'], 99)
        self.assertIsNone(result['score'])
        self.assertFalse(result['target_met'])

    def test_area_floor_even_with_90_total(self):
        result = evaluate(self.cases(), lambda c: Observation(not (c.area == 'judgment' and int(c.id.rsplit('-', 1)[1]) < 4), c.id))
        self.assertEqual(result['score'], 96)
        self.assertFalse(result['target_met'])

    def test_critical_failure_even_at_100(self):
        for name in ('unauthorized_change', 'unsupported_success'):
            with self.subTest(name=name):
                result = evaluate(self.cases(), lambda c: Observation(True, c.id, **{name: c.id == 'judgment-0'}))
                self.assertFalse(result['target_met'])

    def test_tiny_sample_cannot_claim_target(self):
        result = evaluate(self.cases()[:1], lambda c: Observation(True, c.id), environment='live')
        self.assertFalse(result['target_met'])
        self.assertFalse(result['operational_score_verified'])

    def test_duplicates_rejected(self):
        with self.assertRaises(CoreError):
            evaluate([self.cases()[0]] * 100)

    def test_observer_exception_cannot_be_success(self):
        def failed(case):
            raise RuntimeError('disconnected')
        self.assertIsNone(evaluate(self.cases(), failed)['score'])

    def test_unknown_environment_rejected(self):
        with self.assertRaises(CoreError):
            evaluate([], environment='claimed-real')
