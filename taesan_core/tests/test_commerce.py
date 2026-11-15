from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from taesan_core import AccessDenied, CoreError, Evidence, Principal, Store
from taesan_core.catalog import Catalog, Option, Product
from taesan_core.commerce import Companies, Company, Order, iso_time, normalize_order
from taesan_core.order_drafts import Drafts


class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.companies = Companies()
        self.companies.register(Company('hope-sunrise','희망일출',enabled=True))
        self.companies.register(Company('bellavita','벨라비타',enabled=True))
        self.person = Principal('bellavita','employee-1',frozenset({'reader'}))
        self.other = Principal('hope-sunrise','employee-1',frozenset({'reader'}))
        self.order = Order('bellavita','shop','13370','generation-1',{'quantity':2})
        self.product = Product('bellavita','sample','p1',93500,Evidence('catalog','r1',100),
                               (Option('rice',{'10kg':0,'20kg':5500}),),True,True,10)
        self.catalog = Catalog(self.companies,provider=lambda *_:self.product,clock=lambda:100)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 100
        self.store = Store(Path(self.tmp.name)/'store.sqlite3',clock=lambda:self.now)
        self.drafts = Drafts(self.store,self.companies)

    def test_actual_company_names_not_enabled_automatically(self):
        planned = Companies.planned()
        for p in (self.person,self.other):
            with self.subTest(company=p.tenant),self.assertRaises(AccessDenied):
                planned.for_principal(p)

    def test_duplicate_company_config_rejected(self):
        with self.assertRaises(CoreError):self.companies.register(Company('bellavita','duplicate'))

    def test_company_identity_never_taken_from_order_payload(self):
        with self.assertRaises(CoreError):
            normalize_order(self.person,self.companies,'shop',{'id':'1','version':'v1','tenant':'hope-sunrise','values':{}},field_map={})

    def test_normalization_preserves_event_and_delivery_separately(self):
        result = normalize_order(self.person,self.companies,'shop',{'id':'1','version':'v1','values':{'event':'2026-11-17T15:00:00+09:00','delivery':'2026-11-17T14:00:00+09:00'}},field_map={'event':'event_at','delivery':'delivery_at'})
        self.assertNotEqual(result.fields['event_at'],result.fields['delivery_at'])
        self.assertEqual(result.tenant,self.person.tenant)

    def test_event_not_copied_to_delivery(self):
        result=normalize_order(self.person,self.companies,'shop',{'id':'1','version':'v1','values':{'event':'2026-11-17T15:00:00+09:00'}},field_map={'event':'event_at'})
        self.assertNotIn('delivery_at',result.fields)

    def test_unknown_mapping_and_ambiguous_destination_rejected(self):
        payload={'id':'1','version':'v1','values':{'unknown':'text'}}
        for mapping in ({'quantity':'quantity'},{'a':'quantity','b':'quantity'}):
            with self.subTest(mapping=mapping),self.assertRaises(CoreError):
                normalize_order(self.person,self.companies,'shop',payload,field_map=mapping)

    def test_unknown_order_fields_rejected(self):
        with self.assertRaises(CoreError):Order('bellavita','shop','1','v1',{'admin':True})

    def test_unsafe_times_and_float_money_rejected(self):
        for fields in ({'event_at':'2026-11-17T15:00:00'},{'amount':93500.0},{'quantity':True},{'quantity':0}):
            with self.subTest(fields=fields),self.assertRaises(CoreError):
                Order('bellavita','shop','1','v1',fields)

    def test_normalizer_cannot_trust_foreign_currency(self):
        with self.assertRaises(CoreError):
            normalize_order(self.person,self.companies,'shop',{'id':'1','version':'v1','values':{'currency':'USD'}},field_map={'currency':'currency'})

    def test_cross_company_order_authorization(self):
        with self.assertRaises(AccessDenied):self.order.authorize(self.other)

    def test_catalog_price_computed_from_provider(self):
        result=self.catalog.quote(self.person,'sample',2,{'rice':'20kg'})
        self.assertEqual(result.amount,198000)
        self.assertEqual(result.status,'ok')
        self.assertEqual(result.product_version,'p1')

    def test_submitted_amount_mismatch_needs_review(self):
        result=self.catalog.quote(self.person,'sample',1,{'rice':'10kg'},submitted_amount=1)
        self.assertEqual(result.status,'needs_review')
        self.assertEqual(result.amount,93500)

    def test_product_options_required_and_unknown_options_rejected(self):
        self.assertEqual(self.catalog.quote(self.person,'sample',1).status,'clarify')
        self.assertEqual(self.catalog.quote(self.person,'sample',1,{'rice':'unknown'}).status,'invalid')
        self.assertEqual(self.catalog.quote(self.person,'sample',1,{'rice':'10kg','extra':'1'}).status,'invalid')

    def test_disabled_or_hidden_product_not_orderable(self):
        for product in (replace(self.product,visible=False),replace(self.product,active=False)):
            self.product=product
            self.assertEqual(self.catalog.quote(self.person,'sample',1,{'rice':'10kg'}).status,'unavailable')

    def test_stock_zero_vs_unknown(self):
        self.product=replace(self.product,stock=0)
        self.assertEqual(self.catalog.quote(self.person,'sample',1,{'rice':'10kg'}).reason,'insufficient_stock')
        self.product=replace(self.product,stock=None)
        self.assertEqual(self.catalog.quote(self.person,'sample',1,{'rice':'10kg'}).reason,'inventory_not_readable')

    def test_explicit_non_stock_product(self):
        self.product=replace(self.product,stock=None,tracks_stock=False)
        self.assertEqual(self.catalog.quote(self.person,'sample',1,{'rice':'10kg'}).status,'ok')

    def test_stale_or_future_product_evidence_not_current(self):
        for observed in (1,101):
            self.product=replace(self.product,evidence=Evidence('catalog','r1',observed))
            catalog=Catalog(self.companies,provider=lambda *_:self.product,clock=lambda:200,max_age=50)
            if observed==101:catalog.clock=lambda:100
            self.assertEqual(catalog.quote(self.person,'sample',1,{'rice':'10kg'}).reason,'catalog_snapshot_stale')

    def test_catalog_wrong_company_or_wrong_product_is_denied(self):
        self.assertEqual(self.catalog.quote(self.other,'sample',1,{'rice':'10kg'}).status,'denied')
        self.assertEqual(self.catalog.quote(self.person,'other-product',1,{'rice':'10kg'}).status,'denied')

    def test_disconnected_denied_notfound_catalog_separate(self):
        self.assertEqual(Catalog(self.companies).quote(self.person,'sample',1).status,'unavailable')
        self.assertEqual(Catalog(self.companies,provider=lambda *_:None).quote(self.person,'sample',1).status,'not_found')
        def denied(*_):raise PermissionError('private error')
        self.assertEqual(Catalog(self.companies,provider=denied).quote(self.person,'sample',1).status,'denied')

    def test_provider_error_not_exposed(self):
        def failed(*_):raise RuntimeError('private credential')
        result=Catalog(self.companies,provider=failed).quote(self.person,'sample',1)
        self.assertEqual(result.reason,'catalog_read_failed')
        self.assertNotIn('credential',str(result))

    def test_catalog_version_changes_need_review(self):
        self.assertEqual(self.catalog.quote(self.person,'sample',1,{'rice':'10kg'},expected_version='p0').reason,'product_version_changed')

    def test_catalog_bool_or_float_quantity_and_negative_price_rejected(self):
        for quantity in (True,1.0,0,-1):
            with self.subTest(quantity=quantity),self.assertRaises(CoreError):self.catalog.quote(self.person,'sample',quantity)
        with self.assertRaises(CoreError):replace(self.product,price=-1)

    def test_draft_missing_only_and_values_preserved(self):
        draft=self.drafts.create(self.person,self.order)
        self.assertNotIn('quantity',draft.missing)
        updated=self.drafts.patch(self.person,draft.id,1,{'product_id':'sample'})
        self.assertEqual(updated.fields,{'quantity':2,'product_id':'sample'})
        self.assertNotIn('product_id',updated.missing)
        self.assertEqual(updated.id,draft.id)
        self.assertEqual(updated.revision,2)

    def test_company_specific_required_fields(self):
        companies=Companies();companies.register(Company('bellavita','벨라비타',('quantity',),True))
        drafts=Drafts(self.store,companies)
        self.assertEqual(drafts.create(self.person,self.order).missing,())

    def test_draft_isolated_between_companies_and_employees(self):
        draft=self.drafts.create(self.person,self.order)
        for p in (self.other,replace(self.person,subject='employee-2')):
            with self.subTest(principal=p),self.assertRaises(AccessDenied):self.drafts.get(p,draft.id)
        with self.assertRaises(AccessDenied):self.drafts.create(self.other,self.order)

    def test_draft_revision_conflict(self):
        draft=self.drafts.create(self.person,self.order)
        self.drafts.patch(self.person,draft.id,1,{'quantity':3})
        with self.assertRaises(CoreError):self.drafts.patch(self.person,draft.id,1,{'quantity':4})
        self.assertEqual(self.drafts.get(self.person,draft.id).fields['quantity'],3)

    def test_concurrent_draft_update_one_winner(self):
        draft=self.drafts.create(self.person,self.order)
        def update(q):
            try:self.drafts.patch(self.person,draft.id,1,{'quantity':q});return True
            except CoreError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(update,[3,4]))
        self.assertEqual(sum(outcomes),1)

    def test_source_generation_change_requires_review(self):
        draft=self.drafts.create(self.person,self.order)
        with self.assertRaises(CoreError):
            self.drafts.patch(self.person,draft.id,1,{'quantity':3},current_order=replace(self.order,version='generation-2'))
        self.assertEqual(self.drafts.get(self.person,draft.id).revision,1)

    def test_stable_creation_generation_separate_from_source_revision(self):
        payload={'id':'13370','version':'v2','generation':'creation-1','values':{}}
        result=normalize_order(self.person,self.companies,'shop',payload,field_map={})
        self.assertEqual(result.version,'v2')
        self.assertEqual(result.generation,'creation-1')

    def test_same_row_same_revision_but_new_generation_cannot_reuse_draft(self):
        order=replace(self.order,generation='creation-1')
        draft=self.drafts.create(self.person,order)
        with self.assertRaises(CoreError):
            self.drafts.patch(self.person,draft.id,1,{'quantity':3},current_order=replace(order,generation='creation-2'))
        self.assertEqual(self.drafts.get(self.person,draft.id).revision,1)

    def test_draft_clear_field_does_not_reuse_previous_value(self):
        draft=self.drafts.create(self.person,self.order)
        updated=self.drafts.patch(self.person,draft.id,1,{},clear_fields=('quantity',))
        self.assertIn('quantity',updated.missing)
        self.assertNotIn('quantity',updated.fields)

    def test_unknown_field_cannot_be_added_to_draft(self):
        draft=self.drafts.create(self.person,self.order)
        with self.assertRaises(CoreError):self.drafts.patch(self.person,draft.id,1,{'tenant':'hope-sunrise'})

    def test_expired_draft_cannot_resume_and_purge_erases_it(self):
        draft=self.drafts.create(self.person,self.order)
        self.now+=2000
        with self.assertRaises(AccessDenied):self.drafts.get(self.person,draft.id)
        self.drafts.purge()
        with self.store.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM commerce_drafts').fetchone()[0],0)

    def test_draft_survives_restart_without_other_employee_access(self):
        draft=self.drafts.create(self.person,self.order)
        reopened=Drafts(Store(self.store.path,clock=lambda:self.now),self.companies)
        self.assertEqual(reopened.get(self.person,draft.id),draft)
