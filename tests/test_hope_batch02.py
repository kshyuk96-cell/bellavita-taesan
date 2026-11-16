"""Admission attacks and conservative customer evidence mappings (no target code)."""
import unittest
from copy import deepcopy
from pathlib import Path
from manager_bridge.hope_batch02 import EXPECTED,contract,evaluate,customer_report,glossary_observation,formula_report
from manager_bridge.hope_service_runtime import ActualServiceProfile
from manager_bridge.store import Conflict
from taesan_core.hope_tour_qa import GLOSSARY
SOURCE='a'*40
IMAGE='node@sha256:'+'b'*64


def fixture():
    items=[dict(id=n,title='fixture',actual=dict(source=SOURCE,**v) if n==21 else deepcopy(v)) for n,v in EXPECTED.items()]
    pages=[dict(path=p,html='<body>'+''.join('<span>'+v+'</span> ('+k+')' for k,v in GLOSSARY.items())+'</body>') for p in ['/review/','/review/features','/review/app','/review/policy']]
    return dict(schema='hope-actual-service-batch02/v1',source_revision=SOURCE,scope='actual_service_source_local_sqlite_simulator',
        realPG=False,operational_authorization=False,customer_acceptance_approved=False,items=items,artifacts=dict(terms_pages=pages))


class Batch02Tests(unittest.TestCase):
    def test_missing_browser_blocks_admission_even_with_claimed_pass(self):
        raw=fixture();raw['counts']={'PASS':20};raw['items'][17].update(status='PASS',actual=True)
        result=evaluate(raw,SOURCE,contract(SOURCE))
        self.assertEqual(result['counts']['unmeasured'],1);self.assertFalse(result['admission']['admitted'])

    def test_worker_expected_and_status_cannot_hide_money_defect(self):
        raw=fixture();row=next(r for r in raw['items'] if r['id']==34)
        row.update(actual=dict(EXPECTED[34],DP=599999),expected=dict(EXPECTED[34],DP=599999),status='PASS')
        result=evaluate(raw,SOURCE,contract(SOURCE));self.assertEqual(result['counts']['fail'],1)
        self.assertFalse(result['admission']['admitted'])

    def test_missing_duplicate_unknown_source_and_scope_rejected(self):
        for attack in ('missing','duplicate','unknown','source','scope'):
            raw=fixture()
            if attack=='missing':raw['items'].pop()
            elif attack=='duplicate':raw['items'][-1]=raw['items'][0]
            elif attack=='unknown':raw['items'][-1]['id']=99
            elif attack=='source':raw['source_revision']='c'*40
            else:raw['realPG']=True
            with self.subTest(attack=attack),self.assertRaises(Conflict):evaluate(raw,SOURCE,contract(SOURCE))

    def test_contract_cannot_be_lowered(self):
        trusted=contract(SOURCE);trusted['scenarios'][next(iter(trusted['scenarios']))]['priority']='P3'
        with self.assertRaises(Conflict):evaluate(fixture(),SOURCE,trusted)

    def test_html_oracle_ignores_scripts_and_normalizes_markup(self):
        raw=fixture();self.assertTrue(all(glossary_observation(raw['artifacts']['terms_pages']).values()))
        for p in raw['artifacts']['terms_pages']:p['html']='<body>DP<script>본인 납입금 잔액(DP)</script></body>'
        self.assertFalse(any(glossary_observation(raw['artifacts']['terms_pages']).values()))
        self.assertEqual(evaluate(raw,SOURCE,contract(SOURCE))['counts']['fail'],1)

    def test_partial_source_checks_never_promote_customer_pass(self):
        raw=fixture();result=evaluate(raw,SOURCE,contract(SOURCE))
        report=customer_report(raw,result,engine_revision='d'*40)
        self.assertEqual(len(report['scenarios']),38);self.assertEqual(report['summary']['counts']['PASS'],0)
        self.assertFalse(report['summary']['customer_acceptance_approved'])
        self.assertTrue(all(r['environment']['url'].startswith('local-source://') for r in report['scenarios']))
        self.assertIsNone(report['summary']['deployed_revision'])

    def test_independent_formula_detects_return_and_refund_tampering(self):
        raw=dict(source_revision=SOURCE,artifacts=dict(
            signup={grade:dict(paid=paid,DP=principal) for grade,paid,principal in [('hope',75000,50000),('happy',150000,100000),('premium',300000,200000)]},
            cancellation=dict(before=dict(balances=dict(DP=0,BP=50000,SP=0),invoices=[dict(kind='travel',status='paid',amount=200000)]),
                after=dict(balances=dict(DP=600000,BP=150000,SP=100000))),
            withdrawal=dict(principalRecords=[dict(type='executed',status='simulated_returned',amount=750000)])))
        self.assertTrue(all(r['status']=='PASS' for r in formula_report(raw)['records']))
        for key in ('signup','refund','return'):
            altered=deepcopy(raw)
            if key=='signup':altered['artifacts']['signup']['hope']['DP']=75000
            elif key=='refund':altered['artifacts']['cancellation']['after']['balances']['BP']+=1
            else:altered['artifacts']['withdrawal']['principalRecords'][0]['amount']+=85000
            with self.subTest(key=key):self.assertTrue(any(r['status']=='FAIL' for r in formula_report(altered)['records']))

    def test_profile_rejects_unpinned_runtime(self):
        with self.assertRaises(ValueError):ActualServiceProfile(Path('.'),SOURCE,'node:latest')
        with self.assertRaises(Conflict):ActualServiceProfile(Path('.'),SOURCE,IMAGE).source_check()

if __name__=='__main__':unittest.main()
