import copy
import unittest
from manager_bridge.qa_admission import admit_qa_report
from manager_bridge.store import digest


class QAAdmissionTests(unittest.TestCase):
    def fixture(self):
        row = dict(id='s1', group='order', priority='P1', status='pass', coverage_key='money',
                   executed=True, asserted=True, evidence={'receipt': 'fixture:1'},
                   expected=10, actual=10)
        return dict(schema_version='universal-qa/v1', environment='synthetic', source_revision='a'*40,
                    adapter_versions={'orders': '1'}, scenarios=[row],
                    counts=dict(generated=1, executed=1, asserted=1, unique_failures=0),
                    coverage=dict(required=['money'], observed=['money']))

    def admit(self, report):
        report = copy.deepcopy(report)
        report['report_digest'] = digest({k:v for k,v in report.items() if k != 'report_digest'})
        return admit_qa_report(report, expected_source_revision='a'*40, required_coverage=['money'])

    def test_admission_is_only_synthetic_review(self):
        result = self.admit(self.fixture())
        self.assertTrue(result['admitted'])
        self.assertFalse(result['operational_authorization'])
        self.assertEqual('synthetic_only', result['scope'])

    def test_large_passing_total_cannot_hide_critical_fail_or_coverage_gap(self):
        report = self.fixture()
        report['scenarios'] += [dict(report['scenarios'][0], id=f's{i}') for i in range(2, 102)]
        report['counts'].update(generated=101, executed=101, asserted=101)
        report['scenarios'][0].update(status='fail', actual=9)
        report['counts']['unique_failures'] = 1
        self.assertIn('critical_scenario_not_passed', self.admit(report)['reasons'])
        report = self.fixture(); report['coverage']['required'] = []
        self.assertIn('required_coverage_missing', self.admit(report)['reasons'])

    def test_critical_blocked_unmeasured_and_missing_evidence_block(self):
        for status in ('blocked', 'unmeasured'):
            report = self.fixture(); report['scenarios'][0].update(status=status, asserted=False)
            report['counts']['asserted'] = 0; report['coverage']['observed'] = []
            self.assertFalse(self.admit(report)['admitted'])
        report = self.fixture(); report['scenarios'][0]['evidence'] = None
        self.assertIn('verdict_without_execution_assertion_evidence', self.admit(report)['reasons'])

    def test_count_source_duplicates_digest_and_malformed_input_block(self):
        mutations = [lambda r:r['counts'].update(asserted=999),
                     lambda r:r.update(source_revision='b'*40),
                     lambda r:r['scenarios'].append(copy.deepcopy(r['scenarios'][0])),
                     lambda r:r['scenarios'][0].update(executed='yes'),
                     lambda r:r.update(coverage={'required':['money'], 'observed':['invented']})]
        for mutate in mutations:
            report = self.fixture(); mutate(report)
            self.assertFalse(self.admit(report)['admitted'])
        report=self.fixture();report['report_digest']='0'*64
        self.assertIn('report_digest_mismatch', admit_qa_report(report,
            expected_source_revision='a'*40, required_coverage=['money'])['reasons'])
        for value in (None, [], {}, {'scenarios': [None]}):
            self.assertFalse(admit_qa_report(value, expected_source_revision='a'*40,
                required_coverage=['money'])['admitted'])

    def test_noncritical_failure_disclosed_and_unique_failure_recomputed(self):
        report = self.fixture()
        report['scenarios'][0].update(priority='P2', status='fail', actual=9, failure_key='defect1')
        report['counts']['unique_failures']=1
        result=self.admit(report)
        self.assertTrue(result['admitted']);self.assertTrue(result['warnings'])
        report['counts']['unique_failures']=0
        self.assertFalse(self.admit(report)['admitted'])

    def test_formula_pass_cannot_hide_mismatched_values_or_contract(self):
        report=self.fixture(); row=report['scenarios'][0]
        row['formula']=dict(id='f1', expression='unit*quantity', variables={'unit':10,'quantity':1},
                            units={'unit':'KRW','quantity':'count'}, rounding='integer',
                            expected=10, actual=11, status='pass')
        self.assertIn('formula_pass_value_mismatch', self.admit(report)['reasons'])
        row['formula']['actual']=10; self.assertTrue(self.admit(report)['admitted'])
        del row['formula']['units']; self.assertFalse(self.admit(report)['admitted'])

    def test_type_confusion_false_failure_and_forged_state_counts_block(self):
        report=self.fixture(); report['scenarios'][0].update(expected=1, actual=True)
        self.assertIn('pass_value_mismatch', self.admit(report)['reasons'])
        report=self.fixture(); report['scenarios'][0].update(status='fail')
        report['counts']['unique_failures']=1
        self.assertIn('failure_without_value_mismatch', self.admit(report)['reasons'])
        report=self.fixture(); report['counts']['unique_states']=1
        self.assertIn('state_counts_mismatch', self.admit(report)['reasons'])
        report=self.fixture(); report['counts']['pass']=999
        self.assertIn('status_counts_mismatch', self.admit(report)['reasons'])
        report=self.fixture(); report['scenarios'][0].update(adapter='orders',inputs={},state_key='a'*64)
        report['counts']['unique_states']=1
        self.assertIn('state_key_mismatch', self.admit(report)['reasons'])
