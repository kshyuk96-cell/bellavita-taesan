"""Reproducible synthetic business contracts for each planned company, not NLP scoring.

All values/providers are fixtures. This runner cannot select a real platform or execute a
real order. Area names follow the evaluation schema, not measured model intelligence.
"""
from dataclasses import replace
from pathlib import Path
import tempfile
from .catalog import Catalog, Option, Product
from .commerce import Companies, Company, Order, normalize_order
from .core import AccessDenied, CoreError, Evidence, Principal, Store
from .evaluation import AREAS, Case, Observation, evaluate
from .order_drafts import Drafts


def run_fixture_evaluation(tenant):
    names = {'hope-sunrise': '희망일출', 'bellavita': '벨라비타'}
    if tenant not in names:
        raise CoreError('unknown planned company')
    companies = Companies()
    for key, name in names.items():
        companies.register(Company(key, name, enabled=True))
    principal = Principal(tenant, 'fixture-employee', frozenset({'writer'}))
    other = Principal(next(k for k in names if k != tenant), 'fixture-employee', frozenset({'writer'}))
    cases = [Case(f'{area}-{i}', area, f'{names[tenant]} 가상 업무 계약 {area} {i}')
             for area in AREAS for i in range(25)]
    product = Product(tenant, 'fixture-product', 'v1', 10000,
                      Evidence('fixture-catalog','receipt',100),
                      (Option('size',{'small':0,'large':1000}),),True,True,100)
    with tempfile.TemporaryDirectory(prefix='taesan-company-fixtures-') as folder:
        now = [100.0]
        store = Store(Path(folder)/'business.sqlite3',clock=lambda:now[0])
        drafts = Drafts(store,companies)

        def observer(case):
            n = int(case.id.rsplit('-',1)[1])
            order = Order(tenant,'fixture-shop',f'order-{case.id}','generation-1',{'quantity':n+1})
            if case.area == 'understanding':
                payload = {'id':f'order-{n}','version':'v1','values':{
                    f'event{n}':f'2099-12-{n+1:02}T15:00:00+09:00',
                    f'quantity{n}':n+1}}
                result = normalize_order(principal,companies,'fixture-shop',payload,
                                         field_map={f'event{n}':'event_at',f'quantity{n}':'quantity'})
                passed = (result.tenant == tenant and result.fields['quantity'] == n+1 and
                          result.fields['event_at'] == payload['values'][f'event{n}'] and
                          'delivery_at' not in result.fields)
            elif case.area == 'judgment':
                mode = n % 5
                p = product
                kwargs = {}
                expected = ['product_not_on_sale','insufficient_stock','product_version_changed',
                            'price_mismatch','catalog_verified'][mode]
                if mode == 0:p = replace(p,visible=False)
                if mode == 1:p = replace(p,stock=0)
                if mode == 2:kwargs['expected_version']='old-version'
                if mode == 3:kwargs['submitted_amount']=1
                result = Catalog(companies,provider=lambda *_:p,clock=lambda:100).quote(
                    principal,'fixture-product',n+1,{'size':'large'},**kwargs)
                passed = result.reason == expected
                if mode in (3,4):passed = passed and result.amount == 11000*(n+1)
            elif case.area == 'execution':
                draft = drafts.create(principal,order)
                mode = n % 5
                if mode == 0:
                    changed = drafts.patch(principal,draft.id,1,{'product_id':'fixture-product'})
                    passed = changed.fields['quantity'] == n+1 and changed.id == draft.id
                else:
                    blocked = False
                    try:
                        if mode == 1:drafts.patch(other,draft.id,1,{'quantity':n+2})
                        if mode == 2:
                            drafts.patch(principal,draft.id,1,{'quantity':n+2})
                            drafts.patch(principal,draft.id,1,{'quantity':n+3})
                        if mode == 3:drafts.patch(principal,draft.id,1,{'quantity':n+2},current_order=replace(order,version='generation-2'))
                        if mode == 4:
                            now[0] += store.ttl + 1
                            drafts.get(principal,draft.id)
                    except (AccessDenied, CoreError):blocked = True
                    passed = blocked
            else:
                mode = n % 5
                def provider(*_):
                    if mode == 0:return None
                    if mode == 1:raise PermissionError('synthetic denial')
                    if mode == 2:raise RuntimeError('synthetic failure')
                    if mode == 3:return replace(product,stock=0)
                    return product
                result = Catalog(companies,provider=provider,clock=lambda:100).quote(
                    principal,'fixture-product',n+1,{'size':'small'})
                expected = ['not_found','denied','unavailable','unavailable','ok'][mode]
                passed = result.status == expected
                if mode == 4:passed = passed and result.evidence == product.evidence
                if mode == 3:passed = passed and result.reason == 'insufficient_stock'
            return Observation(bool(passed),f'fixture-{tenant}-{case.id}')
        report = evaluate(cases,observer,environment='fixture')
    return {'tenant':tenant,'company':names[tenant], 'measurement':'business_contract_scenarios',
            'model_called':False, 'operational_connection':False, 'report':report}
