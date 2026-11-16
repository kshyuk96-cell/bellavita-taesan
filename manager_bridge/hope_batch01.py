"""Execute the approved 20-item development batch against a local reference adapter.

Reports separate local verification from customer-site acceptance throughout.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
from taesan_core.hope_test_data import FixtureContract,fixture_data,ITEMS,assert_initial_state
from taesan_core.hope_tour_qa import allocation_oracle,refund_oracle
from .hope_lab_store import LabStore
from .hope_lab_transport import make_lab_server,LabClient
from .store import digest
from .hope_batch01_contract import EXPECTED,scenarios,contract as approved_contract,ADAPTER,VERSION
from .qa_admission import admit_qa_report
from taesan_core.universal_qa import Adapter,run_scenarios


class Harness:
    def __init__(self,store,origin,revision):
        self.store,self.origin,self.revision=store,origin,revision;self.runs=[]

    def new(self,label,*,example=False,max_commands=200):
        contract=FixtureContract(self.revision,example_bonus_order='BP_SP' if example else None,
                                 cancellation_fee_won=0 if example else None,max_commands=max_commands)
        session=self.store.create(label,contract)
        clients={role:LabClient(self.origin,session,role,source_revision=self.revision) for role in session.tokens}
        seeded=clients['owner'].seed(key='initial-seed')
        for client in clients.values():client.generation=seeded['generation']
        state=clients['owner'].state()['state'];assert_initial_state(state,contract)
        ctx=dict(contract=contract,session=session,clients=clients,initial=state)
        self.runs.append(ctx);return ctx

    @staticmethod
    def step(ctx,who,action,payload=None,*,key=None,status=200):
        actual,result=ctx['clients'][who].command(action,payload,key=key)
        if actual!=status:raise AssertionError('fixture command status: '+action+' '+str(actual))
        return result['value'] if status==200 else result

    @staticmethod
    def state(ctx):return ctx['clients']['owner'].state()['state']


def execute_batch(revision,*,report_probe=None):
    trusted_contract=approved_contract()
    results=[];contexts=[]
    with TemporaryDirectory(prefix='hope-batch01-') as directory:
        store=LabStore(Path(directory)/'fixture.sqlite');server=make_lab_server(store,revision)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        harness=Harness(store,'http://127.0.0.1:'+str(server.server_address[1]),revision)
        def check(number,expected,execute):
            if digest(expected)!=digest(EXPECTED[number]):raise ValueError('reviewed expectation changed')
            try:
                actual=execute();status='PASS' if digest(actual)==digest(expected) else 'FAIL';reason=None
            except Exception as error:
                actual=None;status='BLOCKED';reason=type(error).__name__
            row=dict(item=number,title=ITEMS[number-1],priority=trusted_contract['scenarios']['batch01-'+str(number).zfill(2)]['priority'],scope='local_reference_fixture',status=status,
                expected=expected,actual=actual,reason=reason,customer_site_verified=False)
            results.append(row)
        try:
            c=harness.new('environment')
            check(1,dict(environment='local_reference_fixture',operationalWrites=False,realCharges=False,realNotifications=False),
                lambda:{k:c['clients']['owner'].preflight()[k] for k in ('environment','operationalWrites','realCharges','realNotifications')})
            check(2,dict(generation=1,matched=True),lambda:dict(generation=c['clients']['owner'].state()['generation'],
                matched=assert_initial_state(harness.state(c),c['contract'])['matched']))
            def isolation():
                other=harness.new('separate-run');status,_=c['clients']['U04'].call('GET','state')
                # A valid identity presented to another workspace is rejected.
                wrong=LabClient(harness.origin,other['session'],'U04',source_revision=revision)
                wrong._token=c['session'].tokens['U04'];cross,_=wrong.call('GET','state')
                return dict(distinct=c['session'].workspace!=other['session'].workspace,own=status,cross=cross)
            check(3,dict(distinct=True,own=200,cross=403),isolation)
            def reset():
                ctx=harness.new('reset',example=True);harness.step(ctx,'U04','purchase',{'product':'P02'},key='purchase')
                ctx['clients']['owner'].seed(key='explicit-reset');now=ctx['clients']['owner'].state()
                stale,_=ctx['clients']['U04'].command('purchase',{'product':'P02'},key='old-generation')
                events=store.evidence(ctx['session'].workspace,ctx['session'].tokens['owner'])
                return dict(generation=now['generation'],initial_state_matches=digest(now['state'])==digest(ctx['initial']),
                            stale_status=stale,history_retained=any(e['kind']=='command' and e['generation']==1 for e in events))
            check(4,dict(generation=2,initial_state_matches=True,stale_status=409,history_retained=True),reset)
            check(5,{uid:dict(grade=grade,status='new',card='unregistered') for uid,grade in [('U01','hope'),('U02','happy'),('U03','premium')]},
                lambda:{uid:{k:harness.state(c)['members'][uid][k] for k in ('grade','status','card')} for uid in ('U01','U02','U03')})
            def roles():
                return dict(member_ids=list(c['clients']['U04'].state()['state']['members']),
                    owner_count=len(c['clients']['owner'].state()['state']['members']),
                    product_keys=list(c['clients']['product_admin'].state()['state']),
                    unauthorized=c['clients']['unauthorized'].call('GET','state')[0],
                    finance_seed=c['clients']['finance'].call('POST','seed',dict(expected_generation=1,
                        request_key='forbidden',contract_digest=c['contract'].digest))[0])
            check(6,dict(member_ids=['U04'],owner_count=10,product_keys=['products'],unauthorized=403,finance_seed=403),roles)
            check(7,dict(DP=600000,BP=150000,SP=100000),lambda:{k:harness.state(c)['members']['U04'][k] for k in ('DP','BP','SP')})
            check(8,{'U05':0,'U06':1000000,'U07':999999,'U07_BP':100001},lambda:{
                **{u:harness.state(c)['members'][u]['DP'] for u in ('U05','U06','U07')},'U07_BP':harness.state(c)['members']['U07']['BP']})
            def refund_data():
                m=harness.state(c)['members']['U09']
                return dict(principal=m['principal'],used=m['used'],BP=m['BP'],DP=m['DP'],
                    independent_ceiling=refund_oracle({**m,'signup_fee':25000}))
            check(9,dict(principal=1200000,used=450000,BP=85000,DP=750000,independent_ceiling=750000),refund_data)
            check(10,{'P01':('domestic',300000),'P02':('overseas',1000000),'P03':('culture',100000),'P04':('cruise',1000000)},
                lambda:{p:(v.get('kind',v['category']),v['price']) for p,v in harness.state(c)['products'].items()})
            check(11,dict(last_seat=1,sold_out=0,rooms={'inside':2,'balcony':1}),lambda:dict(
                last_seat=harness.state(c)['products']['P02']['stock'],sold_out=harness.state(c)['products']['P03']['stock'],
                rooms=harness.state(c)['products']['P04']['rooms']))
            def installments():
                ctx=harness.new('installment-data',example=True)
                booking=harness.step(ctx,'U04','purchase',{'product':'P04','room':'inside'})['result']
                return {k:booking[k] for k in ('deposit_gross','balance_gross','balance_due','payment_mode')}
            check(12,dict(deposit_gross=300000,balance_gross=700000,balance_due='2027-02-14',payment_mode='full_simulator_payment'),installments)
            def card_scenarios():
                ctx=harness.new('cards',example=True)
                harness.step(ctx,'U01','set_card',{'scenario':'unknown'})
                unknown=harness.step(ctx,'U01','signup')['result']['status']
                replay=harness.step(ctx,'U01','signup',key='different-request',status=409)['reason']
                before=len([r for r in harness.state(ctx)['cards'] if r['kind']=='initial'])
                harness.step(ctx,'owner','resolve',{'member':'U01','outcome':'success'})
                harness.step(ctx,'U02','set_card',{'scenario':'decline'});declined=harness.step(ctx,'U02','signup')['result']['status']
                after=harness.state(ctx)
                return dict(unknown=unknown,replay=replay,before_cards=before,initial_cards=len([r for r in after['cards'] if r['kind']=='initial']),
                    resolved_DP=after['members']['U01']['DP'],declined=declined,declined_DP=after['members']['U02']['DP'])
            check(13,dict(unknown='reconciliation_required',replay='reconciliation_required_no_replay',before_cards=0,
                initial_cards=1,resolved_DP=50000,declined='declined',declined_DP=0),card_scenarios)
            def policy():
                ctx=harness.new('policy-pending');before=digest(harness.state(ctx))
                bonus=harness.step(ctx,'U04','purchase',{'product':'P02'},status=409)['reason']
                cp=harness.step(ctx,'U04','corporate_purchase',status=409)['reason']
                suspension=harness.step(ctx,'U08','withdraw_request',status=409)['reason']
                return dict(bonus=bonus,corporate=cp,suspension=suspension,unchanged=before==digest(harness.state(ctx)))
            check(14,dict(bonus='bonus_order_policy_pending',corporate='corporate_policy_pending',
                suspension='suspension_withdrawal_policy_pending',unchanged=True),policy)
            def contract_fence():
                ctx=harness.new('contract-fence');before=digest(harness.state(ctx))
                status,_=ctx['clients']['owner'].call('POST','seed',dict(expected_generation=1,request_key='tamper',contract_digest='0'*64))
                return dict(status=status,unchanged=before==digest(harness.state(ctx)),contract_matches=ctx['clients']['owner'].state()['contract_digest']==ctx['contract'].digest)
            check(15,dict(status=409,unchanged=True,contract_matches=True),contract_fence)
            def grades():
                ctx=harness.new('signup-grades');actual={}
                for uid in ('U01','U02','U03'):
                    harness.step(ctx,uid,'set_card',{'scenario':'success'})
                    receipt=harness.step(ctx,uid,'signup',key='signup-'+uid)
                    repeated=harness.step(ctx,uid,'signup',key='signup-'+uid)
                    if receipt!=repeated:raise AssertionError('signup replay receipt changed')
                    actual[uid]=receipt['result']
                return actual
            check(16,{'U01':dict(status='active',paid=75000,principal=50000,signup_fee=25000,BP=25000),
                      'U02':dict(status='active',paid=150000,principal=100000,signup_fee=50000,BP=50000),
                      'U03':dict(status='active',paid=300000,principal=200000,signup_fee=100000,BP=100000)},grades)
            def purchase():
                ctx=harness.new('mixed-purchase',example=True);contexts.append(ctx)
                receipt=harness.step(ctx,'U04','purchase',{'product':'P02'},key='mixed')
                state=harness.state(ctx);plan=receipt['result']['plan']
                expected=allocation_oracle(dict(price=1000000,DP=600000,BP=150000,SP=100000,bp_limit_bps=1000,bonus_order='BP_SP'))
                return dict(plan=plan,independent_match=plan==expected,stock=state['products']['P02']['stock'],
                    bookings=len(state['bookings']),card_approvals=sum(r['kind']=='purchase' for r in state['cards']),
                    journal={r['bucket']:r['amount'] for r in receipt['journal_appended']})
            check(17,dict(plan=dict(DP=600000,BP=100000,SP=100000,card=200000),independent_match=True,stock=0,bookings=1,
                card_approvals=1,journal={'DP':-600000,'BP':-100000,'SP':-100000}),purchase)
            def cancellation():
                ctx=contexts[0];booking=next(iter(harness.state(ctx)['bookings']))
                ref=harness.step(ctx,'U04','cancel_request',{'booking':booking})['result']['id']
                harness.step(ctx,'finance','cancel_review',{'refund':ref});harness.step(ctx,'owner','cancel_approve',{'refund':ref})
                receipt=harness.step(ctx,'owner','cancel_execute',{'refund':ref},key='refund-once')
                harness.step(ctx,'owner','cancel_execute',{'refund':ref},key='refund-once')
                duplicate=harness.step(ctx,'owner','cancel_execute',{'refund':ref},key='refund-again',status=409)['reason']
                state=harness.state(ctx)
                return dict(balances={k:state['members']['U04'][k] for k in ('DP','BP','SP')},stock=state['products']['P02']['stock'],
                    booking=state['bookings'][booking]['status'],card_net=sum(r['amount'] for r in state['cards'] if r['kind'] in ('purchase','purchase_reversal')),
                    reversals=len(receipt['journal_appended']),original_links=all(r['reverses'] for r in receipt['journal_appended']),duplicate=duplicate)
            check(18,dict(balances=dict(DP=600000,BP=150000,SP=100000),stock=1,booking='cancelled',card_net=0,reversals=3,
                          original_links=True,duplicate='approved_refund_required'),cancellation)
            def withdrawal():
                ctx=harness.new('withdrawal',example=True)
                requested=harness.step(ctx,'U09','withdraw_request')['result'];ref=requested['id']
                stopped=harness.state(ctx)['members']['U09']['recurring'] is False
                harness.step(ctx,'finance','withdraw_review',{'withdrawal':ref});harness.step(ctx,'owner','withdraw_approve',{'withdrawal':ref})
                harness.step(ctx,'owner','withdraw_execute',{'withdrawal':ref},key='withdraw-once')
                harness.step(ctx,'owner','withdraw_execute',{'withdrawal':ref},key='withdraw-once')
                state=harness.state(ctx);member=state['members']['U09']
                card=[r for r in state['cards'] if r['kind']=='withdrawal']
                return dict(ceiling=requested['ceiling'],free_bonus_cash=requested['free_bonus_cash'],stopped=stopped,
                    DP=member['DP'],BP=member['BP'],returned=member['returned'],status=member['status'],
                    returns=len(card),original_source=card[0]['original_reference'])
            check(19,dict(ceiling=750000,free_bonus_cash=0,stopped=True,DP=0,BP=85000,returned=750000,
                status='withdrawn',returns=1,original_source='seed-principal-U09'),withdrawal)
            check(20,dict(customer_promoted=False,canonical_cases=38,word_created=True,excel_created=True),
                  lambda:report_probe(results) if report_probe else dict(customer_promoted=False,canonical_cases=38,word_created=False,excel_created=False))
            evidence=[]
            for ctx in harness.runs:
                evidence.append(dict(workspace=ctx['session'].workspace,contract=ctx['contract'].manifest,
                    initial_digest=digest(ctx['initial']),final=ctx['clients']['owner'].state(),
                    events=store.evidence(ctx['session'].workspace,ctx['session'].tokens['owner']),
                    transport=[event for client in ctx['clients'].values() for event in client.events]))
        finally:
            server.shutdown();thread.join(timeout=3);server.server_close()
    def observe(inputs):
        row=next(r for r in results if r['item']==inputs['item'])
        if row['status']=='BLOCKED':raise RuntimeError('local execution observation blocked')
        return dict(actual=row['actual'],evidence={'scope':'local_reference_fixture','item':row['item'],
            'source_revision':revision,'http_evidence_groups':len(evidence)})
    common=run_scenarios(scenarios(),[Adapter(ADAPTER,VERSION,observe)],source_revision=revision,
                         seed=20261116,required_coverage=[s.coverage_key for s in scenarios()])
    admission=admit_qa_report(common,expected_source_revision=revision,
        required_coverage=[s.coverage_key for s in scenarios()],expected_contract=trusted_contract)
    return dict(schema='hope-qa-batch01/v1',source_revision=revision,scope='local_reference_fixture',
        trusted_contract=trusted_contract,common_report=common,local_admission=admission,items=results,counts={s:sum(r['status']==s for r in results) for s in ('PASS','FAIL','BLOCKED')},
        evidence=evidence,customer_site_verified=False,operational_authorization=False)
