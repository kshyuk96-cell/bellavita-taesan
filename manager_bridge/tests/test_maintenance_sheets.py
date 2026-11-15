from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from manager_bridge.store import Store, PermissionDenied, token_hash
from manager_bridge.maintenance_registry import Registry
from manager_bridge.maintenance_sheets import SheetRow, SheetSnapshot, SheetProvider, SheetChecks, compare_sheets


class SheetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory())
        self.store=Store(Path(self.tmp)/'store.sqlite3',clock=lambda:1000)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        self.config={'company':'bellavita','name':'Fixture ERP','source_kind':'github','source_ref':'fixture/erp','branch':'main','services':[],'maintenance_enabled':False}
        self.registry.register(self.owner,'erp',self.config)
        self.manager=self.store.add_token('manager','manager',['erp'])
        self.before=SheetSnapshot('bellavita','erp','sheet-1','tab-1','revision-1',('id','completed','total'),
            (SheetRow('stable-1',(1,False,2),(None,None,'=1+1')),SheetRow('stable-2',(2,None,4))),
            'fixture/read',1000)
        self.after=replace(self.before)
        self.calls=[]

    def compare(self,before=None,after=None,expected=None):
        return compare_sheets(before or self.before,after or self.after,expected or self.after.headers)

    def checks(self,callback=None,**provider_changes):
        def observe(project,binding,phase):
            self.calls.append(phase)
            return self.before if phase=='before' else self.after
        provider=SheetProvider('bellavita','sheet-1','tab-1',self.after.headers,callback or observe)
        if provider_changes:provider=replace(provider,**provider_changes)
        return SheetChecks(self.store,self.registry,{('erp','sheet-binding'):provider})

    def test_whitespace_revision_is_not_verified_source(self):
        snapshot=replace(self.before,source_version='   ')
        result=compare_sheets(snapshot,snapshot,snapshot.headers)
        self.assertEqual(result['state'],'review')
        self.assertIn('source-version-mismatch',result['reasons'])

    def test_empty_schema_not_confused_with_empty_data(self):
        snapshot=replace(self.before,headers=(),rows=())
        result=compare_sheets(snapshot,snapshot,())
        self.assertEqual(result['state'],'review')
        self.assertIn('missing-schema-header',result['reasons'])

    def test_identical_snapshots_pending_only(self):
        result=self.compare()
        self.assertEqual(result['state'],'compatible')
        self.assertEqual(result['approval_state'],'pending_approval')
        self.assertFalse(result['production_write_authorized'])
        self.assertFalse(result['operational_change'])

    def test_stable_ids_allow_row_move(self):
        self.after=replace(self.before,rows=self.before.rows[::-1])
        self.assertEqual(self.compare()['state'],'compatible')

    def test_appended_column_requires_exact_expected_schema(self):
        self.after=replace(self.before,headers=(*self.before.headers,'tag'),rows=tuple(
            SheetRow(r.key,(*r.values,None),(*(r.formulas or (None,)*3),None)) for r in self.before.rows))
        self.assertEqual(self.compare()['state'],'compatible')
        self.assertEqual(self.compare(expected=self.before.headers)['state'],'review')

    def test_reordered_protected_columns_review(self):
        self.after=replace(self.before,headers=('completed','id','total'))
        self.assertIn('protected-column-position-changed',self.compare()['reasons'])

    def test_renamed_or_removed_column_review(self):
        self.after=replace(self.before,headers=('id','completion','total'))
        self.assertEqual(self.compare()['state'],'review')

    def test_duplicate_headers_review(self):
        self.after=replace(self.before,headers=('id','id','total'))
        self.assertIn('duplicate-header',self.compare()['reasons'])

    def test_missing_stable_id_review(self):
        for key in (None,'',' '):
            with self.subTest(key=key):
                self.after=replace(self.before,rows=(replace(self.before.rows[0],key=key),))
                self.assertIn('missing-stable-row-identity',self.compare()['reasons'])

    def test_duplicate_row_identity_review(self):
        self.after=replace(self.before,rows=(self.before.rows[0],self.before.rows[0]))
        self.assertIn('duplicate-row-identity',self.compare()['reasons'])

    def test_row_loss_review(self):
        self.after=replace(self.before,rows=self.before.rows[:1])
        self.assertIn('original-row-missing',self.compare()['reasons'])

    def test_boolean_integer_blank_distinct(self):
        for value in (0,None,'FALSE',True):
            with self.subTest(value=value):
                self.after=replace(self.before,rows=(replace(self.before.rows[0],values=(1,value,2)),self.before.rows[1]))
                self.assertIn('protected-cell-value-changed',self.compare()['reasons'])

    def test_formula_not_equal_cached_value(self):
        self.after=replace(self.before,rows=(replace(self.before.rows[0],formulas=(None,None,None)),self.before.rows[1]))
        self.assertIn('protected-formula-changed',self.compare()['reasons'])

    def test_changed_or_missing_source_version_review(self):
        for version in ('revision-2',''):
            self.after=replace(self.before,source_version=version)
            self.assertIn('source-version-mismatch',self.compare()['reasons'])

    def test_other_source_scope_review(self):
        for field in ('company','project','spreadsheet','tab'):
            self.after=replace(self.before,**{field:'other'})
            self.assertIn('source-scope-mismatch',self.compare()['reasons'])

    def test_report_never_exposes_raw_cell_or_formula(self):
        self.before=replace(self.before,rows=(SheetRow('sensitive-id',('private customer',False,2),(None,None,'=PRIVATE_FORMULA()')),))
        self.after=replace(self.before)
        output=json.dumps(self.compare())
        for value in ('private customer','PRIVATE_FORMULA','sensitive-id'):
            self.assertNotIn(value,output)

    def test_no_provider_is_not_configured(self):
        self.assertEqual(SheetChecks(self.store,self.registry).compare(self.manager,'erp','sheet-binding')['state'],'not_configured')

    def test_read_failure_denial_and_unknown_are_not_empty(self):
        def failed(*_):raise OSError('private provider error')
        def denied(*_):raise PermissionError('private provider error')
        for callback,state in ((failed,'unavailable'),(denied,'permission_denied'),(lambda *_:None,'unknown')):
            with self.subTest(state=state):
                result=self.checks(callback).compare(self.manager,'erp','sheet-binding')
                self.assertEqual(result['state'],state)
                self.assertNotIn('private',json.dumps(result))
                self.assertNotIn('observed_empty',result)

    def test_observed_zero_rows_is_distinct(self):
        self.before=replace(self.before,rows=());self.after=self.before
        result=self.checks().compare(self.manager,'erp','sheet-binding')
        self.assertEqual(result['state'],'compatible')
        self.assertTrue(result['observed_empty'])

    def test_wrong_company_does_not_call_provider(self):
        result=self.checks(company='other').compare(self.manager,'erp','sheet-binding')
        self.assertEqual(result['state'],'permission_denied');self.assertEqual(self.calls,[])

    def test_wrong_snapshot_source_does_not_compare(self):
        self.before=replace(self.before,spreadsheet='other')
        self.assertEqual(self.checks().compare(self.manager,'erp','sheet-binding')['state'],'permission_denied')

    def test_stale_or_future_source_receipt(self):
        for time in (1,1001):
            self.before=replace(self.before,observed_at=time)
            self.assertEqual(self.checks().compare(self.manager,'erp','sheet-binding')['state'],'stale')

    def test_revocation_after_first_observer_prevents_second(self):
        calls=[]
        def revoke(*_):
            calls.append('called')
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return self.before
        with self.assertRaises(PermissionDenied):self.checks(revoke).compare(self.manager,'erp','sheet-binding')
        self.assertEqual(calls,['called'])

    def test_shape_and_nonfinite_data_rejected(self):
        with self.assertRaises(ValueError):replace(self.before,rows=(SheetRow('x',(1,)),))
        with self.assertRaises(ValueError):SheetRow('x',(float('nan'),))

    def test_changed_data_changes_exact_package_fingerprint(self):
        first=self.compare()['fingerprint']
        self.after=replace(self.before,rows=(replace(self.before.rows[0],values=(1,True,2)),self.before.rows[1]))
        self.assertNotEqual(first,self.compare()['fingerprint'])
