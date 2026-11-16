from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock
from taesan_core.hope_test_data import FixtureContract,fixture_data,assert_initial_state
from taesan_core.hope_tour_qa import allocation_oracle
from manager_bridge.hope_lab_store import LabStore
from manager_bridge.hope_lab_model import LabBlocked,validate_state
from manager_bridge.hope_lab_transport import make_lab_server,LabClient,LabTransportUncertain,remote_fixture_readiness
from manager_bridge.hope_batch01 import execute_batch
from manager_bridge.hope_batch01_reports import export,customer_template
from manager_bridge.store import Conflict,digest


class HopeBatch01Tests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path=self.root/'fixtures.sqlite';self.store=LabStore(self.path)
        self.contract=FixtureContract('a'*40,example_bonus_order='BP_SP',cancellation_fee_won=0)
        self.session=self.store.create('test',self.contract)
        self.seed()

    def seed(self,*,key='seed',generation=0):
        return self.store.seed(self.session.workspace,self.session.tokens['owner'],expected_generation=generation,
                               request_key=key,contract_digest=self.contract.digest)

    def command(self,who,action,payload=None,*,key=None,generation=1):
        return self.store.command(self.session.workspace,self.session.tokens[who],generation=generation,
                                  request_key=key or action,action=action,payload=payload or {})

    def state(self):return self.store.inspect(self.session.workspace,self.session.tokens['owner'])['state']

    def buy(self):return self.command('U04','purchase',{'product':'P02'},key='purchase')['result']

    def cancel(self,booking):
        ref=self.command('U04','cancel_request',{'booking':booking})['result']['id']
        self.command('finance','cancel_review',{'refund':ref});self.command('owner','cancel_approve',{'refund':ref})
        return self.command('owner','cancel_execute',{'refund':ref}),ref

    def server(self,*,revision='a'*40):
        server=make_lab_server(self.store,revision);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def close():server.shutdown();thread.join(timeout=3);server.server_close()
        self.addCleanup(close)
        return 'http://127.0.0.1:'+str(server.server_address[1])

    def test_exact_initial_data_contract_and_origin_balances(self):
        self.assertTrue(assert_initial_state(self.state(),self.contract)['matched'])
        validate_state(self.state());self.assertEqual(10,len(self.state()['members']))
        self.assertEqual(4,len(self.state()['products']))
        for key in ('DP','BP','SP'):
            self.assertEqual(self.state()['members']['U09'][key],sum(j['amount'] for j in self.state()['journal'] if j['member']=='U09' and j['bucket']==key))

    def test_contract_rejects_nonreference_policy_and_invalid_limits(self):
        for values in (dict(scope='production'),dict(cancellation_fee_won=1),dict(cancellation_fee_won=False),
                       dict(cancellation_fee_won=0.0),dict(seed=True),dict(max_commands=0),dict(example_bonus_order='CP_BP')):
            with self.assertRaises(ValueError):FixtureContract('a'*40,**values)

    def test_no_existing_business_database_is_modified(self):
        path=self.root/'business.sqlite';c=sqlite3.connect(path);c.execute('CREATE TABLE customer(id TEXT)');c.commit();c.close();path.chmod(0o600)
        before=path.read_bytes()
        with self.assertRaises(ValueError):LabStore(path)
        self.assertEqual(before,path.read_bytes())

    def test_symlink_and_public_database_rejected(self):
        link=self.root/'link.sqlite';link.symlink_to(self.path)
        with self.assertRaises(ValueError):LabStore(link)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):LabStore(self.path)

    def test_run_recreation_is_not_automatic_replay(self):
        with self.assertRaises(Conflict):self.store.create('test',self.contract)
        self.assertNotIn(self.session.tokens['owner'],repr(self.session))

    def test_cross_workspace_credentials_denied(self):
        other=self.store.create('other',self.contract)
        with self.assertRaises(PermissionError):self.store.inspect(other.workspace,self.session.tokens['owner'])

    def test_member_and_product_roles_cannot_read_other_members(self):
        own=self.store.inspect(self.session.workspace,self.session.tokens['U04'])['state']
        self.assertEqual(['U04'],list(own['members']))
        self.assertTrue(all(j['member']=='U04' for j in own['journal']))
        self.assertEqual(['products'],list(self.store.inspect(self.session.workspace,self.session.tokens['product_admin'])['state']))
        with self.assertRaises(PermissionError):self.store.inspect(self.session.workspace,self.session.tokens['unauthorized'])

    def test_reset_owner_only_and_bad_contract_no_changes(self):
        before=digest(self.state())
        with self.assertRaises(PermissionError):self.store.seed(self.session.workspace,self.session.tokens['finance'],expected_generation=1,request_key='reset',contract_digest=self.contract.digest)
        with self.assertRaises(Conflict):self.store.seed(self.session.workspace,self.session.tokens['owner'],expected_generation=1,request_key='reset',contract_digest='0'*64)
        self.assertEqual(before,digest(self.state()))

    def test_reset_restores_data_fences_old_generation_and_retains_history(self):
        self.buy();self.seed(key='reset',generation=1)
        assert_initial_state(self.state(),self.contract)
        with self.assertRaises(Conflict):self.command('U04','purchase',{'product':'P02'})
        evidence=self.store.evidence(self.session.workspace,self.session.tokens['owner'])
        self.assertTrue(any(e['kind']=='command' and e['generation']==1 for e in evidence))
        with self.store._transaction() as c:
            old=json.loads(c.execute('SELECT state FROM lab_snapshots WHERE workspace=? AND generation=1',(self.session.workspace,)).fetchone()[0])
        self.assertEqual(1,len(old['bookings']))

    def test_duplicate_signup_and_reopen_never_double_charge(self):
        self.command('U01','set_card',{'scenario':'success'})
        first=self.command('U01','signup');self.store=LabStore(self.path)
        self.assertEqual(first,self.command('U01','signup'))
        self.assertEqual(1,sum(r['kind']=='initial' for r in self.state()['cards']))
        with self.assertRaises(LabBlocked):self.command('U01','signup',key='new-key')

    def test_request_key_payload_or_actor_changes_conflict(self):
        self.buy()
        with self.assertRaises(Conflict):self.command('U04','purchase',{'product':'P01'},key='purchase')
        with self.assertRaises(Conflict):self.command('U06','purchase',{'product':'P02'},key='purchase')

    def test_all_three_membership_grades_have_separate_principal_and_fee(self):
        for uid,monthly in [('U01',50000),('U02',100000),('U03',200000)]:
            self.command(uid,'set_card',{'scenario':'success'},key=uid+'-card')
            result=self.command(uid,'signup',key=uid+'-signup')['result']
            self.assertEqual(monthly,result['principal']);self.assertEqual(monthly//2,result['BP'])
            self.assertEqual(monthly+monthly//2,result['paid'])

    def test_mixed_purchase_matches_independent_oracle(self):
        result=self.buy();state=self.state()
        expected=allocation_oracle(dict(price=1000000,DP=600000,BP=150000,SP=100000,bp_limit_bps=1000,bonus_order='BP_SP'))
        self.assertEqual(expected,result['plan']);self.assertEqual(0,state['products']['P02']['stock'])
        self.assertEqual(1,sum(r['kind']=='purchase' for r in state['cards']))
        self.assertEqual(200000,next(r for r in state['cards'] if r['kind']=='purchase')['amount'])

    def test_zero_balance_uses_only_simulated_card(self):
        result=self.command('U05','purchase',{'product':'P02'})['result']
        self.assertEqual(dict(DP=0,BP=0,SP=0,card=1000000),result['plan'])

    def test_exact_dp_does_not_use_bonus_or_card_even_if_card_declines(self):
        self.command('U06','set_card',{'scenario':'decline'})
        result=self.command('U06','purchase',{'product':'P02'})['result']
        self.assertEqual(dict(DP=1000000,BP=0,SP=0,card=0),result['plan'])
        self.assertFalse(any(r['kind']=='purchase' for r in self.state()['cards']))

    def test_one_won_short_uses_one_bonus_point(self):
        result=self.command('U07','purchase',{'product':'P02'})['result']
        self.assertEqual(dict(DP=999999,BP=1,SP=0,card=0),result['plan'])

    def test_room_price_and_installment_data_are_server_derived(self):
        booking=self.command('U04','purchase',{'product':'P04','room':'balcony'})['result']
        self.assertEqual(1200000,booking['total']);self.assertEqual(360000,booking['deposit_gross'])
        self.assertEqual(840000,booking['balance_gross'])
        self.assertEqual('full_simulator_payment',booking['payment_mode'])

    def test_sold_out_and_invalid_quantities_do_not_change_state(self):
        before=digest(self.state())
        with self.assertRaises(LabBlocked):self.command('U04','purchase',{'product':'P03'})
        for value in (0,-1,True,3,'1'):
            with self.assertRaises(ValueError):self.command('U04','purchase',{'product':'P02','quantity':value},key='invalid-'+str(value))
        self.assertEqual(before,digest(self.state()))

    def test_declined_purchase_rolls_back_principal_points_stock_and_card(self):
        self.command('U04','set_card',{'scenario':'decline'});before=digest(self.state())
        result=self.buy()
        self.assertEqual('declined',result['status']);self.assertEqual(before,digest(self.state()))

    def test_unknown_signup_blocks_replay_reset_and_requires_owner_query(self):
        self.command('U01','set_card',{'scenario':'unknown'})
        self.assertEqual('reconciliation_required',self.command('U01','signup')['result']['status'])
        with self.assertRaises(LabBlocked):self.command('U01','signup',key='again')
        with self.assertRaises(Conflict):self.seed(key='unsafe-reset',generation=1)
        with self.assertRaises(PermissionError):self.command('finance','resolve',{'member':'U01','outcome':'success'})
        result=self.command('owner','resolve',{'member':'U01','outcome':'success'})
        self.assertEqual('active',result['result']['status'])
        self.assertEqual(result,self.command('owner','resolve',{'member':'U01','outcome':'success'}))
        with self.assertRaises(LabBlocked):self.command('owner','resolve',{'member':'U01','outcome':'success'},key='again')
        self.assertEqual(1,sum(r['kind']=='initial' for r in self.state()['cards']))

    def test_unknown_purchase_inventory_change_stays_unresolved(self):
        self.command('U04','set_card',{'scenario':'unknown'});self.buy()
        self.command('U06','purchase',{'product':'P02'},key='other-member')
        with self.assertRaises(LabBlocked):self.command('owner','resolve',{'member':'U04','outcome':'success'})
        self.assertIn('U04',self.state()['pending'])
        self.assertEqual(1,len(self.state()['bookings']))

    def test_last_seat_concurrency_approves_only_one(self):
        def buy(uid):
            try:return self.command(uid,'purchase',{'product':'P02'},key='race-'+uid)['result']['status']
            except LabBlocked:return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(buy,['U04','U06']))
        self.assertEqual(1,results.count('confirmed'));self.assertEqual(1,len(self.state()['bookings']))
        self.assertEqual(0,self.state()['products']['P02']['stock'])

    def test_pending_bonus_cp_and_suspension_policies_block(self):
        contract=FixtureContract('a'*40);session=self.store.create('pending-policy',contract)
        self.store.seed(session.workspace,session.tokens['owner'],expected_generation=0,request_key='seed',contract_digest=contract.digest)
        for uid,action,payload in [('U04','purchase',{'product':'P02'}),('U04','corporate_purchase',{}),('U08','withdraw_request',{})]:
            with self.assertRaises(LabBlocked):self.store.command(session.workspace,session.tokens[uid],generation=1,request_key=action,action=action,payload=payload)

    def test_cancellation_policy_is_not_assumed_zero(self):
        contract=FixtureContract('a'*40,example_bonus_order='BP_SP');session=self.store.create('fee-pending',contract)
        self.store.seed(session.workspace,session.tokens['owner'],expected_generation=0,request_key='seed',contract_digest=contract.digest)
        booking=self.store.command(session.workspace,session.tokens['U04'],generation=1,request_key='buy',action='purchase',payload={'product':'P02'})['result']
        with self.assertRaises(LabBlocked):self.store.command(session.workspace,session.tokens['U04'],generation=1,request_key='cancel',action='cancel_request',payload={'booking':booking['id']})

    def test_refund_restores_original_sources_inventory_and_reversal_links(self):
        booking=self.buy();before=self.state();receipt,ref=self.cancel(booking['id']);after=self.state()
        self.assertEqual(600000,after['members']['U04']['DP']);self.assertEqual(150000,after['members']['U04']['BP'])
        self.assertEqual(100000,after['members']['U04']['SP']);self.assertEqual(1,after['products']['P02']['stock'])
        self.assertEqual(before['journal'],after['journal'][:len(before['journal'])])
        self.assertTrue(all(j['reverses'] for j in receipt['journal_appended']))
        self.assertEqual(0,sum(c['amount'] for c in after['cards'] if c['kind'] in ('purchase','purchase_reversal')))
        self.assertEqual(receipt,self.command('owner','cancel_execute',{'refund':ref}))
        with self.assertRaises(LabBlocked):self.command('owner','cancel_execute',{'refund':ref},key='double-refund')

    def test_refund_requires_ownership_and_separate_finance_approval(self):
        booking=self.buy()
        with self.assertRaises(PermissionError):self.command('U05','cancel_request',{'booking':booking['id']})
        ref=self.command('U04','cancel_request',{'booking':booking['id']})['result']['id']
        with self.assertRaises(LabBlocked):self.command('owner','cancel_execute',{'refund':ref})
        with self.assertRaises(PermissionError):self.command('U04','cancel_review',{'refund':ref})

    def test_withdrawal_stops_recurring_limits_return_and_preserves_bonus(self):
        requested=self.command('U09','withdraw_request')['result'];ref=requested['id']
        self.assertEqual(750000,requested['ceiling']);self.assertEqual(0,requested['free_bonus_cash'])
        self.assertFalse(self.state()['members']['U09']['recurring'])
        with self.assertRaises(LabBlocked):self.command('U09','purchase',{'product':'P01'})
        self.command('finance','withdraw_review',{'withdrawal':ref});self.command('owner','withdraw_approve',{'withdrawal':ref})
        receipt=self.command('owner','withdraw_execute',{'withdrawal':ref})
        member=self.state()['members']['U09'];self.assertEqual(0,member['DP']);self.assertEqual(85000,member['BP'])
        self.assertEqual(750000,member['returned']);self.assertEqual('withdrawn',member['status'])
        self.assertEqual(receipt,self.command('owner','withdraw_execute',{'withdrawal':ref}))
        with self.assertRaises(LabBlocked):self.command('owner','withdraw_execute',{'withdrawal':ref},key='double-return')

    def test_command_limits_and_credential_logs(self):
        contract=FixtureContract('a'*40,max_commands=1);session=self.store.create('limited',contract)
        self.store.seed(session.workspace,session.tokens['owner'],expected_generation=0,request_key='seed',contract_digest=contract.digest)
        self.store.command(session.workspace,session.tokens['U01'],generation=1,request_key='card',action='set_card',payload={'scenario':'success'})
        with self.assertRaises(Conflict):self.store.command(session.workspace,session.tokens['U01'],generation=1,request_key='signup',action='signup',payload={})
        evidence=json.dumps(self.store.evidence(session.workspace,session.tokens['owner']))
        for token in session.tokens.values():self.assertNotIn(token,evidence)

    def test_state_or_contract_tampering_is_detected(self):
        state=self.state();state['members']['U04']['BP']+=1
        with self.assertRaises(ValueError):assert_initial_state(state,self.contract)
        with self.assertRaises(ValueError):validate_state(state)
        state=self.state();state['products']['P02']['stock']=-1
        with self.assertRaises(ValueError):validate_state(state)

    def test_loopback_environment_and_source_fences(self):
        with self.assertRaises(ValueError):make_lab_server(self.store,'a'*40,host='0.0.0.0')
        with self.assertRaises(ValueError):LabClient('https://production.example.com',self.session,'owner',source_revision='a'*40)
        client=LabClient(self.server(revision='b'*40),self.session,'owner',source_revision='a'*40)
        with self.assertRaises(ValueError):client.seed()
        self.assertFalse(any(e['action']=='seed' for e in client.events))

    def test_http_roundtrip_roles_and_unknown_fields(self):
        client=LabClient(self.server(),self.session,'U04',source_revision='a'*40);client.generation=1
        status,result=client.command('purchase',{'product':'P02'},key='http-purchase')
        self.assertEqual(200,status);self.assertEqual(200000,result['value']['result']['plan']['card'])
        before=digest(self.state());status,_=client.command('purchase',{'product':'P01','cardNumber':'forbidden'},key='bad-field')
        self.assertEqual(400,status);self.assertEqual(before,digest(self.state()))

    def test_http_uncertainty_never_retries_or_logs_auth(self):
        client=LabClient('http://127.0.0.1:1234',self.session,'owner',source_revision='a'*40)
        client._opener=Mock();client._opener.open.side_effect=TimeoutError('private value')
        with self.assertRaises(LabTransportUncertain):client.call('POST','seed',{})
        self.assertEqual(1,client._opener.open.call_count)
        self.assertNotIn('private value',json.dumps(client.events));self.assertNotIn(self.session.tokens['owner'],json.dumps(client.events))

    def test_remote_protocol_absence_never_enables_seed(self):
        caps=dict(mode='isolated_server_review',cardProvider='simulator',operationalWrites=False,realCharges=False,realNotifications=False)
        self.assertFalse(remote_fixture_readiness(caps)['setup_allowed'])
        self.assertFalse(remote_fixture_readiness(dict(caps,testDataProtocol='hope-test-lab/v1'))['setup_allowed'])
        self.assertEqual('nonproduction_guards_not_verified',remote_fixture_readiness(dict(caps,realCharges=0))['reason'])

    def test_customer_report_keeps_historical_defects_and_never_promotes_model(self):
        prior={'sut_revision':'b'*40,'defects':[dict(id='old',status='OPEN',severity='S1')]}
        batch=dict(source_revision='a'*40,items=[],counts={'PASS':20})
        report=customer_template(batch,prior)
        self.assertIsNone(report['sut_revision']);self.assertEqual(1,report['summary']['unresolved_S1_S2'])
        self.assertEqual('OPEN_NOT_RETESTED',report['defects'][0]['status'])
        self.assertFalse(any(s['status']=='PASS' for s in report['scenarios']))

    def test_whole_batch_20_over_http_and_actual_customer_format_exports(self):
        def probe(items):return export(dict(source_revision='a'*40,items=items,counts={},evidence=[]),self.root/'reports')
        result=execute_batch('a'*40,report_probe=probe)
        self.assertEqual({'PASS':20,'FAIL':0,'BLOCKED':0},result['counts'])
        self.assertFalse(result['customer_site_verified'])
        self.assertTrue(result['local_admission']['admitted'])
        self.assertFalse(result['local_admission']['operational_authorization'])
        export(result,self.root/'reports')
        from openpyxl import load_workbook
        book=load_workbook(self.root/'reports/customer-report.xlsx',read_only=True)
        self.assertEqual(21,book['개발묶음20'].max_row);self.assertEqual(39,book['시나리오결과'].max_row)
        evidence=json.dumps(result)
        self.assertNotIn('Bearer ',evidence)

    def test_reviewed_batch_contract_cannot_drop_or_lower_critical_checks(self):
        from manager_bridge.hope_batch01_contract import contract,scenarios,EXPECTED,ADAPTER,VERSION
        from taesan_core.universal_qa import Adapter,run_scenarios
        from manager_bridge.qa_admission import admit_qa_report
        suite=scenarios();trusted=contract()
        report=run_scenarios(suite,[Adapter(ADAPTER,VERSION,lambda i:dict(actual=EXPECTED[i['item']],evidence={'scope':'fixture_only'}))],
            source_revision='a'*40,seed=20261116,required_coverage=[s.coverage_key for s in suite])
        for mutation in ('lower','remove'):
            altered=deepcopy(report)
            if mutation=='lower':
                altered['scenarios'][16]['priority']='P3';altered['scenarios'][16]['risk']['priority']='P3'
            else:del altered['scenarios'][16]
            altered['report_digest']=digest({k:v for k,v in altered.items() if k!='report_digest'})
            self.assertFalse(admit_qa_report(altered,expected_source_revision='a'*40,
                required_coverage=[s.coverage_key for s in suite],expected_contract=trusted)['admitted'])


    def test_declined_membership_only_retries_after_explicit_new_request(self):
        self.command('U01','set_card',{'scenario':'decline'},key='decline-card')
        first=self.command('U01','signup',key='first-request')
        self.command('U01','set_card',{'scenario':'success'},key='new-card')
        self.assertEqual(first,self.command('U01','signup',key='first-request'))
        self.assertEqual(0,self.state()['members']['U01']['DP'])
        self.assertEqual('active',self.command('U01','signup',key='explicit-known-failure-retry')['result']['status'])
        self.assertEqual(1,sum(c['kind']=='initial' for c in self.state()['cards']))

    def test_zero_principal_withdrawal_has_no_simulated_cash_return(self):
        requested=self.command('U05','withdraw_request')['result'];ref=requested['id']
        self.assertEqual(0,requested['ceiling'])
        self.command('finance','withdraw_review',{'withdrawal':ref})
        self.command('owner','withdraw_approve',{'withdrawal':ref})
        self.command('owner','withdraw_execute',{'withdrawal':ref})
        self.assertFalse(any(c['kind']=='withdrawal' for c in self.state()['cards']))
