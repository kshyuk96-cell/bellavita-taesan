from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from manager_bridge.hope_customer_coverage import coverage_contract,assess,coverage_probes
from manager_bridge.hope_partial_oracle import split_integer,room_expectation
from manager_bridge.hope_persistent_review import business_pin,provision_business,check_business
from manager_bridge.hope_shared_inventory import provision_exchange,independent_domain_check
from manager_bridge.hope_batch05 import validate_execution_reports
from manager_bridge.store import Conflict

SOURCE='a'*40
BINDING=dict(project='hope-tour',task='hope-batch05-source-candidate',source_revision=SOURCE,test_contract_digest='b'*64,adapter_release='persistent-recovery-v1',generation=1)


def business_contract():return dict(schema='hope-persistent-review/v1',environment='isolated_nonproduction',project='hope-tour',task=BINDING['task'],source_revision=SOURCE,database='review.sqlite',schema_version=1,operational_tables=False,real_pg=False)


class EvidenceContractTests(unittest.TestCase):
    def setUp(self):
        self.contract=coverage_contract(BINDING)
        self.record=dict(binding=deepcopy(BINDING),row='PAY-06',check='duplicate_payment',scope='actual_source_api',status='pass',actual={'effects':1})

    def test_partial_api_never_means_full_workflow(self):
        result=assess(self.contract,[self.record],binding=BINDING)
        self.assertEqual(38,len(result['rows']));self.assertEqual(0,result['counts']['PASS']);self.assertFalse(result['admitted'])
        row=next(r for r in result['rows'] if r['id']=='PAY-06');self.assertEqual('BLOCKED',row['status']);self.assertIn('pg',row['missing'])

    def test_each_binding_field_including_boolean_generation_rejected(self):
        for key in BINDING:
            value=deepcopy(self.record);value['binding'][key]=True if key=='generation' else 'wrong'
            with self.subTest(key=key),self.assertRaises(Conflict):assess(self.contract,[value],binding=BINDING)

    def test_wrong_function_mapping_and_duplicate_evidence_rejected(self):
        value=deepcopy(self.record);value['row']='MEM-01'
        with self.assertRaises(Conflict):assess(self.contract,[value],binding=BINDING)
        with self.assertRaises(Conflict):assess(self.contract,[self.record,self.record],binding=BINDING)

    def test_deleted_critical_row_and_priority_lowering_rejected(self):
        changed=deepcopy(self.contract);changed['rows'].pop('MEM-07')
        with self.assertRaises(Conflict):assess(changed,[],binding=BINDING)
        changed=deepcopy(self.contract);changed['rows']['PAY-06']['priority']='P3'
        with self.assertRaises(Conflict):assess(changed,[],binding=BINDING)

    def test_worker_cannot_claim_browser_pg_or_policy_capability(self):
        for check,scope in [('ui','supported_browser'),('pg','actual_pg'),('policy','approved_policy')]:
            value=deepcopy(self.record);value.update(check=check,scope=scope)
            with self.subTest(scope=scope),self.assertRaises(Conflict):assess(self.contract,[value],binding=BINDING)
        with self.assertRaises(Conflict):assess(self.contract,[],binding=BINDING,capabilities={'actual_pg'})

    def test_safety_probes_cover_all_bindings_and_claims(self):
        result=coverage_probes(BINDING);self.assertEqual(10,result[96]['rejected']);self.assertTrue(result[99]['unapprovedNotAdmitted'])


class PersistentBusinessTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.exchange=self.root/'exchange';self.exchange.mkdir();self.business=self.root/'business';self.business.mkdir()
        inv=dict(schema='hope-shared-fixture-inventory/v1',environment='isolated_nonproduction',project='hope-tour',source_revision=SOURCE,owner='fixture',adapter_version='shared-fixture-v1',operational_tables=False,supplier_connected=False,resource={},slots={'one':1})
        provision_exchange(self.exchange,inv,SOURCE);self.contract=business_contract();provision_business(self.business,self.contract,SOURCE,self.exchange)

    def test_exact_source_task_contract_and_fresh_database(self):
        self.assertEqual(self.business,check_business(self.business,self.contract,SOURCE))
        with self.assertRaises(Conflict):provision_business(self.business,self.contract,SOURCE,self.exchange)
        for field,value in [('source_revision','b'*40),('task','another'),('operational_tables',True),('schema_version',True)]:
            changed=dict(self.contract,**{field:value})
            with self.subTest(field=field),self.assertRaises(Conflict):business_pin(changed,SOURCE)

    def test_metadata_or_symlink_cannot_redirect_business_binding(self):
        with sqlite3.connect(self.business/'review.sqlite') as db:db.execute("UPDATE qa_review_runtime SET source=?",('b'*40,))
        with self.assertRaises(Conflict):check_business(self.business,self.contract,SOURCE)
        other=self.root/'link';other.symlink_to(self.business,target_is_directory=True)
        with self.assertRaises(ValueError):check_business(other,self.contract,SOURCE)


class StopReceiptTests(unittest.TestCase):
    def fixture(self):return dict(schema='hope-actual-service-batch05/v1',role='setup',source_revision=SOURCE,inventory_digest='i',business_digest='b',realPG=False,supplier_connected=False,operational_authorization=False,customer_acceptance_approved=False,stop_certificate=dict(source_revision=SOURCE,task='task',container='worker-a',container_absent=True,process_exited=True),runtime_provenance=dict(container_name='worker-a',process_exited=True))
    def test_live_stale_role_scope_or_reused_executor_rejected(self):
        report=self.fixture();validate_execution_reports([(report,'setup',False)],SOURCE,'task','i','b')
        for attack in ('live','source','role','scope'):
            changed=deepcopy(report)
            if attack=='live':changed['stop_certificate']['container_absent']=False
            elif attack=='source':changed['source_revision']='c'*40
            elif attack=='role':changed['role']='main'
            else:changed['realPG']=True
            with self.subTest(attack=attack),self.assertRaises(Conflict):validate_execution_reports([(changed,'setup',False)],SOURCE,'task','i','b')
        with self.assertRaises(Conflict):validate_execution_reports([(report,'setup',False),(report,'setup',False)],SOURCE,'task','i','b')

    def test_exit_zero_or_child_claim_does_not_prove_interruption(self):
        report=self.fixture();report.update(schema='hope-persistent-interruption/v1',role='crash-before-store',interrupted=True,exit_code=0)
        with self.assertRaises(Conflict):validate_execution_reports([(report,'crash-before-store',True)],SOURCE,'task','i','b')


class PartialAllocationTests(unittest.TestCase):
    def test_odd_total_and_stable_fractional_tie(self):
        expected=room_expectation();self.assertEqual([500006,500005],[r['total'] for r in expected]);self.assertEqual([50001,50000],[r['sources']['BP'] for r in expected]);self.assertEqual([100005,100005],[r['sources']['card'] for r in expected])
        self.assertEqual([1,0,0],split_integer(1,[1,1,1]));self.assertEqual([0,0],split_integer(0,[0,0]))
        with self.assertRaises(ValueError):split_integer(True,[1,1])
        with self.assertRaises(ValueError):split_integer(3,[1,1])

    def test_unpaid_cancelled_card_is_not_over_refund_or_missing_payment(self):
        state=dict(ledger=[],bookings=[dict(id='b',contractId='ct',total=10,status='cancelled',pointsCommitted=True,plan=dict(CP=0,SP=0,BP=0,DP=0,card=10))],refunds=[dict(id='r',bookingId='b',status='refunded',quote=dict(refundTotal=0,fee=0,unpaidReleased=10,refund=dict(CP=0,SP=0,BP=0,DP=0,card=0)))],invoices=[],travelEvents=[])
        self.assertEqual([],independent_domain_check(state))
        state['refunds'][0]['quote']['unpaidReleased']=9
        self.assertIn('paid_invoice_card_mismatch',independent_domain_check(state))

if __name__=='__main__':unittest.main()
