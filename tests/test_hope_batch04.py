from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from manager_bridge.hope_shared_inventory import provision_exchange,validate_contract,check_exchange,independent_stock_check,independent_domain_check
from manager_bridge.hope_registered_runtime import HopeArtifacts
from manager_bridge.hope_batch04 import contract,evaluate,EXPECTED,PAYMENT_PLANS
from manager_bridge.qa_jobs import QAJobs
from manager_bridge.store import Store,Conflict
from manager_bridge.tests import test_qa_admission
from taesan_core.hope_tour_qa import GLOSSARY

SOURCE='a'*40


def inventory_contract():
    return dict(schema='hope-shared-fixture-inventory/v1',environment='isolated_nonproduction',project='hope-tour',source_revision=SOURCE,owner='fixture-owner',adapter_version='shared-fixture-v1',operational_tables=False,supplier_connected=False,resource=dict(product_id='P02',departure='2027-03-16',room='TEST 객실'),slots={'one':1})


def domain():
    return dict(contracts=[dict(id='ct')],bookings=[dict(id='b',contractId='ct',total=100,plan=dict(DP=100,BP=0,SP=0,CP=0,card=0))],
        refunds=[dict(id='r',bookingId='b',status='refunded',quote=dict(refundTotal=100,fee=0,refund=dict(DP=100,BP=0,SP=0,CP=0,card=0)))],
        ledger=[dict(id='j',sourceKey='refund:r',entries=[dict(account='ct',book='DP',delta=100),dict(account='system',book='DP',delta=-100)])],invoices=[],travelEvents=[])


def evidence():
    inventory=inventory_contract();stock=dict(slots=[dict(slot='one',capacity=1,available=1)],leases=[],events=[])
    d=domain();faults=[]
    altered=deepcopy(stock);altered['slots'][0]['available']=2;faults.append(dict(id='free-stock-corruption',inventory=altered))
    for name in ('duplicate-debit','excess-refund','unbalanced-journal'):
        value=deepcopy(d)
        if name=='duplicate-debit':value['ledger'].append(deepcopy(value['ledger'][0]))
        elif name=='excess-refund':value['refunds'][0]['quote']['refund']['DP']+=1
        else:value['ledger'][0]['entries'][0]['delta']+=1
        faults.append(dict(id=name,domain=value,targetDetected=False))
    pages=[dict(path=p,html='<body>'+''.join(v+'('+k+')' for k,v in GLOSSARY.items())+'</body>') for p in ['/review/','/review/features','/review/app','/review/policy']]
    def flow(label):
        value=deepcopy(d);plan=PAYMENT_PLANS[label];value['bookings'][0].update(total=sum(plan.values()),plan=deepcopy(plan))
        value['refunds'][0]['quote'].update(refundTotal=sum(plan.values()),refund=deepcopy(plan));value['travelEvents']=[dict(type='refunded',refundId='r',amount=plan['card'])]
        value['ledger'][0]['entries']=[dict(account=a,book=k,delta=amount*direction) for k,amount in plan.items() if k!='card' for a,direction in [('ct',1),('system',-1)]]
        return value
    raw=dict(schema='hope-actual-service-batch04/v1',source_revision=SOURCE,inventory_digest=validate_contract(inventory,SOURCE),scope='actual_service_source_shared_test_inventory',role='main',realPG=False,operational_authorization=False,customer_acceptance_approved=False,
        items=[dict(id=n,actual=deepcopy(EXPECTED[n])) for n in (61,62,63,64,65,67,68,69,70)],artifacts=dict(inventory=stock,cleanDomain=d,duplicate=flow("duplicate"),partial=flow("partial"),faultInjection=faults,terms_pages=pages))
    return raw,inventory,{n:deepcopy(EXPECTED[n]) for n in (66,76,77,78)}


class Batch04Oracles(unittest.TestCase):
    def test_scope_source_and_boolean_capacity_rejected(self):
        for attack in ('source','environment','supplier','capacity'):
            value=inventory_contract()
            if attack=='source':value['source_revision']='b'*40
            elif attack=='environment':value['environment']='production'
            elif attack=='supplier':value['supplier_connected']=True
            else:value['slots']['one']=True
            with self.subTest(attack=attack),self.assertRaises(Conflict):validate_contract(value,SOURCE)

    def test_only_fresh_dedicated_exchange_and_pinned_contract(self):
        root=Path(self.enterContext(tempfile.TemporaryDirectory()));pin=provision_exchange(root,inventory_contract(),SOURCE)
        self.assertEqual(root,check_exchange(root,pin))
        with self.assertRaises(Conflict):provision_exchange(root,inventory_contract(),SOURCE)
        with self.assertRaises(Conflict):check_exchange(root,'0'*64)
        (root/'unexpected.db').touch()
        with self.assertRaises(Conflict):check_exchange(root,pin)

    def test_stock_conservation_cannot_hide_capacity_change(self):
        raw,inv,_=evidence();snapshot=raw['artifacts']['inventory'];self.assertEqual([],independent_stock_check(snapshot,inv['slots']))
        snapshot['slots'][0].update(capacity=2,available=2)
        self.assertIn('trusted_capacity_mismatch',independent_stock_check(snapshot,inv['slots']))
        snapshot['slots'][0].update(capacity=1,available=1);snapshot['leases']=[dict(key='x',slot='one',quantity=1,released=0,status='uncertain')]
        self.assertIn('inventory_conservation:one',independent_stock_check(snapshot,inv['slots']))

    def test_independent_ledger_detects_balanced_duplicate_and_overrefund(self):
        clean=domain();self.assertEqual([],independent_domain_check(clean))
        value=deepcopy(clean);value['ledger'].append(deepcopy(value['ledger'][0]));self.assertIn('duplicate_journal_source',independent_domain_check(value))
        value=deepcopy(clean);value['refunds'][0]['quote']['refund']['DP']=101
        self.assertIn('excess_refund_source',independent_domain_check(value))

    def test_balanced_underrefund_and_wrong_native_plan_are_rejected(self):
        state=domain();state['bookings'][0].update(status='cancelled',pointsCommitted=True)
        state['refunds'][0]['quote']['refund']['DP']=99;state['refunds'][0]['quote']['refundTotal']=99
        state['ledger'][0]['entries'][0]['delta']=99;state['ledger'][0]['entries'][1]['delta']=-99
        self.assertIn('full_refund_source_mismatch',independent_domain_check(state))
        raw,inv,host=evidence();raw['artifacts']['duplicate']['bookings'][0]['plan']['DP']-=1
        self.assertEqual(evaluate(raw,SOURCE,contract(SOURCE),host,raw['inventory_digest'],inv)['counts']['fail'],1)

    def test_card_refund_event_must_match_original_refund(self):
        state=domain();state['bookings'][0].update(total=110,plan=dict(DP=100,BP=0,SP=0,CP=0,card=10))
        state['refunds'][0]['quote'].update(refundTotal=110);state['refunds'][0]['quote']['refund']['card']=10
        state['travelEvents']=[dict(type='refunded',refundId='r',amount=9)]
        self.assertIn('refund_card_event_mismatch',independent_domain_check(state))
        state['travelEvents'][0]['amount']=10
        self.assertEqual([],independent_domain_check(state))

    def test_browser_self_claim_cannot_satisfy_four_required_checks(self):
        raw,inv,host=evidence();host.update({n:True for n in (72,73,74,75)})
        result=evaluate(raw,SOURCE,contract(SOURCE),host,raw['inventory_digest'],inv)
        self.assertEqual(result['counts']['unmeasured'],4);self.assertEqual(result['counts']['fail'],0);self.assertFalse(result['admission']['admitted'])
        self.assertEqual(4,sum(f['detected'] for f in result['fault_injection']))

    def test_source_role_case_contract_and_fault_count_attacks(self):
        raw,inv,host=evidence()
        for attack in ('source','role','case','contract'):
            value=deepcopy(raw);trusted=contract(SOURCE)
            if attack=='source':value['source_revision']='b'*40
            elif attack=='role':value['role']='race-a'
            elif attack=='case':value['items'][-1]=value['items'][0]
            else:trusted['scenarios']['batch04-65']['expected']={}
            with self.subTest(attack=attack),self.assertRaises(Conflict):evaluate(value,SOURCE,trusted,host,raw['inventory_digest'],inv)
        raw['artifacts']['faultInjection'].pop()
        self.assertEqual(evaluate(raw,SOURCE,contract(SOURCE),host,raw['inventory_digest'],inv)['counts']['fail'],1)


class SealedArtifacts(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.store=Store(self.root/'state.sqlite')
        self.owner=self.store.add_token('owner','owner',['*']);self.store.register_project(self.owner,'hope','fixture/hope');self.worker=self.store.add_token('worker','worker',['hope'])
        f=test_qa_admission.QAAdmissionTests();report=f.fixture();jobs=QAJobs(self.store);approval=jobs.approve(self.owner,project='hope',source_revision='a'*40,contract=f.expected_contract,coverage=['money'],adapter_release='probe-v1')
        self.job=jobs.receive(self.owner,approval,'probe','probe');jobs.claim(self.worker,self.job['id'])
        report['job_binding']=dict(project='hope',task='probe',approval=approval,adapter_release='probe-v1',source_revision='a'*40)
        from manager_bridge.store import digest
        report['report_digest']=digest({k:v for k,v in report.items() if k!='report_digest'});jobs.finish(self.worker,self.job['id'],report)
        self.output=self.root/'output';self.output.mkdir();(self.output/'customer-report.json').write_text('{}');self.repository=HopeArtifacts(self.store);self.repository.collect(self.owner,self.job['id'],self.output)

    def test_sealed_bundle_detects_deleted_row(self):
        self.repository.seal(self.owner,self.job['id'],['customer-report.json'])
        with self.store._transaction() as db:db.execute('DELETE FROM hope_job_artifacts WHERE job=?',(self.job['id'],))
        with self.assertRaises(Conflict):self.repository.list(self.owner,self.job['id'])

    def test_bytes_tamper_detected_on_listing_and_read(self):
        with self.store._transaction() as db:db.execute('UPDATE hope_job_artifacts SET data=? WHERE job=?',(b'changed',self.job['id']))
        with self.assertRaises(Conflict):self.repository.list(self.owner,self.job['id'])
        with self.assertRaises(Conflict):self.repository.get(self.owner,self.job['id'],'customer-report.json')

    def test_seal_requires_owner_and_missing_files_are_rejected(self):
        with self.assertRaises(PermissionError):self.repository.seal(self.worker,self.job['id'],['customer-report.json'])
        with self.assertRaises(Conflict):self.repository.seal(self.owner,self.job['id'],['customer-report.docx'])

    def test_persisted_manifest_forbids_append_after_restart(self):
        self.repository.seal(self.owner,self.job['id'],['customer-report.json'])
        reopened=HopeArtifacts(Store(self.store.path));self.assertEqual(1,len(reopened.list(self.owner,self.job['id'])))
        (self.output/'customer-report.docx').write_bytes(b'test')
        with self.assertRaises(Conflict):reopened.collect(self.owner,self.job['id'],self.output)

if __name__=='__main__':unittest.main()
