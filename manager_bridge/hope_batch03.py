"""Batch 41–60 manager oracles and local central-manager integration checks."""
from copy import deepcopy
from pathlib import Path
from .hope_browser_contract import browser_plan
from lxml import html
from .hope_batch02 import glossary_observation
from .hope_tour_reporting import final_report
from taesan_core.hope_tour_qa import REQUIREMENTS
from .store import Conflict,digest
from .qa_admission import admit_qa_report
from taesan_core.universal_qa import Scenario,Risk,Adapter,build_contract,run_scenarios,EvidenceUnavailable

ADAPTER='hope-actual-batch03';VERSION='actual-source-terms-manager-v1'
EXPECTED={41:dict(matched=True),42:dict(matched=True),43:dict(nativeDetails=9,summary=9,focusCSS=True),
44:dict(unsupportedSkipped=True,noFallback=True),45:True,46:True,
47:dict(originalMissing=True,candidateAllPresent=True),
48:dict(revokedDenied=True,immutableRelease=True,workerProvisionDenied=True),
49:dict(httpIntake=True,claim=True,sourceBound=True),
50:dict(bindingMutationsBlocked=5),51:dict(duplicateSameJob=True,projectConcurrentDenied=True,replayDenied=True),
52:dict(cancelUncertain=True,replayDenied=True,localChildStopped=True),
53:dict(wordRoundTrip=True,excelRoundTrip=True,unauthorizedDenied=True,immutable=True),
54:dict(missingCoverageBlocked=True,expectedTamperBlocked=True),
55:dict(U05=dict(DP=0,BP=0,SP=0,card=1000000),U06=dict(DP=1000000,BP=0,SP=0,card=0),U07=dict(DP=999999,BP=1,SP=0,card=0)),
56:dict(count=9,matched=True),57:dict(declinedNoLoss=True,duplicatesSingleBooking=True,duplicateConflict=409),
58:dict(secondStatus=409,pendingBefore=1,pendingAfter=0),
59:dict(sameWorkspaceMaxOne=True,crossWorkspaceGlobalMaxOne=True),
60:dict(restored=True,rebook=True,staleDenied=409)}


def suite(source):
    return [Scenario('batch03-'+str(n),ADAPTER,'service-edge-flows' if n>=55 else 'terms-and-manager',
        'batch03-'+str(n),Risk('P1' if n in (41,42,43,44,45,46,47) else 'P0',
        '누락된 권한·금액·인수 증거는 고객 오판과 중복 처리 위험',
        '서로 다른 실행·세대·경계·동시 요청에서 발생 가능',
        '고정 후보와 개별 실행 증거를 보존하고 재시험'),{'item':n,'source':source},expected,
        '고객 문서·승인된41–60 사전 기대값. BP=min(잔액,상품가-DP,floor(상품가×등급상한/10000)); 원 단위 정수',
        '개발항목별 상태; 브라우저미검증·전역재고미연결을 통과로 간주하지 않음') for n,expected in EXPECTED.items()]


def contract(source):return build_contract(suite(source),[Adapter(ADAPTER,VERSION,lambda _:None)])


def evaluate(raw,source,trusted,host,baseline):
    if raw.get('schema')!='hope-actual-service-batch03/v1' or raw.get('source_revision')!=source or raw.get('scope')!='actual_service_source_local_sqlite_simulator':raise Conflict('actual-source binding mismatch')
    if any(raw.get(k) is not False for k in ('realPG','operational_authorization','customer_acceptance_approved')):raise Conflict('invalid scope promotion')
    if contract(source)!=trusted:raise Conflict('trusted contract mismatch')
    expected_ids={41,42,43,55,56,57,58,59,60}
    rows=raw.get('items',[])
    if len(rows)!=len(expected_ids) or any(type(r.get('id')) is not int for r in rows) or {r['id'] for r in rows}!=expected_ids:raise Conflict('missing/duplicate source observations')
    actual={r['id']:deepcopy(r.get('actual')) for r in rows};artifacts=raw.get('artifacts',{})
    terms=glossary_observation(artifacts.get('terms_pages'))
    original=glossary_observation(baseline['artifacts']['terms_pages'])
    if baseline.get('source_revision')==source:raise Conflict('original and candidate source must differ')
    actual[41]=dict(matched=all(terms[k] for k in ('DP','BP','SP','CP')))
    actual[42]=dict(matched=all(terms[k] for k in ('PG','API','CRM','QA','KPI')))
    pages=artifacts['terms_pages'];roots=[html.fromstring(p['html']) for p in pages]
    if actual[43] is not None:
        actual[43]['nativeDetails']=min(len(r.xpath('//details[@class="review-term"]/summary')) for r in roots)
        actual[43]['summary']=min(len(r.xpath('//details[@class="review-term"]/p')) for r in roots)
    actual[47]=dict(originalMissing=not all(original.values()),candidateAllPresent=all(terms.values()))
    plan=browser_plan(source)
    actual[44]=dict(unsupportedSkipped=plan['status']=='NOT RUN' and plan['preview_started'] is False,noFallback=plan['automatic_fallback'] is False)
    actual.update({n:host.get(n) for n in range(48,55)})
    # Browser rows have no accepted observation transport in this profile.
    actual[45]=actual[46]=None
    matrix=artifacts.get('bpMatrix',[]);caps={'hope':{'domestic':1500,'overseas':1000,'culture':5000},'happy':{'domestic':2000,'overseas':1500,'culture':7000},'premium':{'domestic':2500,'overseas':1800,'culture':10000}}
    keys={(r['grade'],r['category']) for r in matrix}
    required={(g,c) for g in caps for c in caps[g]}
    matched=keys==required and len(matrix)==9
    for row in matrix:
        if any(type(row.get(k)) is not int or row[k]<0 for k in ('total','DP','BP','SP','card')):matched=False;continue
        expected=min(row['total']-row['DP'],row['total']*caps[row['grade']][row['category']]//10000)
        matched &= row['BP']==expected and sum(row[k] for k in ('DP','BP','SP','card'))==row['total']
    actual[56]=dict(count=len(matrix),matched=bool(matched))
    def execute(inputs):
        n=inputs['item']
        if actual.get(n) is None:raise EvidenceUnavailable('supported_browser_not_available' if n in (45,46) else 'missing_execution_evidence')
        return dict(actual=actual[n],evidence=dict(scope='local_manager_http' if 48<=n<=54 else raw['scope'],
            source_revision=source,raw_artifact_digest=digest(artifacts),item=n))
    report=run_scenarios(suite(source),[Adapter(ADAPTER,VERSION,execute)],source_revision=source,seed=20261116,required_coverage=['batch03-'+str(n) for n in EXPECTED])
    admission=admit_qa_report(report,expected_source_revision=source,required_coverage=['batch03-'+str(n) for n in EXPECTED],expected_contract=trusted)
    return dict(common_report=report,trusted_contract=trusted,admission=admission,glossary=terms,baseline_glossary=original,
        browser_plan=plan,counts={s:sum(r['status']==s for r in report['scenarios']) for s in ('pass','fail','blocked','unmeasured')})


def customer_report(raw,result,engine_revision,prior):
    empty=dict(report=dict(counts=dict(executed=18),scenarios=[]),requirements={k:dict(label=v[0],status='unmeasured') for k,v in REQUIREMENTS.items()})
    report=final_report(empty,{},engine_revision=engine_revision,sut_revision=raw['source_revision'])
    report['defects']=deepcopy(prior.get('defects',[]))
    for row in report['scenarios']:
        row['environment'].update(url='local-source://'+raw['source_revision']+'/review/',pg_mode='simulator')
        row['deployed_revision']=None
        row['preconditions']['restoration']='전용 인메모리 SQLite 검수 공간; 컨테이너 종료 시 폐기'
    report['summary'].update(deployed_revision=None,execution_scope=raw['scope'])
    mapping={'MEM-10':47,'PAY-02':55,'PAY-03':55,'PAY-04':56,'PAY-05':57,'PAY-06':57,'PAY-07':58,'PRD-04':59,'PRD-07':60,'E2E-02':60,'E2E-04':58}
    checks={r['inputs']['item']:r for r in result['common_report']['scenarios']}
    for row in report['scenarios']:
        n=mapping.get(row['test_id'])
        if n is None:continue
        check=checks[n];row.update(status='FAIL' if check['status']=='fail' else 'BLOCKED',actual=dict(ui=None,server=check['actual'],pg=None),
            execution_steps=['실제 후보 소스 Worker·격리 SQLite·독립 관리기 기대값 대조'],evidence=check['evidence'],
            note='후보 소스의 부분 검증; 고객 UI/정책/실제 PG 인수 조건 별도',defect_ids=[])
    for defect in report['defects']:
        if defect['id']=='BUG-TERM-001':defect.update(status='HTML_RETEST_MATCH_BROWSER_PENDING',retest='PARTIAL_HTML_MATCH',retest_source_revision=raw['source_revision'])
    report['defects'].append(dict(id='LIMIT-REVIEW-INVENTORY-001',scenario_ids=['PRD-04'],severity='S1',priority='P0',
        status='OPEN',kind='isolated_review_scope_limit_not_production_oversell_proof',
        symptom='검수 공간별 재고 독립: 전역 마지막1석 공유 보호를 검증할 수 없음',actual=raw['artifacts'].get('inventory'),
        expected='승인된 공유 비운영 재고에서 전체 확정합계1 이하',owner=None,due_date=None,fixed_revision=None,retest='FAIL'))
    states=('PASS','FAIL','BLOCKED','NOT RUN')
    report['summary'].update(browser_plan=plan,counts={s:sum(r['status']==s for r in report['scenarios']) for s in states},
        executed=sum(r['execution_steps'] is not None for r in report['scenarios']),unique_observed_checks=sum(result['counts'][s] for s in ('pass','fail')),
        unresolved_S1_S2=sum(d.get('severity') in ('S1','S2') for d in report['defects']),recommendation='인수 차단: 전역재고·브라우저·실제PG·정책 미검증')
    report['summary']['business_results']={g:{s:sum(r['group']==g and r['status']==s for r in report['scenarios']) for s in states} for g in {r['group'] for r in report['scenarios']}}
    report['summary']['execution_basis_pass_rate_percent']=0 if report['summary']['counts']['FAIL'] else None
    report['retest_regression']=dict(status='PARTIAL_HTML_AND_API',candidate_source_revision=raw['source_revision'],browser_verified=False,actual_pg_verified=False,original_defect_ids=[d['id'] for d in report['defects']])
    report['actual_source_batch03']=dict(counts=result['counts'],admission=result['admission'],source_provenance=raw.get('source_provenance'),runtime_provenance=raw.get('runtime_provenance'))
    return report
