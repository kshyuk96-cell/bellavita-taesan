import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from manager_bridge.hope_review_client import HopeReviewClient,ReviewUncertain
from manager_bridge.hope_tour_acceptance import evaluate,CASES
from manager_bridge.hope_tour_reporting import final_report,ROWS
from manager_bridge.hope_tour_exports import spreadsheet,document
from taesan_core.hope_tour_qa import allocation_oracle,refund_oracle,formula_suite,fixture_adapters,BP_LIMITS
from taesan_core.universal_qa import run_scenarios,build_contract,EvidenceUnavailable
from manager_bridge.qa_admission import admit_qa_report


class HopeTourQATests(unittest.TestCase):
    def test_customer_examples_and_invalid_money(self):
        allocation=allocation_oracle(dict(price=1000000,DP=600000,BP=150000,SP=100000,bp_limit_bps=1000,bonus_order='BP_SP'))
        self.assertEqual(dict(DP=600000,BP=100000,SP=100000,card=200000),allocation)
        self.assertEqual(750000,refund_oracle(dict(principal=1200000,used=450000,returned=0,deductions=0,BP=85000)))
        for value in (-1,True,1.5,'100'):
            with self.assertRaises(ValueError):refund_oracle(dict(principal=value,used=0,returned=0,deductions=0))

    def test_pending_bonus_order_is_not_assumed(self):
        with self.assertRaises(EvidenceUnavailable):allocation_oracle(dict(price=100,DP=0,BP=100,SP=100,bp_limit_bps=1000))

    def test_canonical_final_format_has_38_unique_ids(self):
        self.assertEqual(38,len(ROWS));self.assertEqual(38,len({r[0] for r in ROWS}))
        self.assertEqual({f'MEM-{i:02}' for i in range(1,11)},{r[0] for r in ROWS if r[0].startswith('MEM-')})
        self.assertEqual(1800,BP_LIMITS['premium']['overseas'])

    def test_empty_evidence_never_reports_executed_or_pass(self):
        value=evaluate({},sut_revision='a'*40)
        self.assertFalse(value['admission']['admitted'])
        self.assertEqual(0,value['report']['counts']['executed'])
        self.assertEqual(len(CASES),value['report']['counts']['unmeasured'])
        self.assertTrue(all(not r['asserted'] for r in value['report']['scenarios']))
        final=final_report(value,{},engine_revision='b'*40,sut_revision='a'*40)
        self.assertEqual(38,final['summary']['total']);self.assertEqual(0,final['summary']['counts']['PASS'])
        self.assertIsNone(final['summary']['execution_basis_pass_rate_percent'])
        self.assertFalse(final['summary']['customer_acceptance_approved'])

    def test_simulator_cannot_attest_real_pg_or_mobile(self):
        observations={key:dict(actual=expected,scope='isolated_server_review_http',sut_revision='a'*40,source_window_verified=True,steps=['fixture'])
                      for key,(_,expected) in CASES.items()}
        value=evaluate(observations,sut_revision='a'*40)
        self.assertFalse(value['admission']['admitted'])
        for key in ('actual-pg','real-devices','mobile-help','bonus-order-approved'):
            self.assertEqual('unmeasured',next(r for r in value['report']['scenarios'] if r['id']==key)['status'])
        final=final_report(value,{},engine_revision='b'*40,sut_revision='a'*40)
        self.assertFalse(any(r['status']=='PASS' for r in final['scenarios']))

    def test_stale_evidence_blocks_and_declaration_conflict_is_scoped(self):
        observation=dict(actual='SP',scope='server_html_declaration',sut_revision='a'*40,source_window_verified=True,steps=['FB-13 label'])
        value=evaluate({'principal-first-declaration':observation},sut_revision='a'*40)
        row=next(r for r in value['report']['scenarios'] if r['id']=='principal-first-declaration')
        self.assertEqual('fail',row['status'])
        final=final_report(value,{},engine_revision='b'*40,sut_revision='a'*40)
        self.assertEqual('NOT RUN',next(r for r in final['scenarios'] if r['test_id']=='PAY-01')['status'])
        self.assertEqual('developer_declaration_conflict_not_runtime_proof',final['defects'][0]['kind'])
        self.assertEqual('blocked',next(r for r in evaluate({'principal-first-declaration':observation},sut_revision='c'*40)['report']['scenarios'] if r['id']=='principal-first-declaration')['status'])

    def test_formula_scenarios_are_reproducible_and_mutations_detected(self):
        suite=formula_suite();adapters=fixture_adapters();contract=build_contract(suite,adapters)
        self.assertEqual(contract,build_contract(formula_suite(),adapters))
        report=run_scenarios(suite,adapters,source_revision='a'*40,seed=20261116,
            required_coverage=['principal-first','principal-refund-ceiling','grade-category-cap','grade-initial-charge'])
        self.assertEqual(2060,report['counts']['pass']);self.assertEqual(2060,report['counts']['unique_states'])
        for mutant in ('bonus_first','ignore_bp_cap','refund_bonus','refund_signup_fee'):
            altered=run_scenarios(suite,fixture_adapters(mutant),source_revision='a'*40,seed=20261116,required_coverage=report['coverage']['required'])
            self.assertGreater(altered['counts']['fail'],0)
            self.assertFalse(admit_qa_report(altered,expected_source_revision='a'*40,required_coverage=report['coverage']['required'],expected_contract=contract)['admitted'])

    def test_client_forbids_operational_origin_actions_and_sensitive_fields(self):
        with self.assertRaises(ValueError):HopeReviewClient(origin='https://payments.example.com')
        c=HopeReviewClient();c.capabilities=Mock()
        for action,fields in [('deploy',{}),('registerCard',{'scenario':'success','cardNumber':'forbidden'}),
                              ('signup',{'grade':'hope','consents':{'cardNumber':'forbidden'}})]:
            with self.assertRaises(ValueError):c.command(action,fields)
        c.capabilities.assert_not_called()

    def test_guard_false_boolean_and_no_uncertain_retry(self):
        c=HopeReviewClient();c._request=Mock(return_value=(200,dict(mode='isolated_server_review',cardProvider='simulator',serverPersistence=True,
              realCharges=0,operationalWrites=False,realNotifications=False)))
        with self.assertRaises(ReviewUncertain):c.login('member')
        self.assertEqual(1,c._request.call_count)
        c=HopeReviewClient();c._opener=Mock();c._opener.open.side_effect=TimeoutError('private detail')
        with self.assertRaises(ReviewUncertain):c._request('POST','/review/api/command',{})
        self.assertEqual(1,c._opener.open.call_count)
        self.assertNotIn('private detail',json.dumps(c.events))

    def test_customer_exports_and_spreadsheet_formula_injection(self):
        value=evaluate({},sut_revision='a'*40);final=final_report(value,{},engine_revision='b'*40,sut_revision='a'*40)
        final['summary']['external_text']='=HYPERLINK("bad")'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);spreadsheet(final,root/'report.xlsx');document(final,root/'report.docx')
            from openpyxl import load_workbook
            book=load_workbook(root/'report.xlsx')
            self.assertEqual(39,book['시나리오결과'].max_row);self.assertEqual(81,book['FB80추적표'].max_row)
            self.assertTrue(book['종합결과'].cell(book['종합결과'].max_row,2).value.startswith("'="))
            from docx import Document
            self.assertTrue(any('시나리오별 결과보고서' in p.text for p in Document(root/'report.docx').paragraphs))


    def test_source_window_change_or_missing_post_observation_never_verifies(self):
        from manager_bridge.hope_tour_acceptance import verify_source_window
        before=dict(project_id='fixture',origin='https://hope-tour-feedback.shrewdmaxim1.chatgpt.site',
            version_id='v31',deployment_id='deploy31',source_revision='a'*40,deployment_status='succeeded',
            observed_at='2026-11-16T01:00:00+00:00')
        after=dict(before,observed_at='2026-11-16T01:03:00+00:00')
        observed=dict(started_at='2026-11-16T01:01:00+00:00',finished_at='2026-11-16T01:02:00+00:00')
        self.assertTrue(verify_source_window(observed,before,after,'a'*40))
        self.assertFalse(verify_source_window(observed,before,None,'a'*40))
        self.assertFalse(verify_source_window(observed,before,dict(after,version_id='v32'),'a'*40))
        self.assertFalse(verify_source_window(observed,before,dict(after,observed_at='2026-11-16T00:59:00+00:00'),'a'*40))
