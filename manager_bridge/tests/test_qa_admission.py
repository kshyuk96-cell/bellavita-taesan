import copy
import unittest
from manager_bridge.qa_admission import admit_qa_report
from manager_bridge.store import digest
from taesan_core.universal_qa import Risk, Scenario, Adapter, build_contract


class QAAdmissionTests(unittest.TestCase):
    def fixture(self, priority='P1', formula=False):
        formula_value = (dict(id='f1', expression='unit*quantity', variables={'unit':10,'quantity':1},
                        units={'unit':'KRW','quantity':'count'}, rounding='integer', expected=10)
                         if formula else None)
        scenario = Scenario('s1','orders','order','money',Risk(priority,'money impact','boundary','manual'),
                            {'quantity':1},10,'independent specification','one integer quote',formula_value)
        self.expected_contract = build_contract([scenario],[Adapter('orders','1',lambda _: None)])
        row = dict(self.expected_contract['scenarios']['s1'], status='pass',
                   executed=True, asserted=True, evidence={'receipt':'fixture:1'},actual=10)
        if formula:
            row['formula'] = dict(formula_value, actual=10, status='pass')
        report = dict(schema_version='universal-qa/v1', environment='synthetic', source_revision='a'*40,
                    adapter_versions={'orders': '1'}, scenarios=[row],
                    contract_digest=digest(self.expected_contract),
                    counts=dict(generated=1, executed=1, asserted=1, unique_failures=0),
                    coverage=dict(required=['money'], observed=['money']))
        return copy.deepcopy(report)

    def admit(self, report):
        report = copy.deepcopy(report)
        report['report_digest'] = digest({k:v for k,v in report.items() if k != 'report_digest'})
        return admit_qa_report(report, expected_source_revision='a'*40, required_coverage=['money'],expected_contract=self.expected_contract)

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
            expected_source_revision='a'*40, required_coverage=['money'],expected_contract=self.expected_contract)['reasons'])
        for value in (None, [], {}, {'scenarios': [None]}):
            self.assertFalse(admit_qa_report(value, expected_source_revision='a'*40,
                required_coverage=['money'],expected_contract=self.expected_contract)['admitted'])

    def test_noncritical_failure_disclosed_and_unique_failure_recomputed(self):
        report = self.fixture(priority='P2')
        report['scenarios'][0].update(status='fail', actual=9)
        row=report['scenarios'][0]
        row['failure_key']=digest([row['adapter'],row['coverage_key'],row['expected'],row['actual']])
        report['counts']['unique_failures']=1
        result=self.admit(report)
        self.assertTrue(result['admitted']);self.assertTrue(result['warnings'])
        report['counts']['unique_failures']=0
        self.assertFalse(self.admit(report)['admitted'])

    def test_formula_pass_cannot_hide_mismatched_values_or_contract(self):
        report=self.fixture(formula=True); row=report['scenarios'][0]
        row['formula']['actual']=11
        self.assertIn('formula_pass_value_mismatch', self.admit(report)['reasons'])
        row['formula']['actual']=10; self.assertTrue(self.admit(report)['admitted'])
        del row['formula']['units']; self.assertFalse(self.admit(report)['admitted'])

    def test_type_confusion_false_failure_and_forged_state_counts_block(self):
        report=self.fixture(); report['scenarios'][0].update(expected=1, actual=True)
        self.assertIn('pass_value_mismatch', self.admit(report)['reasons'])
        report=self.fixture(); report['scenarios'][0].update(status='fail')
        report['counts']['unique_failures']=1
        self.assertIn('failure_without_value_mismatch', self.admit(report)['reasons'])
        report=self.fixture(); report['counts']['unique_states']=999
        self.assertIn('state_counts_mismatch', self.admit(report)['reasons'])
        report=self.fixture(); report['counts']['pass']=999
        self.assertIn('status_counts_mismatch', self.admit(report)['reasons'])
        report=self.fixture(); report['scenarios'][0].update(adapter='orders',inputs={},state_key='a'*64)
        report['counts']['unique_states']=1
        self.assertIn('state_key_mismatch', self.admit(report)['reasons'])

    def test_manager_contract_blocks_priority_downgrade_and_missing_risk(self):
        for mutate in (lambda row:row.update(priority='P2'),
                       lambda row:row.update(risk=dict(row['risk'],priority='P3')),
                       lambda row:row.pop('risk'), lambda row:row.pop('adapter'),
                       lambda row:row.pop('state_key'), lambda row:row.pop('oracle')):
            report=self.fixture(); mutate(report['scenarios'][0])
            # Attacker can recompute all submitted hashes, but not manager contract.
            report['contract_digest']=digest({'forged':'contract'})
            self.assertFalse(self.admit(report)['admitted'])

    def test_required_formula_removal_and_coverage_priority_drift_block(self):
        report=self.fixture(formula=True)
        del report['scenarios'][0]['formula']
        self.assertIn('trusted_scenario_contract_mismatch',self.admit(report)['reasons'])
        report=self.fixture();row=report['scenarios'][0]
        row.update(priority='P3',risk=dict(row['risk'],priority='P3'),coverage_key='cosmetic')
        report['coverage']={'required':['money','cosmetic'],'observed':['cosmetic']}
        self.assertIn('trusted_scenario_contract_mismatch',self.admit(report)['reasons'])

    def test_absent_trusted_contract_and_contract_digest_relabel_block(self):
        report=self.fixture()
        report['report_digest']=digest(report)
        self.assertIn('trusted_scenario_contract_required',admit_qa_report(report,
            expected_source_revision='a'*40,required_coverage=['money'])['reasons'])
        report['scenarios'][0]['priority']='P2'
        report['scenarios'][0]['risk']['priority']='P2'
        # Replacing submitted contract digest with a downgraded contract cannot
        # alter the independently held expected_contract.
        forged=copy.deepcopy(self.expected_contract)
        forged['scenarios']['s1']['priority']='P2'
        forged['scenarios']['s1']['risk']['priority']='P2'
        report['contract_digest']=digest(forged)
        self.assertIn('scenario_contract_digest_mismatch',self.admit(report)['reasons'])
