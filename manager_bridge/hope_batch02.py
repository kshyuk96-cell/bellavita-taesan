"""Manager-owned oracles for pinned service-source evidence; no live-site promotion."""
from copy import deepcopy
from datetime import datetime, timezone
from lxml import html
from taesan_core.hope_tour_qa import GLOSSARY, REQUIREMENTS
from taesan_core.universal_qa import Adapter, Scenario, Risk, EvidenceUnavailable, build_contract, run_scenarios
from .qa_admission import admit_qa_report
from .store import Conflict, digest
from .hope_tour_reporting import final_report

ADAPTER='hope-actual-batch02'
VERSION='actual-source-sqlite-v1'
EXPECTED={
21:dict(domainFunctions=True),
22:dict(built=True,uid=65534,networkDenied=True,hostFsDenied=True,spawnDenied=True),
23:dict(sqlite=True,active=True),24:dict(forbidden=403,unchanged=True),
25:dict(productionDenied=True,attachDenied=True),26:dict(count=10,matched=True),
27:dict(DP=600000,BP=150000,SP=100000,sourceIntegrity=True,paidInvoices=12),
28:dict(count=4,lastSeat=1,soldOutDenied=True,roomTypes=2),
29:dict(issued=1200000,remaining=750000,manualDP=False,balanced=True),
30:dict(staleDenied=409,generation=2,archived=True,rollback=True),
31:dict(matched=True,tamperDetected=True),32:dict(memberDenied=403,financeDenied=403,anonymous=401,otherIsolated=True),
33:dict(hope=75000,happy=150000,premium=300000),
34:dict(DP=600000,BP=100000,SP=100000,card=200000,confirmed=True,nonzeroCardApprovals=1),
35:dict(DP=600000,BP=150000,SP=100000,cancelled=True,cardReturned=200000,lotOrigins=True,noIssues=True),
36:dict(cap=750000,returned=750000,bonusCash=0,stopped=True,withdrawn=True),
37:dict(html=True,api=True,journal=True,requestIds=True),38:True,
39:dict(runtimeDPFirst=True,scopeSeparated=True,termsExpanded=True),
40:dict(realPG=False,actualBrowser=False,customerApproved=False)}


def suite(source):
    return [Scenario('batch02-'+str(n),ADAPTER,'actual-source-flow' if 33<=n<=37 else 'actual-source-preparation',
        'batch02-'+str(n),Risk('P1' if n in (28,38,39,40) else 'P0',
        '준비·권한·금액 증거 오류는 잘못된 인수 판단을 초래', '경계·반복·불확실 상태에서 발생 가능',
        '고정 소스와 원거래 증거를 대조하고 재실행'),{'item':n,'source':source},
        dict(source=source,**expected) if n==21 else expected,
        '관리기 사전 고정 기대값; 결제 합계 600000+100000+100000+200000=1000000; 반환 max(0,1200000-450000)=750000',
        '승인된 21~40번 준비·업무·재시험 단위') for n,expected in EXPECTED.items()]


def contract(source):return build_contract(suite(source),[Adapter(ADAPTER,VERSION,lambda _:None)])


def glossary_observation(pages):
    if type(pages) is not list or {p.get('path') for p in pages}!={'/review/','/review/features','/review/app','/review/policy'}:
        raise Conflict('all four original HTML pages required')
    texts=[]
    for page in pages:
        if type(page.get('html')) is not str:raise Conflict('missing original HTML')
        root=html.fromstring(page['html'])
        for element in root.xpath('//script|//style|//template'):element.drop_tree()
        texts.append(''.join(root.text_content().split()))
    return {key:any(''.join((name+'('+key+')').split()) in text for text in texts) for key,name in GLOSSARY.items()}


def evaluate(raw,source,trusted_contract):
    if raw.get('schema')!='hope-actual-service-batch02/v1' or raw.get('source_revision')!=source:
        raise Conflict('source/schema mismatch')
    if raw.get('scope')!='actual_service_source_local_sqlite_simulator' or any(raw.get(k) is not False for k in ('realPG','operational_authorization','customer_acceptance_approved')):
        raise Conflict('unsupported execution scope')
    if trusted_contract!=contract(source):raise Conflict('manager contract changed')
    items=raw.get('items',[])
    if len(items)!=20 or any(type(r.get('id')) is not int for r in items) or {r['id'] for r in items}!=set(EXPECTED):
        raise Conflict('missing/duplicate/unknown development item')
    rows={r['id']:deepcopy(r) for r in items}
    terms=glossary_observation(raw.get('artifacts',{}).get('terms_pages'))
    if rows[39].get('actual') is not None:rows[39]['actual']['termsExpanded']=all(terms.values())
    # Child-supplied expected/status/counts are deliberately not authoritative.
    def execute(inputs):
        row=rows[inputs['item']]
        if inputs['item']==38 or row.get('actual') is None:raise EvidenceUnavailable(row.get('reason','observation_unavailable'))
        return dict(actual=row['actual'],evidence={'scope':raw['scope'],'source_revision':source,
            'virtual_clock':raw.get('virtual_clock'),'item':row['id'],'raw_artifact_digest':digest(raw['artifacts'])})
    adapters=[Adapter(ADAPTER,VERSION,execute)]
    report=run_scenarios(suite(source),adapters,source_revision=source,seed=20261116,required_coverage=['batch02-'+str(n) for n in EXPECTED])
    admission=admit_qa_report(report,expected_source_revision=source,required_coverage=['batch02-'+str(n) for n in EXPECTED],expected_contract=trusted_contract)
    return dict(common_report=report,trusted_contract=trusted_contract,admission=admission,glossary=terms,
        counts={s:sum(r['status']==s for r in report['scenarios']) for s in ('pass','fail','blocked','unmeasured')})


# Only partial evidence is mapped: final_report always requires UI/PG and approvals.
CUSTOMER_MAP={'MEM-01':33,'MEM-03':27,'MEM-07':36,'MEM-10':39,'PRD-02':28,
 'PRD-07':35,'PAY-01':34,'PAY-09':36,'SEC-01':32,'E2E-01':34,'E2E-02':35,'E2E-03':36}


def customer_report(raw,evaluation,*,engine_revision,prior=None):
    source=raw['source_revision'];byid={r['id']:r for r in raw['items']}
    empty={'report':{'counts':{'executed':19},'scenarios':[]},'requirements':{
        k:dict(label=v[0],priority=v[1],status='unmeasured') for k,v in REQUIREMENTS.items()}}
    report=final_report(empty,{},engine_revision=engine_revision,sut_revision=source)
    for row in report['scenarios']:
        row['environment'].update(url='local-source://'+source+'/review/',pg_mode='simulator',runtime='Node24 + local SQLite D1 compatibility adapter')
        row['preconditions']['restoration']='전용 인메모리 SQLite·새 검수 공간; 컨테이너 종료 시 폐기'
        row['deployed_revision']=None
        number=CUSTOMER_MAP.get(row['test_id'])
        if number is None:continue
        check=next(x for x in evaluation['common_report']['scenarios'] if x['id']=='batch02-'+str(number))
        row.update(status='FAIL' if check['status']=='fail' else 'BLOCKED',
            actual=dict(ui=None,server=byid[number].get('actual'),pg=None),
            tested_at=datetime.now(timezone.utc).isoformat(),execution_steps=['고정 실제 소스 Worker → 격리 SQLite → 실제 API/업무함수'],
            evidence=dict(scope=raw['scope'],source_revision=source,development_item=number),
            note='실제 소스의 비운영 부분 검증; 브라우저·실제 PG·정책 승인 증거 미충족')
        if row['test_id']=='MEM-10':row['actual']['server']=evaluation['glossary']
    report['defects']=deepcopy((prior or {}).get('defects',[]))
    for defect in report['defects']:
        is_terms=defect['id']=='BUG-TERM-001'
        defect.update(status='OPEN_RETEST_FAILED' if is_terms else 'API_RETEST_MATCH_UI_POLICY_PENDING',
            retest_source_revision=source,retest='FAIL' if is_terms else 'PARTIAL_API_MATCH',fixed_revision=None)
    states=('PASS','FAIL','BLOCKED','NOT RUN')
    report['summary'].update(counts={s:sum(r['status']==s for r in report['scenarios']) for s in states},
        executed=sum(r['execution_steps'] is not None for r in report['scenarios']),unique_observed_checks=19,
        deployed_revision=None,execution_scope=raw['scope'],recommendation='인수 차단: 필수 미검증·용어 결함·정책 승인 대기',
        unresolved_S1_S2=sum(d.get('severity') in ('S1','S2') and d['status']!='CLOSED' for d in report['defects']))
    report['summary']['business_results']={g:{s:sum(r['group']==g and r['status']==s for r in report['scenarios']) for s in states} for g in {r['group'] for r in report['scenarios']}}
    report['retest_regression']=dict(status='PARTIAL',fixed_revision=None,source_revision=source,actual_pg=False,actual_browser=False)
    report['actual_source_batch02']=dict(counts=evaluation['counts'],admission=evaluation['admission'],source_provenance=raw.get('source_provenance'),runtime_provenance=raw.get('runtime_provenance'))
    return report


def formula_report(raw):
    """Integer-only host oracles; never calls the target service's calculators."""
    artifacts=raw['artifacts'];signup=artifacts['signup'];cancel=artifacts['cancellation']
    after=cancel['after'];before=cancel['before'];withdrawal=artifacts['withdrawal']
    def record(key,expression,variables,expected,actual,condition):
        return dict(id=key,expression=expression,variables=variables,units='KRW / point (1:1 TEST assumption)',
            calculation_order=expression,rounding='integer exact; no fractional case in this batch',
            condition=condition,policy_approval='customer example only; operational approval pending',
            expected=expected,actual=actual,status='PASS' if type(actual) is type(expected) and actual==expected else 'FAIL')
    records=[]
    for grade,monthly,fee in [('hope',50000,25000),('happy',100000,50000),('premium',200000,100000)]:
        records.append(record('signup-'+grade,'monthly+signup_fee',dict(monthly=monthly,signup_fee=fee),monthly+fee,signup[grade]['paid'],'initial simulator approval'))
        records.append(record('principal-'+grade,'monthly (signup fee excluded)',dict(monthly=monthly,signup_fee=fee),monthly,signup[grade]['DP'],'paid initial invoice'))
    money={k:after['balances'][k]-before['balances'][k] for k in ('DP','BP','SP')}
    card=sum(i['amount'] for i in before['invoices'] if i['kind']=='travel' and i['status']=='paid')
    records.append(record('mixed-total','DP+BP+SP+card',dict(**money,card=card),1000000,sum(money.values())+card,'U04/P02 one booking, no cancellation fee'))
    for book,expected in [('DP',600000),('BP',100000),('SP',100000)]:
        records.append(record('restored-'+book,'after_cancel-before_cancel',dict(before=before['balances'][book],after=after['balances'][book]),expected,money[book],'R01 full cancellation; zero fee TEST policy'))
    executed=[r for r in withdrawal['principalRecords'] if r.get('type')=='executed' and r.get('status')=='simulated_returned']
    records.append(record('principal-cap','max(0,paid-used)',dict(paid=1200000,used=450000,excluded_free_BP=85000),max(0,1200000-450000),sum(r['amount'] for r in executed),'U09 proven paid principal only'))
    return dict(schema='hope-batch02-formulas/v1',source_revision=raw['source_revision'],records=records,
        missing_boundary_coverage=['fractional rounding','nonzero cancellation fees','CP policy','BP/SP relative order approval'],
        operational_authorization=False)
