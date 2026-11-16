"""Batch81–100: manager-owned recovery, calculation and customer evidence oracles."""
from copy import deepcopy
from .hope_browser_contract import browser_plan
from .hope_batch02 import glossary_observation
from .hope_shared_inventory import independent_stock_check,independent_domain_check,validate_contract
from .hope_partial_oracle import inspect_rounding
from .hope_persistent_review import business_pin
from .hope_customer_coverage import coverage_contract,assess,coverage_probes,CAPABILITIES
from .hope_tour_reporting import final_report
from .store import Conflict,digest
from .qa_admission import admit_qa_report
from taesan_core.hope_tour_qa import REQUIREMENTS
from taesan_core.universal_qa import Scenario,Risk,Adapter,run_scenarios,build_contract,EvidenceUnavailable

ADAPTER='hope-actual-batch05';VERSION='persistent-recovery-v1'
EXPECTED={81:dict(workspaces=9,businessPersistent=True),82:dict(sourceRejected=409,taskRejected=409,generationRejected=409,foreignRejected=404),
83:dict(readonlyQueries=6,memberReconcileDenied=403,noReplay=True),84:dict(existingRestored=2,unknownHeld=True),
85:dict(absentReleased=3,replaysDenied=3,activeExecutorDenied=409),86:dict(interrupted=5,stopped=5,recovered=5),
87:dict(executors=2,sameRevision=True,winningWrites=1,losingWrites=1,cardApprovals=1,journals=1),
88:dict(retryDenied=409,requeryDenied=409,cancelled=True,cardApprovals=1,cardReturned=200000),
89:dict(fullDuplicate=True,positiveDuplicate=True,positiveBalance=True),90:dict(detected=3,independentDetected=3,cleanIssues=0),
91:dict(skip=True,noFallback=True),92:True,93:True,94:True,
95:dict(rows=38,partialBlocked=True,customerPass=0),96:dict(rejected=10),
97:dict(total=1000011,rooms=2,sliceTotal=1000011,independentMatch=True),
98:dict(repeatDenied=409,firstUnpaid=100005,cardReturned=100005,stock=2,independentMatch=True),
99:dict(unapprovedNotAdmitted=True,policyMissing=True),100:dict(customerRows=38,customerPass=0,admitted=False)}


def suite(source):
    return [Scenario('batch05-'+str(n),ADAPTER,'persistent-source-recovery' if n<=90 else 'evidence-and-rounding','batch05-'+str(n),
        Risk('P1' if 91<=n<=94 else 'P0','중단 재실행·초과/과소반환·누락 증거는 금액과 인수 판단에 영향','여러 실행자·중간 종료·부분취소·증거 매핑 경계에서 발생 가능','동일 source/task/generation 원거래를 조회해 대조'),
        {'item':n,'source':source},expected,'승인된81–100 사전 기대값; source record + stopped executor certificate; exact rational largest remainder TEST oracle',
        '실제 코드와 파일DB의 중단/복구·CAS·하위 증거 상태; 미검증 UI/정책은 전체 통과가 아님') for n,expected in EXPECTED.items()]


def contract(source):return build_contract(suite(source),[Adapter(ADAPTER,VERSION,lambda _:None)])


def evidence_binding(source,trusted):return dict(project='hope-tour',task='hope-batch05-source-candidate',source_revision=source,test_contract_digest=digest(trusted),adapter_release=VERSION,generation=1)


def validate_execution_reports(entries,source,task,inv_pin,biz_pin):
    names=[]
    for report,role,interrupted in entries:
        receipt=report.get('stop_certificate',{});runtime=report.get('runtime_provenance',{})
        schema='hope-persistent-interruption/v1' if interrupted else 'hope-actual-service-batch05/v1'
        if report.get('schema')!=schema or report.get('role')!=role or report.get('source_revision')!=source or receipt.get('source_revision')!=source or receipt.get('task')!=task or receipt.get('container')!=runtime.get('container_name') or receipt.get('container_absent') is not True or receipt.get('process_exited') is not True or runtime.get('process_exited') is not True:raise Conflict('executor source/role or stop proof mismatch')
        if interrupted:
            if report.get('exit_code')!=86 or report.get('interrupted') is not True:raise Conflict('expected process interruption absent')
        elif report.get('inventory_digest')!=inv_pin or report.get('business_digest')!=biz_pin or any(report.get(k) is not False for k in ('realPG','supplier_connected','operational_authorization','customer_acceptance_approved')):raise Conflict('executor data contract or scope mismatch')
        names.append(runtime['container_name'])
    if len(set(names))!=len(names):raise Conflict('executor observation reused')


def evaluate(raw,source,trusted,inv_contract,biz_contract):
    if trusted!=contract(source):raise Conflict('manager contract changed')
    inv_pin=validate_contract(inv_contract,source);biz_pin=business_pin(biz_contract,source)
    if (raw.get('schema')!='hope-actual-service-batch05/v1' or raw.get('role')!='main' or raw.get('source_revision')!=source or raw.get('inventory_digest')!=inv_pin or raw.get('business_digest')!=biz_pin):raise Conflict('persistent source/profile mismatch')
    if raw.get('scope')!='actual_source_persistent_test_recovery' or any(raw.get(k) is not False for k in ('realPG','supplier_connected','operational_authorization','customer_acceptance_approved')):raise Conflict('scope promotion rejected')
    rows=raw.get('items',[])
    if len(rows)!=4 or any(type(r.get('id')) is not int for r in rows) or {r['id'] for r in rows}!={89,90,97,98}:raise Conflict('source checks missing/duplicate')
    actual={r['id']:deepcopy(r['actual']) for r in rows};a=raw['artifacts'];execution=raw['recovery_execution'];setup=execution['setup'];recovered=execution['recovery']['artifacts']['recovery'];safety=a['safety']
    normal=[setup,execution['recovery'],execution['unknown'],*execution['race']]
    stopped=[execution[p] for p in ('before-hold','after-hold','before-store','after-store','before-response')]
    validate_execution_reports([(raw,'main',False),(setup,'setup',False),(execution['recovery'],'recover',False),(execution['unknown'],'complete-unknown',False),*[(r,role,False) for r,role in zip(execution['race'],('race-a','race-b'))],*[(execution[phase],'crash-'+phase,True) for phase in ('before-hold','after-hold','before-store','after-store','before-response')]],source,biz_contract['task'],inv_pin,biz_pin)
    actual[81]=dict(workspaces=len(a['bindings']),businessPersistent={b['workspace'] for b in a['bindings']}=={b['workspace'] for b in setup['artifacts']['bindings']} and raw['runtime_provenance'].get('persistent_business_mount') is True and all(r['artifacts'].get('keyContinuity') is True for r in normal))
    actual[82]=dict(sourceRejected=safety['sourceMismatch'],taskRejected=safety['taskMismatch'],generationRejected=safety['stale'],foreignRejected=safety['foreign'])
    actual[83]=dict(readonlyQueries=sum(r['readonly'] is True and r['beforeRevision']==r['afterRevision']==r['query']['data']['revision'] for r in recovered),memberReconcileDenied=safety['memberReconcile'],noReplay=all(r['query']['data']['replayAllowed'] is False for r in recovered))
    existing=[r for r in recovered if r['label'] in ('after-store','before-response')];unknown=next(r for r in recovered if r['label']=='restarted-unknown')
    actual[84]=dict(existingRestored=sum(r['reconciliation']['data']['bookingId']==r['final']['bookings'][0]['id'] and len(r['final']['bookings'])==1 for r in existing),unknownHeld=unknown['query']['data']['state']=='payment_pending_query' and next(s for s in execution['recovery']['artifacts']['inventory']['slots'] if s['slot']=='restarted-unknown')['available']==0)
    absent=[r for r in recovered if r['label'] in ('before-hold','after-hold','before-store')]
    stocks={s['slot']:s for s in execution['recovery']['artifacts']['inventory']['slots']}
    actual[85]=dict(absentReleased=sum(r['query']['data']['state']=='known_absent' and not r['final']['bookings'] and stocks[r['label']]['available']==1 for r in absent),replaysDenied=sum(r['replayStatus']==409 for r in absent),activeExecutorDenied=safety['activeExecutorDenied'])
    actual[86]=dict(interrupted=sum(r.get('interrupted') is True and r.get('exit_code')==86 and r.get('checkpoint',{}).get('phase')==r['role'].removeprefix('crash-') for r in stopped),stopped=sum(r['stop_certificate']['container_absent'] is True for r in stopped),recovered=sum(r['reconciliation']['status']==200 for r in recovered if r['label']!='restarted-unknown'))
    barriers=execution['cas_barrier'];race=execution['race'][0]['artifacts']['race']['final'];booking=race['bookings'][0]
    actual[87]=dict(executors=len(execution['race']),sameRevision=len({b['revision'] for b in barriers})==1 and len({b['request_id'] for b in barriers})==1 and len(barriers)==2,winningWrites=sum(b['changes']==1 for b in barriers),losingWrites=sum(b['changes']==0 for b in barriers),cardApprovals=sum(e['type']=='succeeded' and e['amount']>0 for e in race['travelEvents']),journals=sum(j['sourceKey']=='booking:'+booking['id'] for j in race['ledger']))
    u=execution['unknown']['artifacts']['unknownRecovery'];final=u['afterRefund']
    actual[88]=dict(retryDenied=u['retry'],requeryDenied=u['requery'],cancelled=all(b['status']=='cancelled' for b in final['bookings']),cardApprovals=sum(e['type']=='succeeded' and e['amount']>0 for e in final['travelEvents']),cardReturned=sum(e['amount'] for e in final['travelEvents'] if e['type']=='refunded'))
    injected=a['faultInjection'];expected_faults={'duplicate-source','positive-duplicate-debit','duplicate-event','excess-refund','missing-journal'}
    if len(injected)!=5 or {f['id'] for f in injected}!=expected_faults:raise Conflict('fault evidence missing or duplicate')
    fault_results=[dict(id=f['id'],target_detected=bool(f['targetIssues']),independent_issues=independent_domain_check(f['domain'])) for f in injected]
    actual[90]=dict(actual[90],independentDetected=sum(bool(f['independent_issues']) for f in fault_results if f['id'] in ('duplicate-event','excess-refund','missing-journal')))
    rounding=inspect_rounding(a['rounding']);actual[97]['independentMatch']=rounding['slices_match'];actual[98]['independentMatch']=all(r['status']=='PASS' for r in rounding['records'])
    issues=independent_stock_check(a['inventory'],inv_contract['slots'])
    for label,state in [('audit',a['cleanAudit']),('race',race),('unknown',final),('partial-final',a['rounding']['afterSecond']),('partial-first',a['rounding']['afterFirst'])]:issues.extend(label+':'+i for i in independent_domain_check(state))
    # Formula/source checks fail closed even if child reported PASS.
    if issues:actual[98]['independentMatch']=False
    plan=browser_plan(source);actual[91]=dict(skip=plan['status']=='NOT RUN',noFallback=plan['automatic_fallback'] is False)
    for n in (92,93,94):actual[n]=None
    binding=evidence_binding(source,trusted);actual.update(coverage_probes(binding))
    coverage=build_customer_evidence(raw,binding,actual,issues)
    actual[100]=dict(customerRows=len(coverage['rows']),customerPass=coverage['counts']['PASS'],admitted=coverage['admitted'])
    def execute(inputs):
        n=inputs['item']
        if actual.get(n) is None:raise EvidenceUnavailable('supported_browser_not_available')
        return dict(actual=actual[n],evidence=dict(scope=raw['scope'],source_revision=source,business_contract_digest=biz_pin,inventory_contract_digest=inv_pin,item=n,artifact_digest=digest(a)))
    report=run_scenarios(suite(source),[Adapter(ADAPTER,VERSION,execute)],source_revision=source,seed=20261116,required_coverage=['batch05-'+str(n) for n in EXPECTED])
    admission=admit_qa_report(report,expected_source_revision=source,required_coverage=['batch05-'+str(n) for n in EXPECTED],expected_contract=trusted)
    return dict(common_report=report,trusted_contract=trusted,counts={s:sum(r['status']==s for r in report['scenarios']) for s in ('pass','fail','blocked','unmeasured')},admission=admission,browser_plan=plan,coverage=coverage,customer_contract=coverage_contract(binding),rounding=rounding,fault_injection=fault_results,issues=issues,glossary=glossary_observation(a['terms_pages']))


def build_customer_evidence(raw,binding,actual,issues):
    records=[]
    def add(row,check,scope,value,passed=True):records.append(dict(binding=deepcopy(binding),row=row,check=check,scope=scope,status='pass' if passed else 'fail',actual=value))
    add('PAY-06','duplicate_payment','actual_source_api',actual[87],actual[87]==EXPECTED[87])
    add('PAY-06','duplicate_booking','actual_source_api',actual[84],actual[84]==EXPECTED[84])
    add('PAY-06','ledger','independent_ledger',issues,not issues)
    add('PAY-07','unknown_payment','actual_source_api',actual[88],actual[88]==EXPECTED[88]);add('PAY-07','ledger','independent_ledger',issues,not issues)
    for row,check in [('PAY-08','partial_refund'),('E2E-02','refund_flow')]:
        add(row,check,'actual_source_api',actual[98],actual[98]==EXPECTED[98]);add(row,'ledger','independent_ledger',issues,not issues);add(row,'inventory','shared_test_inventory',{'restored':actual[98]['stock']},actual[98]['stock']==2)
    add('E2E-04','recovery_api','actual_source_api',actual[86],actual[86]==EXPECTED[86])
    terms=glossary_observation(raw['artifacts']['terms_pages']);add('MEM-10','terms_html','server_html',terms,all(terms.values()))
    return assess(coverage_contract(binding),records,binding=binding)


def customer_report(raw,result,engine_revision,prior):
    empty=dict(report=dict(counts=dict(executed=17),scenarios=[]),requirements={k:dict(label=v[0],status='unmeasured') for k,v in REQUIREMENTS.items()})
    report=final_report(empty,{},engine_revision=engine_revision,sut_revision=raw['source_revision']);verdicts={r['id']:r for r in result['coverage']['rows']}
    for row in report['scenarios']:
        v=verdicts[row['test_id']];row.update(status=v['status'],deployed_revision=None,evidence=dict(source_revision=raw['source_revision'],observations=v['observations'],missing=v['missing'],customer_contract_digest=result['coverage']['contract_digest']),actual=dict(ui=None,server=[r['actual'] for r in v['observations']],pg=None),execution_steps=['계약의 하위 증거별 소스·작업·세대·기능 확인'] if v['observations'] else None,note='필수 하위 증거 누락: '+', '.join(v['missing']))
        row['environment'].update(url='local-source://'+raw['source_revision']+'/review/',business_db='persistent TEST SQLite',inventory='separate shared TEST SQLite');row['preconditions']['restoration']='별도 영속 TEST 업무·재고 DB; 원거래 조회 후 확인된 상태만 복구'
    report['defects']=deepcopy(prior.get('defects',[]))
    for defect in report['defects']:
        if defect['id']=='BUG-RECONCILE-DUPLICATE-001':defect.update(status='FIXED_IN_LOCAL_CANDIDATE',retest='PASS_SOURCE_RECONCILER_AND_INDEPENDENT_ORACLE',fixed_revision=raw['source_revision'],retest_source_revision=raw['source_revision'])
    counts=result['coverage']['counts'];report['summary'].update(counts=counts,executed=sum(bool(v['observations']) for v in verdicts.values()),unique_observed_checks=17,deployed_revision=None,execution_scope=raw['scope'],unresolved_S1_S2=sum(d.get('severity') in ('S1','S2') and d['status'] not in ('CLOSED','FIXED_IN_LOCAL_CANDIDATE') for d in report['defects']),recommendation='인수 차단: UI/실제PG/공급사/정책 필수 하위 증거 미확보')
    report['summary']['business_results']={g:{s:sum(r['group']==g and r['status']==s for r in report['scenarios']) for s in counts} for g in {r['group'] for r in report['scenarios']}}
    report['retest_regression']=dict(status='PARTIAL_SOURCE_RETEST',candidate_source_revision=raw['source_revision'],actual_pg=False,actual_browser=False)
    report['customer_evidence_contract']=result['customer_contract'];report['actual_source_batch05']=dict(counts=result['counts'],admission=result['admission'],issues=result['issues'])
    return report
