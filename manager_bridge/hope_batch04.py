"""Reviewed batch61–80 oracles. Shared fixture proof cannot authorize customer acceptance."""
from copy import deepcopy
from .hope_batch02 import glossary_observation
from .hope_batch03 import customer_report as previous_customer_report
from .hope_browser_contract import browser_plan
from .hope_shared_inventory import independent_stock_check,independent_domain_check
from .hope_tour_reporting import final_report
from .store import Conflict,digest
from .qa_admission import admit_qa_report
from taesan_core.universal_qa import Scenario,Risk,Adapter,run_scenarios,build_contract,EvidenceUnavailable
from taesan_core.hope_tour_qa import REQUIREMENTS

ADAPTER='hope-actual-batch04';VERSION='shared-fixture-persistent-manager-v1'
EXPECTED={61:dict(pinned=True,supplier=False),62:dict(separate=True,operationalTables=0),63:dict(soldOutDenied=True,restored=1),
64:dict(expired=1,uncertainBlocked=True,held=1),65:dict(successes=1,rejected=1,confirmed=1),
66:dict(executors=2,barrierReady=2,confirmed=1,rejected=1),67:dict(bookings=1,cardApprovals=1,stale=409),
68:dict(afterFirst=1,afterReplay=1,afterSecond=2,rebook=True),69:dict(depositRetry=409,balanceRetry=409,receipts=2),
70:dict(queryThenRefund=True,pending=0,restored=2,cancelWhileUnknownDenied=True),
71:dict(skip=True,noFallback=True),72:True,73:True,74:True,75:True,
76:dict(profileRecovered=True,workerProvisionDenied=True),77:dict(restartUncertain=True,replayDenied=True,executionCalls=0),
78:dict(recovered=True,unauthorizedDenied=True,tamperDetected=True,missingDetected=True,limitDenied=True),
79:dict(cleanStockIssues=0,cleanDomainIssues=0,faultsDetected=4),80:dict(customerRows=38,customerPass=0,realPG=False)}


def suite(source):
    return [Scenario('batch04-'+str(n),ADAPTER,'shared-inventory-payment' if n<=70 or n==79 else 'browser-persistent-manager',
        'batch04-'+str(n),Risk('P1' if 71<=n<=75 else 'P0','중복예약·재청구·증거 소실은 잘못된 인수를 초래','독립 실행·중단·응답불명·복원 경계에서 발생 가능','원거래·공유 저장소·사전 계약을 대조하고 재시험'),
        {'item':n,'source':source},expected,'승인된61–80 사전 기대값; available+sum(quantity-released)=capacity; 원장 각book 합계0; 반환원수단≤원결제',
        '공유 테스트 재고·실제 후보 도메인·영속 관리기·미검증 UI를 구분') for n,expected in EXPECTED.items()]


def contract(source):return build_contract(suite(source),[Adapter(ADAPTER,VERSION,lambda _:None)])


def evaluate(raw,source,trusted,host,inventory_pin,inventory_contract):
    if raw.get('schema')!='hope-actual-service-batch04/v1' or raw.get('source_revision')!=source or raw.get('inventory_digest')!=inventory_pin or raw.get('role')!='main':raise Conflict('source/inventory/executor binding mismatch')
    if raw.get('scope')!='actual_service_source_shared_test_inventory' or any(raw.get(k) is not False for k in ('realPG','operational_authorization','customer_acceptance_approved')):raise Conflict('scope promotion rejected')
    if trusted!=contract(source):raise Conflict('trusted contract changed')
    rows=raw.get('items',[]);required={61,62,63,64,65,67,68,69,70}
    if len(rows)!=9 or any(type(r.get('id')) is not int for r in rows) or {r['id'] for r in rows}!=required:raise Conflict('missing/duplicate source case')
    actual={r['id']:deepcopy(r.get('actual')) for r in rows};artifacts=raw['artifacts']
    actual.update({n:host.get(n) for n in (66,76,77,78)})
    plan=browser_plan(source);actual[71]=dict(skip=plan['status']=='NOT RUN',noFallback=plan['automatic_fallback'] is False)
    for n in (72,73,74,75):actual[n]=None
    from .hope_shared_inventory import validate_contract
    if validate_contract(inventory_contract,source)!=inventory_pin:raise Conflict('trusted inventory contract mismatch')
    stock=independent_stock_check(artifacts['inventory'],inventory_contract['slots']);domain=independent_domain_check(artifacts['cleanDomain'])
    for label,state in [('duplicate',artifacts.get('duplicate')),('partial',artifacts.get('partial'))]+[(r['slot']+'-'+stage,r.get(stage)) for r in artifacts.get('travelUnknown',[]) for stage in ('final','afterRefund')]:
        if state is None:domain.append('missing_domain_evidence:'+label)
        else:domain.extend(label+':'+issue for issue in independent_domain_check(state))
    faults=artifacts.get('faultInjection',[]);expected_faults={'free-stock-corruption','duplicate-debit','excess-refund','unbalanced-journal'}
    detected=[]
    for fault in faults:
        errors=independent_stock_check(fault['inventory']) if 'inventory' in fault else independent_domain_check(fault['domain'])
        detected.append(dict(id=fault['id'],detected=bool(errors),issues=errors,target_detected=fault.get('targetDetected')))
    detected_count=sum(r['detected'] for r in detected) if len(faults)==4 and {f['id'] for f in faults}==expected_faults else 0
    actual[79]=dict(cleanStockIssues=len(stock),cleanDomainIssues=len(domain),faultsDetected=detected_count)
    # Partial customer evidence must never be upgraded by the host export step.
    preview=customer_report(raw,dict(counts={},admission={},common_report={'scenarios':[]}),engine_revision='0'*40,prior={'defects':[]})
    actual[80]=dict(customerRows=len(preview['scenarios']),customerPass=preview['summary']['counts']['PASS'],realPG=raw['realPG'])
    def execute(inputs):
        n=inputs['item']
        if actual.get(n) is None:raise EvidenceUnavailable('supported_browser_not_available' if 72<=n<=75 else 'host_execution_evidence_missing')
        return dict(actual=actual[n],evidence=dict(scope='persistent_local_manager_http' if 76<=n<=78 else raw['scope'],source_revision=source,inventory_contract_digest=inventory_pin,item=n,artifact_digest=digest(artifacts)))
    report=run_scenarios(suite(source),[Adapter(ADAPTER,VERSION,execute)],source_revision=source,seed=20261116,required_coverage=['batch04-'+str(n) for n in EXPECTED])
    admission=admit_qa_report(report,expected_source_revision=source,required_coverage=['batch04-'+str(n) for n in EXPECTED],expected_contract=trusted)
    return dict(common_report=report,trusted_contract=trusted,admission=admission,browser_plan=plan,glossary=glossary_observation(artifacts['terms_pages']),fault_injection=detected,stock_issues=stock,domain_issues=domain,
        counts={s:sum(r['status']==s for r in report['scenarios']) for s in ('pass','fail','blocked','unmeasured')})


def customer_report(raw,result,*,engine_revision,prior):
    empty=dict(report=dict(counts=dict(executed=16),scenarios=[]),requirements={k:dict(label=v[0],status='unmeasured') for k,v in REQUIREMENTS.items()})
    report=final_report(empty,{},engine_revision=engine_revision,sut_revision=raw['source_revision']);report['defects']=deepcopy(prior.get('defects',[]))
    checks={r['inputs']['item']:r for r in result['common_report']['scenarios']}
    mapping={'PRD-04':66,'PRD-07':68,'PAY-06':67,'PAY-07':69,'PAY-08':68,'E2E-02':68,'E2E-04':70}
    for row in report['scenarios']:
        row['environment'].update(url='local-source://'+raw['source_revision']+'/review/',inventory='shared TEST SQLite; supplier not connected');row['deployed_revision']=None
        row['preconditions']['restoration']='전용 review 메모리 DB와 공유 TEST SQLite; 승인된 fixture 자원만'
        number=mapping.get(row['test_id']);check=checks.get(number)
        if check:row.update(status='FAIL' if check['status']=='fail' else 'BLOCKED',execution_steps=['고정 실제 후보 Worker·공유 TEST 재고·독립 정수/원장 대조'],actual=dict(ui=None,server=check['actual'],pg=None),evidence=check['evidence'],note='공유 테스트 adapter 부분 검증; 공급사·실제 PG·화면·정책 인수 증거 별도')
    for defect in report['defects']:
        if defect['id']=='LIMIT-REVIEW-INVENTORY-001':defect.update(status='TEST_ADAPTER_RETEST_MATCH_SUPPLIER_PENDING',retest='PARTIAL_SHARED_FIXTURE_MATCH',retest_source_revision=raw['source_revision'])
        elif defect['id']=='BUG-TERM-001':defect.update(status='HTML_RETEST_MATCH_BROWSER_PENDING',retest='BROWSER_NOT_RUN')
    states=('PASS','FAIL','BLOCKED','NOT RUN');counts={s:sum(r['status']==s for r in report['scenarios']) for s in states}
    report['summary'].update(counts=counts,executed=sum(r['execution_steps'] is not None for r in report['scenarios']),deployed_revision=None,execution_scope=raw['scope'],
        unique_observed_checks=sum(result['counts'].get(s,0) for s in ('pass','fail')),unresolved_S1_S2=sum(d.get('severity') in ('S1','S2') and d['status']!='CLOSED' for d in report['defects']),recommendation='인수 차단: 실제 공급사/PG/브라우저/정책 승인 미검증')
    report['summary']['business_results']={g:{s:sum(r['group']==g and r['status']==s for r in report['scenarios']) for s in states} for g in {r['group'] for r in report['scenarios']}}
    report['retest_regression']=dict(status='PARTIAL_SHARED_FIXTURE_AND_API',candidate_source_revision=raw['source_revision'],actual_pg=False,actual_browser=False,supplier_connected=False)
    report['actual_source_batch04']=dict(counts=result['counts'],admission=result['admission'],inventory_contract_digest=raw.get('inventory_digest'),runtime_provenance=raw.get('runtime_provenance'))
    return report
