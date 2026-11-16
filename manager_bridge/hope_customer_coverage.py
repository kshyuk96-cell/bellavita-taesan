"""Immutable 38-row evidence requirements; a partial API check is never full acceptance."""
from copy import deepcopy
from .hope_tour_reporting import ROWS
from .store import Conflict,digest

BINDING_KEYS={'project','task','source_revision','test_contract_digest','adapter_release','generation'}
CHECK_SCOPES={'api':'actual_source_api','ledger':'independent_ledger','inventory':'shared_test_inventory','ui':'supported_browser','pg':'actual_pg','policy':'approved_policy','human':'human_observation','devices':'real_device','backup':'independent_backup_restore','terms':'server_html','supplier':'actual_supplier_inventory'}
CAPABILITIES=frozenset({'actual_source_api','independent_ledger','shared_test_inventory','server_html'})
# A name identifies a specific requirement, not an interchangeable success flag.
SPECIAL={
 'MEM-10':{'terms_html':'terms','keyboard_touch':'ui','mobile_help':'ui'},
 'PAY-06':{'duplicate_booking':'api','duplicate_payment':'api','ledger':'ledger','ui':'ui','pg':'pg','policy':'policy'},
 'PAY-07':{'unknown_payment':'api','ledger':'ledger','ui':'ui','pg':'pg','policy':'policy'},
 'PAY-08':{'partial_refund':'api','ledger':'ledger','inventory':'inventory','ui':'ui','pg':'pg','policy':'policy'},
 'E2E-02':{'refund_flow':'api','ledger':'ledger','inventory':'inventory','ui':'ui','pg':'pg','policy':'policy'},
 'E2E-04':{'recovery_api':'api','last_seat_api':'api','inventory':'inventory','ui':'ui','pg':'pg','policy':'policy'},
 'UX-01':{'task_metrics':'human','ui':'ui'},'UX-02':{'mobile_layout':'ui','actual_devices':'devices'},
 'OPS-01':{'backup_restore':'backup','regression_api':'api','ui':'ui'}}


def coverage_contract(binding):
    if type(binding) is not dict or set(binding)!=BINDING_KEYS or binding.get('project')!='hope-tour' or type(binding.get('generation')) is not int or binding['generation']<1:
        raise Conflict('full project/task/source/contract/adapter/generation binding required')
    if not all(type(binding[k]) is str and binding[k] for k in BINDING_KEYS-{'generation'}):raise Conflict('typed coverage binding required')
    rows={}
    for sid,group,priority,*_ in ROWS:
        needed=dict(SPECIAL.get(sid,{sid.lower()+'_api':'api','ui':'ui'}))
        if sid not in SPECIAL and (group in ('결제','통합') or sid in ('MEM-01','MEM-03','MEM-04','MEM-07','PRD-02','PRD-07')):needed.update(ledger='ledger',policy='policy')
        if priority=='P0' or sid=='MEM-06':needed.update(policy='policy')
        if sid=='PAY-10':needed.update(pg='pg')
        if sid=='PRD-04':needed.update(inventory='inventory',supplier_inventory='supplier',policy='policy')
        rows[sid]=dict(priority=priority,required={name:CHECK_SCOPES[kind] for name,kind in needed.items()})
    return dict(schema='hope-customer-evidence-contract/v1',binding=deepcopy(binding),rows=rows)


def assess(contract,records,*,binding,capabilities=CAPABILITIES):
    if contract!=coverage_contract(binding):raise Conflict('customer requirements altered or missing')
    if not set(capabilities)<=CAPABILITIES:raise Conflict('unprovisioned evidence transport')
    byrow={sid:{} for sid in contract['rows']}
    for record in records:
        if type(record) is not dict or set(record)!={'binding','row','check','scope','status','actual'} or digest(record['binding'])!=digest(binding):raise Conflict('stale or malformed customer evidence')
        digest(record)
        sid=record['row'];check=record['check']
        if sid not in byrow or check not in contract['rows'][sid]['required']:raise Conflict('unknown customer row/check mapping')
        if record['scope']!=contract['rows'][sid]['required'][check] or record['scope'] not in capabilities:raise Conflict('unsupported or mislabelled evidence capability')
        if record['status'] not in ('pass','fail','unmeasured') or check in byrow[sid]:raise Conflict('duplicate or unknown evidence verdict')
        byrow[sid][check]=record
    results=[]
    for sid,spec in contract['rows'].items():
        found=byrow[sid];missing=[k for k in spec['required'] if k not in found or found[k]['status']!='pass']
        status='FAIL' if any(r['status']=='fail' for r in found.values()) else 'PASS' if not missing else 'BLOCKED' if found else 'NOT RUN'
        results.append(dict(id=sid,priority=spec['priority'],status=status,missing=missing,observations=list(found.values())))
    return dict(contract_digest=digest(contract),rows=results,counts={s:sum(r['status']==s for r in results) for s in ('PASS','FAIL','BLOCKED','NOT RUN')},admitted=all(r['status']=='PASS' for r in results if r['priority'] in ('P0','P1')),operational_authorization=False)


def coverage_probes(binding):
    trusted=coverage_contract(binding)
    sample=dict(binding=deepcopy(binding),row='PAY-06',check='duplicate_payment',scope='actual_source_api',status='pass',actual={'effects':1})
    partial=assess(trusted,[sample],binding=binding)
    row=next(r for r in partial['rows'] if r['id']=='PAY-06')
    rejected=0
    for key in sorted(BINDING_KEYS):
        altered=deepcopy(sample);altered['binding'][key]=2 if key=='generation' else 'wrong'
        try:assess(trusted,[altered],binding=binding)
        except Conflict:rejected+=1
    for attack in ('mapping','capability','deleted-row','priority'):
        trial=deepcopy(sample);changed=deepcopy(trusted)
        if attack=='mapping':trial['row']='MEM-01'
        elif attack=='capability':trial.update(row='PAY-06',check='ui',scope='supported_browser')
        elif attack=='deleted-row':changed['rows'].pop('MEM-07')
        else:changed['rows']['PAY-06']['priority']='P3'
        try:assess(changed,[trial],binding=binding)
        except Conflict:rejected+=1
    return {95:dict(rows=len(partial['rows']),partialBlocked=row['status']=='BLOCKED',customerPass=partial['counts']['PASS']),96:dict(rejected=rejected),99:dict(unapprovedNotAdmitted=not partial['admitted'],policyMissing='policy' in row['missing'])}
