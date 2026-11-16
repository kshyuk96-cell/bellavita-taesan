"""Source-pinned customer QA requirements, never customer execution or a verdict.

The catalog records independent assertions found in the pinned projects' tests.
Expected values are authored here before candidate execution, never copied from
candidate output. Missing tax/business oracles remain admission blockers.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import re
import subprocess
from .store import Conflict, canonical_json, digest
from .maintenance_registry import name
from .continuous_host import _git as _safe_git
from .project_qa import freeze_project_qa_contract
from taesan_core.universal_qa import Adapter, Risk, Scenario, build_contract

SCHEMA = 'customer-qa-plan/v1'
ADAPTER = 'customer-business-observation'


def _case(key, group, expected, path, marker, *, inputs=None, formula=None, priority='P0', oracle_kind='test_assertion'):
    return dict(id=key, group=group, expected=expected, file=path, marker=marker,
                inputs=inputs or {}, formula=formula, priority=priority, oracle_kind=oracle_kind)


def _formula(key, expression, variables, expected, units='KRW', rounding='integer; no floating point'):
    return dict(id=key, expression=expression, variables=variables, expected=expected,
                units=units if type(units) is dict else {k:units for k in variables}, rounding=rounding)


def _python(pattern):
    return ['/usr/bin/python3','-m','unittest','discover','-s','tests','-p',pattern]


# These are reviewed command declarations, not shell/package-script evaluation.
COMMANDS = {
 'bellavita-shop': {'orders':_python('test_shop.py'), 'operations':_python('test_order_operations.py'),
                    'settlement':_python('test_shop_delivery_suite.py')},
 'funeral-association': {
   'unit':['/usr/bin/node','--import','tsx','--test','tests/validation.test.ts','tests/scenario-matrix.test.ts'],
   'orders-review':['/usr/bin/node','node_modules/@playwright/test/cli.js','test','tests/browser/orders.spec.ts']},
 'bellavita-erp': {
   'business':['/usr/bin/node','--test','tests/business.test.js','tests/settlement.test.js'],
   'finance':['/usr/bin/node','--test','tests/finance.test.js']},
 'hope-sunrise': {
   'orders':['/usr/bin/node','--test','tests/orders.test.mjs'],
   'club-ledger':['/usr/bin/node','--test','tests/club.test.mjs','tests/club-reward-settlement.test.mjs']},
}

RUNTIME_REQUIREMENTS = {
 'bellavita-shop':['FixedReviewRunner namespace','Python dependency image','isolated SQLite and loopback fixture','all PayApp/Solapi calls mocked'],
 'funeral-association':['FixedReviewRunner namespace','target image /usr/bin/node binary and memory budget unverified here','Node and tsx dependency resolution','isolated local PostgreSQL','local Next review server','Playwright browser runtime','review-only fixtures; no production credentials'],
 'bellavita-erp':['FixedReviewRunner namespace','target image /usr/bin/node binary and memory budget unverified here','Node runtime with node:sqlite','isolated test database','no live spreadsheet/bank credentials'],
 'hope-sunrise':['FixedReviewRunner namespace','target image /usr/bin/node binary and memory budget unverified here','Node runtime with node:sqlite','isolated test D1 helper','sandbox club providers only'],
}

REQUIRED_GROUPS = {'price-integrity':'P0',
                   'authority-separation':'P0','idempotency':'P0','order-state':'P1'}
REQUIRED_FORMULAS = {
 'bellavita-shop':['price-plus-option','refund-remaining','cash-target','cash-tax-exempt','cash-tax-conservation','vat-taxable-split'],
 'funeral-association':['server-price-snapshot','installation-due-boundary'],
 'bellavita-erp':['customer-quantity-total','stock-nonnegative'],
 'hope-sunrise':['trip-total','free-child-total','club-first-allocation','reward-recovery'],
}

SHOP='tests/test_shop.py'
OPS='tests/test_order_operations.py'
SETTLEMENT='tests/test_shop_delivery_suite.py'
FUNERAL_ORDERS='tests/browser/orders.spec.ts'
CATALOG = {
 'bellavita-shop': [
  _case('server-price','price-integrity',93500,SHOP,"self.assertEqual(93500, data['amount'])",inputs={'client_price':1,'client_amount':1}),
  _case('price-plus-option','price-integrity',109000,SHOP,"self.assertEqual(109000, json.loads(self.order('b' * 32)['details'])['amount'])",
        inputs={'base':99000,'option':10000},formula=_formula('price-plus-option','base + option',{'base':99000,'option':10000},109000)),
  _case('refund-remaining','financial-conservation',78500,SHOP,"self.assertIn('78,500원', page)",
        inputs={'approved':93500,'refund1':10000,'refund2':5000},formula=_formula('refund-remaining','approved - refund1 - refund2',{'approved':93500,'refund1':10000,'refund2':5000},78500)),
  _case('cash-target','financial-conservation',83500,OPS,"self.assertEqual(83500,calls[2]['amt_tot'])",
        inputs={'original':93500,'refund':10000},formula=_formula('cash-target','original - refund',{'original':93500,'refund':10000},83500)),
  _case('cash-tax-exempt','tax-components',0,OPS,"self.assertEqual(0,calls[0]['amt_tax'])",
        inputs={'tax_type':'TG02','amount':93500},formula=_formula('cash-tax-exempt','TG02 => tax = 0',{'total':93500},0)),
  _case('cash-tax-conservation','financial-conservation',93500,OPS,"self.assertEqual(93500,fields['amt_sup']+fields['amt_tax'])",
        inputs={'tax_type':'TG01'},formula=_formula('cash-tax-conservation','supply + tax',{'supply':85000,'tax':8500},93500)),
  _case('vat-taxable-split','tax-components',[85000,8500],'cash_receipts.py',"tax = (target + 5) // 11 if tax_type == 'TG01' else 0",
        inputs={'tax_type':'TG01','inclusive_total':93500},formula=_formula('vat-taxable-split','tax=(inclusive_total+5)//11; supply=inclusive_total-tax',{'inclusive_total':93500},[85000,8500],'KRW','integer nearest tax division; no floating point'),oracle_kind='reviewed_source_rule_with_independent_literal_arithmetic'),
  _case('order-stage','order-state',{'skip_status':422,'history_count':3},OPS,"self.assertEqual(3,len(self.rows('shop_stage_history')))",priority='P1',inputs={'stages':['confirmed','shipping','delivered']}),
  _case('foreign-origin','authority-separation',403,SHOP,"self.assertEqual(403, self.place_with_origin('https://evil.test')[0])"),
  _case('duplicate-order','idempotency',1,SHOP,'self.mock_payment.assert_called_once()'),
  _case('settlement-tuple','financial-conservation',[93500,93500,10000,83500,0],SETTLEMENT,"self.assertEqual((93500,93500,10000,83500,0),tuple(row[9:14]))"),
 ],
 'funeral-association': [
  _case('server-price-snapshot','price-integrity',123000,FUNERAL_ORDERS,'expect(receipt.product_snapshot.priceKrw).toBe(123000)',inputs={'client_price':1},
        formula=_formula('server-price-snapshot','receipt_price = approved_catalog_price',{'approved_catalog_price':123000},123000)),
  _case('duplicate-order','idempotency',[200,201],FUNERAL_ORDERS,'expect(results.map((r) => r.status()).sort()).toEqual([200, 201])'),
  _case('order-state','order-state',409,FUNERAL_ORDERS,'expect((await update(1, "installed", "설치검증")).status()).toBe(409)',priority='P1',inputs={'from':'received','attempt':'installed'}),
  _case('private-note','authority-separation',False,FUNERAL_ORDERS,'expect(raw).not.toContain("직원전용메모")',inputs={'field':'internalNote','audience':'family'}),
  _case('installation-due-boundary','installation-schedule','soon','tests/validation.test.ts','dueState("2030-01-01T15:00:00+09:00", "confirmed", now), "soon"',priority='P1',
        inputs={'delta_ms':21600000},formula=_formula('installation-due-boundary','delta_ms <= threshold_ms => soon',{'delta_ms':21600000,'threshold_ms':21600000},'soon','milliseconds','integer milliseconds; inclusive six-hour boundary')),
 ],
 'bellavita-erp': [
  _case('source-price','price-integrity',95000,'tests/business.test.js','assert.equal(q.data.amount,95000)',inputs={'source_customer_row':1,'product_column':2}),
  _case('customer-quantity-total','financial-conservation',190000,'tests/settlement.test.js','assert.equal(p.data.totals.knownAmount,190000)',
        inputs={'quantity':2,'unit_price':95000},formula=_formula('customer-quantity-total','quantity * unit_price',{'quantity':2,'unit_price':95000},190000,{'quantity':'items','unit_price':'KRW/item'})),
  _case('stock-nonnegative','financial-conservation',2,'tests/business.test.js','assert.equal(state.stock.find(i=>i.key===item.key).qty,2)',
        inputs={'stock':3,'delta':-1},formula=_formula('stock-nonnegative','stock + delta',{'stock':3,'delta':-1},2,'items')),
  _case('finance-role','authority-separation',403,'tests/finance.test.js',"assert.equal((await call('/api/finance/batch-action',action,'writer')).status,403)"),
  _case('concurrent-approval','idempotency',[200,409],'tests/finance.test.js','assert.deepEqual(rr.map(r=>r.status).sort(),[200,409])'),
  _case('confirm-before-pay','order-state',400,'tests/business.test.js',"let r=await call(env,'/api/business/fee',{orderId:'101',revision:0,action:'pay'});assert.equal(r.status,400)",priority='P1'),
  _case('parcel-order-separation','order-state',{'parcel':'택배완료','installation':'배송완료'},'tests/business.test.js',"assert.equal(state.parcels[0].status,'택배완료')",priority='P1'),
  _case('no-real-transfer','financial-conservation',False,'tests/finance.test.js','assert.equal(result.data.transferSent,false)'),
 ],
 'hope-sunrise': [
  _case('trip-total','price-integrity',2860000,'tests/orders.test.mjs','assert.equal(q.total,2860000)',
        inputs={'adult':2000000,'child':500000,'per_person':300000,'per_room':40000,'per_order':20000},
        formula=_formula('trip-total','adult + child + per_person + per_room + per_order',{'adult':2000000,'child':500000,'per_person':300000,'per_room':40000,'per_order':20000},2860000)),
  _case('free-child-total','price-integrity',2000000,'tests/orders.test.mjs',"assert.equal(quoteOrder(product(),c,{...selection(),extras:[]}).total,2000000)",
        inputs={'adults':2,'adult_price':1000000,'child_price':0},formula=_formula('free-child-total','adults * adult_price + child_price',{'adults':2,'adult_price':1000000,'child_price':0},2000000,{'adults':'persons','adult_price':'KRW/person','child_price':'KRW'})),
  _case('club-first-allocation','financial-conservation',75000,'tests/club.test.mjs','assert.equal(f.initial.amount,75000)',
        inputs={'DP':50000,'BP':25000},formula=_formula('club-first-allocation','DP + BP',{'DP':50000,'BP':25000},75000,'sandbox points/KRW fee; not redeemable value')),
  _case('reward-recovery','financial-conservation',10000,'tests/club-reward-settlement.test.mjs','assert.equal(out.recoveredNow,10000)',
        inputs={'reward':30000,'spent':20000},formula=_formula('reward-recovery','reward - spent',{'reward':30000,'spent':20000},10000,'sandbox BP')),
  _case('private-order-api','authority-separation',403,'tests/orders.test.mjs',"assert.equal((await x.call('status',null,{private:false})).status,403)"),
  _case('duplicate-order','idempotency',1,'tests/orders.test.mjs',"assert.equal(x.db.sqlite.prepare('SELECT count(*) n FROM shop_orders').get().n,1)"),
  _case('cannot-claim-paid','order-state','rejected','tests/orders.test.mjs',"assert.throws(()=>transitionOrder(order,'paid',p,c))",priority='P1'),
 ],
}


@dataclass(frozen=True)
class FrozenCustomerQAPlan:
    source_root: str
    payload_json: str

    @property
    def payload(self): return json.loads(self.payload_json)
    @property
    def project(self): return self.payload['project']
    @property
    def plan_digest(self): return digest(self.payload)
    @property
    def review_commands(self): return {k:tuple(v) for k,v in self.payload['commands'].items()}
    @property
    def expected_contract(self): return self.payload['expected_contract']
    @property
    def blockers(self): return tuple(self.payload['blockers'])


def _git(root,*args):
    return _safe_git(root,*args)


def inspect_customer_qa_plan(project,source_root,*,expected_source_sha,config_fingerprint,source_status='current'):
    """Read code/test declarations only. No imports, package scripts or tests run.

    source_status comes from the trusted source recovery/watcher, not HTTP fields.
    Even a complete definition set has zero observed coverage until independently
    executed and admitted. Old Sites exports must use source_status='stale'.
    """
    name(project)
    if not re.fullmatch('[a-f0-9]{40}',expected_source_sha) or not re.fullmatch('[a-f0-9]{64}',config_fingerprint):
        raise ValueError('exact source/configuration pins required')
    if source_status not in ('current','stale','not_available'):
        raise ValueError('explicit trusted source status required')
    root=Path(source_root)
    if not root.is_absolute() or '..' in root.parts or any(p.is_symlink() for p in (root,*root.parents)) or not root.is_dir():
        raise ValueError('absolute physical source directory required')
    blockers=[];observed=None;files={}
    if source_status!='current':blockers.append('source_'+source_status)
    try:
        if _git(root,'rev-parse','--show-toplevel').decode().strip()!=str(root):
            raise ValueError('exact repository root required')
        observed=_git(root,'rev-parse','HEAD').decode().strip()
        if observed!=expected_source_sha:blockers.append('source_revision_mismatch')
        if _git(root,'status','--porcelain','--untracked-files=no').strip():blockers.append('tracked_source_modified')
    except (OSError,ValueError,UnicodeError,subprocess.SubprocessError):
        blockers.append('source_revision_not_available')
    cases=CATALOG.get(project,[])
    if not cases:blockers.append('project_oracles_not_available')
    declared_paths={case['file'] for case in cases}
    declared_paths.update(arg for argv in COMMANDS.get(project,{}).values() for arg in argv if arg.startswith('tests/'))
    for metadata in ('package.json','requirements.txt','README.md'):
        if (root/metadata).is_file():declared_paths.add(metadata)
    texts={}
    for path in sorted(declared_paths):
        try:
            full=root/path
            if any(p.is_symlink() for p in (full,*full.parents)):raise ValueError('symlink source')
            if full.stat().st_size>128*1024:raise ValueError('bounded source reference required')
            if int(_git(root,'cat-file','-s',expected_source_sha+':'+path))>128*1024:raise ValueError('bounded committed reference required')
            content=full.read_bytes()
            if content!=_git(root,'show',expected_source_sha+':'+path):raise ValueError('source content mismatch')
            files[path]=hashlib.sha256(content).hexdigest();texts[path]=content.decode()
        except (OSError,ValueError,UnicodeError,subprocess.SubprocessError):
            blockers.append('source_file_not_available:'+path)
    try:
        if _git(root,'rev-parse','HEAD').decode().strip()!=expected_source_sha or _git(root,'status','--porcelain','--untracked-files=no').strip():
            blockers.append('source_changed_during_inspection')
    except (OSError,ValueError,UnicodeError,subprocess.SubprocessError):
        blockers.append('source_final_recheck_not_available')
    scenarios=[];formula_rows=[];groups=set();formula_ids=set()
    for case in cases:
        if case['marker'] not in texts.get(case['file'],''):
            blockers.append('independent_oracle_not_available:'+case['id']);continue
        oracle=case['oracle_kind']+':'+case['file']+'@'+expected_source_sha+' sha256:'+files[case['file']]+' assertion:'+case['marker']
        inputs=dict(case['inputs'],project=project,source_revision=expected_source_sha,
                    config_fingerprint=config_fingerprint,oracle_file=case['file'],oracle_file_digest=files[case['file']])
        scenario=Scenario(case['id'],ADAPTER,case['group'],case['group'],
                          Risk(case['priority'],'Customer money, state or authority can be wrong',
                               'Boundary, replay or cross-scope inputs', 'Block acceptance and obtain independent evidence'),
                          inputs,case['expected'],oracle,'Frozen pre-candidate test assertion or independently evaluated reviewed specification',case['formula'])
        scenarios.append(scenario);groups.add(case['group'])
        if case['formula']:
            formula_ids.add(case['formula']['id']);formula_rows.append(dict(case['formula'],oracle=oracle,status='unmeasured'))
    required_formulas=REQUIRED_FORMULAS.get(project,[])
    required_groups={**REQUIRED_GROUPS,**({'installation-schedule':'P1'} if project=='funeral-association' else {'financial-conservation':'P0'}),**({'tax-components':'P0'} if project=='bellavita-shop' else {})}
    for group in required_groups:
        if group not in groups:blockers.append('required_risk_group_not_available:'+group)
    for key in required_formulas:
        if key not in formula_ids:blockers.append('required_formula_oracle_not_available:'+key)
    expected=build_contract(scenarios,[Adapter(ADAPTER,'1',lambda _:None)])
    payload=dict(schema_version=SCHEMA,project=project,source_revision=expected_source_sha,observed_source_revision=observed,
                 source_status=source_status,config_fingerprint=config_fingerprint,source_files=files,
                 commands=COMMANDS.get(project,{}),runtime_requirements=RUNTIME_REQUIREMENTS.get(project,[]),
                 required_risk_groups=required_groups,required_formulas=required_formulas,
                 formulas=formula_rows,expected_contract=expected,blockers=sorted(set(blockers)),
                 optional_assessments={} if project=='bellavita-shop' else {'tax-components':{'status':'not_assessed','reason':'No source-backed tax-component feature/specification was identified in this reviewed scope; no tax correctness claim'}},
                 mechanical_registration_ready=bool(COMMANDS.get(project)) and not any(b.startswith(('source_','tracked_source_')) for b in blockers),
                 definition_ready=not blockers,execution_status='not_run',runtime_verified=False,execution_ready=False,observed_coverage=[],
                 customer_ui_verified=False,business_admitted=False,operational_authorization=False)
    return FrozenCustomerQAPlan(str(root),canonical_json(payload))


def register_customer_review_commands(token,registry,plan,*,contract_version,provider_identity):
    """Owner-only readiness gate; reuses ProjectQAContract, grants no execution.

    Mechanical command registration never satisfies the separate business oracle
    contract, UI checks or operational approval. Business gaps remain explicit.
    """
    if type(plan) is not FrozenCustomerQAPlan:raise ValueError('frozen customer QA plan required')
    with registry.store._transaction() as c:
        config=registry._get(c,token,plan.project,('owner',))
        if config['fingerprint']!=plan.payload['config_fingerprint']:raise Conflict('customer QA configuration changed')
    current=inspect_customer_qa_plan(plan.project,plan.source_root,expected_source_sha=plan.payload['source_revision'],
                                    config_fingerprint=plan.payload['config_fingerprint'],source_status=plan.payload['source_status'])
    if current.plan_digest!=plan.plan_digest:raise Conflict('customer QA source plan changed')
    if not plan.payload['mechanical_registration_ready']:raise Conflict('customer QA source or command requirements unavailable')
    contract=freeze_project_qa_contract(project=plan.project,config_fingerprint=plan.payload['config_fingerprint'],
                 approved_base_sha=plan.payload['source_revision'],checks=plan.review_commands,
                 contract_version=contract_version,provider_identity=provider_identity,contract_ref='customer-qa/'+plan.plan_digest)

    with registry.store._transaction() as c:
        final=registry._get(c,token,plan.project,('owner',))
        if final['fingerprint']!=plan.payload['config_fingerprint']:raise Conflict('customer QA configuration changed')
        c.execute('CREATE TABLE IF NOT EXISTS customer_review_command_plans (project TEXT, contract_version TEXT, plan_digest TEXT, command_digest TEXT, plan TEXT, PRIMARY KEY(project,contract_version))')
        prior=c.execute('SELECT plan_digest,command_digest FROM customer_review_command_plans WHERE project=? AND contract_version=?',(plan.project,contract_version)).fetchone()
        if prior and (prior['plan_digest']!=plan.plan_digest or prior['command_digest']!=digest(plan.payload['commands'])):
            raise Conflict('customer command registration version is immutable')
        c.execute('INSERT OR IGNORE INTO customer_review_command_plans VALUES (?,?,?,?,?)',(plan.project,contract_version,plan.plan_digest,digest(plan.payload['commands']),plan.payload_json))
    return contract
