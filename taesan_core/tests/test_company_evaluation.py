import unittest
from taesan_core import CoreError
from taesan_core.company_evaluation import run_fixture_evaluation


class CompanyEvaluationTests(unittest.TestCase):
    def test_both_companies_have_100_observed_business_cases(self):
        for tenant in ('hope-sunrise','bellavita'):
            with self.subTest(tenant=tenant):
                result=run_fixture_evaluation(tenant)
                self.assertFalse(result['model_called'])
                self.assertFalse(result['operational_connection'])
                report=result['report']
                self.assertEqual(report['observed'],100)
                self.assertEqual(report['passed'],100)
                self.assertEqual(report['score'],100)
                self.assertTrue(report['target_met'])
                self.assertFalse(report['operational_score_verified'])
                self.assertEqual(report['environment'],'fixture')
                for area in report['areas'].values():
                    self.assertEqual(area['total'],25)
                    self.assertEqual(area['passed'],25)

    def test_unknown_company_does_not_get_default_configuration(self):
        with self.assertRaises(CoreError):run_fixture_evaluation('unregistered-company')
