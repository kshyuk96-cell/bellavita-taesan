"""T01-T10 traceability and evidence admission, distinct from developer claims."""
import re
import uuid
from lxml import html
from taesan_core.hope_tour_qa import REQUIREMENTS, GLOSSARY, risk, REFERENCE_SHA256, POLICY_VERSION
from taesan_core.universal_qa import Adapter, Scenario, EvidenceUnavailable, run_scenarios, build_contract
from .qa_admission import admit_qa_report
from .store import digest
from .hope_review_client import ReviewUncertain

# Every full workflow has mandatory evidence; simulator checks do not replace PG/UI.
CASES={
 'signup':('T01',{'status':'active','DP':50000,'BP':25000,'paid':75000}),
 'identity-live':('T01',True),
 'monthly-replay':('T02',{'monthly_invoices':1,'DP':100000}),
 'payment-unknown':('T02',{'before':'pending_first_payment','after':'active','DP':50000}),
 'declined':('T02',{'status':'pending_first_payment','DP':0,'BP':0}),
 'product-cross-device':('T03',True),
 'booking-confirmed':('T04','confirmed'),
 'inventory-concurrent':('T04',True),
 'principal-first-declaration':('T05','DP'),
 'principal-first-runtime':('T05',True),
 'bp-limit-approved':('T06',True),
 'bonus-order-approved':('T06',True),
 'corporate-policy-approved':('T06',True),
 'refund-restoration':('T07',{'status':'cancelled','DP':100000,'BP':25000}),
 'refund-pg-ledger':('T07',True),
 'withdrawal-ceiling-runtime':('T08',True),
 'suspension-no-auto-refund':('T08',True),
 'member-admin-denied':('T09',403),
 'mfa-audit-privacy':('T09',True),
 'actual-pg':('T10',True),
 'real-devices':('T10',True),
 'backup-restore':('T10',True),
 'terms-expanded':('TERM',{key:True for key in GLOSSARY}),
 'mobile-help':('TERM',True)}


def scenarios():
    return [Scenario(key,'hope-review',requirement,key,risk(REQUIREMENTS[requirement][1]),
        {'case':key},expected,'고객 문서 T01~T10·§5~8의 기대조건; 개발사 complete 표시를 실행 증거로 사용하지 않음',
        '실제 신원/PG/브라우저와 격리 시뮬레이터 검사를 분리한 필수 증거 단위')
        for key,(requirement,expected) in CASES.items()]


def evaluate(observations, *, sut_revision, seed=20261116):
    def execute(inputs):
        observation=observations.get(inputs['case'])
        if observation is None:raise EvidenceUnavailable('not_executed_or_policy_not_approved')
        if type(observation) is not dict or observation.get('sut_revision')!=sut_revision:
            raise ValueError('stale observation rejected')
        if observation.get('scope') not in ('isolated_server_review_http','server_html_declaration'):
            raise ValueError('unsupported evidence source')
        # These externally verified capabilities cannot be inferred from a simulator.
        if inputs['case'] in ('actual-pg','identity-live','real-devices','backup-restore','refund-pg-ledger',
                              'bp-limit-approved','bonus-order-approved','corporate-policy-approved','mobile-help'):
            raise EvidenceUnavailable('independent_evidence_or_policy_approval_required')
        return dict(actual=observation['actual'],evidence={'scope':observation['scope'],
            'sut_revision':sut_revision,'steps':observation['steps']})
    adapters=[Adapter('hope-review','customer-observation-v1',execute)]
    suite=scenarios();contract=build_contract(suite,adapters)
    report=run_scenarios(suite,adapters,source_revision=sut_revision,seed=seed,required_coverage=list(CASES))
    report['reference']={'file_sha256':REFERENCE_SHA256,'policy_version':POLICY_VERSION,
        'policy_approval':'customer_reference_not_operational_approval'}
    report['scope']='isolated simulator observations and HTML declarations; full workflow acceptance requires all mandatory evidence'
    report['report_digest']=digest({k:v for k,v in report.items() if k!='report_digest'})
    admission=admit_qa_report(report,expected_source_revision=sut_revision,
                              required_coverage=list(CASES),expected_contract=contract)
    requirements={}
    for key,(label,priority,fb) in REQUIREMENTS.items():
        rows=[r for r in report['scenarios'] if r['group']==key]
        status='pass' if all(r['status']=='pass' for r in rows) else (
            'fail' if any(r['status']=='fail' for r in rows) else
            'blocked' if any(r['status']=='blocked' for r in rows) else 'unmeasured')
        requirements[key]=dict(label=label,priority=priority,fb_ids=fb,status=status,
            counts={state:sum(r['status']==state for r in rows) for state in ('pass','fail','blocked','unmeasured')})
    return dict(report=report,contract=contract,admission=admission,requirements=requirements)


def collect(client_factory, *, sut_revision):
    """Fixed synthetic workspace actions only; never run downloaded package code."""
    observations={};events=[];errors=[];caps={}
    def save(key,actual,steps,scope='isolated_server_review_http'):
        observations[key]=dict(actual=actual,steps=steps,scope=scope,sut_revision=sut_revision)
    def signup(client,scenario='success'):
        client.login('member');client.command('registerCard',{'scenario':scenario})
        fields=dict(grade='hope',consents={'terms':True,'privacy':True,'recurring':True,'marketing':False})
        client.command('signup',fields)
        return client.state()
    c=client_factory()
    try:
        caps=c.capabilities()
        rows=caps.get('coverage',{}).get('rows',[])
        fb13=next((r for r in rows if r.get('id')=='FB-13'),{})
        order=re.search(r'(DP|SP|BP|CP)\s*→',fb13.get('label',''))
        save('principal-first-declaration',order.group(1) if order else None,
             {'developer_label':fb13.get('label'),'developer_status':fb13.get('status')},'server_html_declaration')
        texts=[]
        for path in ('/review/','/review/features','/review/app'):
            status,raw=c.get(path)
            if status!=200:raise ReviewUncertain('html_not_observed')
            tree=html.fromstring(raw)
            for element in tree.xpath('//script|//style'):element.drop_tree()
            texts.append(re.sub(r'\s+','',tree.text_content()))
        text=''.join(texts)
        save('terms-expanded',{key:re.sub(r'\s+','',name+'('+key+')') in text for key,name in GLOSSARY.items()},
             {'pages':['/review/','/review/features','/review/app'],'method':'server HTML text; no browser visibility claim'},'server_html_declaration')
        state=signup(c)
        paid=sum(i['amount'] for i in state['state']['invoices'] if i.get('status')=='paid')
        save('signup',dict(status=state['state']['contracts'][0]['status'],DP=state['balances']['DP'],
                          BP=state['balances']['BP'],paid=paid),{'balances':state['balances'],'paid_total':paid})
        c.login('admin');c.command('runCycle');c.command('runCurrentCycle');state=c.state()
        save('monthly-replay',dict(monthly_invoices=sum(i.get('kind')=='monthly' for i in state['state']['invoices']),
            DP=state['balances']['DP']),{'balances':state['balances'],'invoice_kinds':[i.get('kind') for i in state['state']['invoices']]})
        c.login('member');c.command('runCycle',expected_status=403)
        save('member-admin-denied',403,{'method':'POST','action':'runCycle','authenticated_role':'member','status':403})
        state=c.state();c.command('book',{'expectedTotal':state['quote']['total']});state=c.state()
        booking=state['state']['bookings'][0]
        # Retain allocation observation only if server explicitly exposes priority.
        plan=booking.get('plan',{})
        if type(plan) is dict and type(plan.get('order')) is list:
            save('principal-first-runtime',plan['order'][0]=='DP',{'plan':plan})
        for subtype in ('deposit','balance'):
            state=c.state();invoice=next(i for i in state['state']['invoices'] if i.get('bookingId')==booking['id'] and i.get('subtype')==subtype)
            c.command('payInvoice',{'invoiceId':invoice['id']})
        state=c.state();save('booking-confirmed',state['state']['bookings'][0]['status'],
                             {'status':state['state']['bookings'][0]['status'],'realCharges':False})
        # Full cancellation after both installments; no assumed fee policy oracle.
        c.command('requestRefund',{'bookingId':booking['id'],'rooms':2});state=c.state()
        refund=state['state']['refunds'][-1]
        c.login('finance');c.command('reviewRefund',{'refundId':refund['id']})
        c.login('admin');c.command('approveRefund',{'refundId':refund['id']});c.command('executeRefund',{'refundId':refund['id']})
        c.login('member');state=c.state()
        save('refund-restoration',dict(status=state['state']['bookings'][0]['status'],DP=state['balances']['DP'],BP=state['balances']['BP']),
             {'balances':state['balances'],'refund_status':state['state']['refunds'][-1].get('status'),'provider':'simulator'})
    except (ReviewUncertain,KeyError,ValueError,StopIteration,TypeError):
        errors.append('main_flow_requires_reconciliation_or_adapter_update')
    finally:events.extend(c.events)
    for scenario,key in [('decline','declined'),('unknown','payment-unknown')]:
        client=client_factory()
        try:
            state=signup(client,scenario)
            before=state['state']['contracts'][0]['status']
            if scenario=='decline':
                save(key,dict(status=before,DP=state['balances']['DP'],BP=state['balances']['BP']),{'balances':state['balances'],'scenario':scenario})
            else:
                client.login('admin');client.command('reconcilePayment');state=client.state()
                save(key,dict(before=before,after=state['state']['contracts'][0]['status'],DP=state['balances']['DP']),
                     {'balances':state['balances'],'scenario':scenario,'automatic_retry':False})
        except (ReviewUncertain,KeyError,ValueError,TypeError):errors.append(key+'_not_verified')
        finally:events.extend(client.events)
    # No cookies/tokens, passwords, raw card fields or entire member records exported.
    return dict(observations=observations,transport_events=events,errors=errors,
        developer_claims=caps.get('coverage',{}),capabilities={k:caps.get(k) for k in
            ('mode','realCharges','operationalWrites','actualPgCardTests','browserRunVerified','productionIdentityVerified')})
