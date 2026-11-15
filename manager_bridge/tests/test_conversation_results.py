from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from openpyxl import load_workbook
from manager_bridge.store import Store, NotFound, PermissionDenied, Conflict
from manager_bridge.conversation_results import ResultHistory, count_summary

class ResultTests(unittest.TestCase):
    def setUp(self):
        root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now=1000
        self.store=Store(root/'private.sqlite3',clock=lambda:self.now)
        self.history=ResultHistory(self.store,ttl=60)
        self.scope=('tenant','employee','project','channel','conversation','config')
    def result(self,items=None,**extra):
        return dict(action='orders.list',query={'date':'2026-11-15'},items=items if items is not None else [{'id':'13370','stableUid':'stable','status':'접수'}],source={'reference':'fixture','observed_at':self.now},provider_state='ok',**extra)
    def test_scope_and_expiry_block_export(self):
        identifier=self.history.remember(self.scope,self.result())
        with self.assertRaises(NotFound):self.history.xlsx(('tenant','another','project','channel','conversation','config'),identifier,lambda:True)
        self.now+=60
        with self.assertRaises(NotFound):self.history.xlsx(self.scope,identifier,lambda:True)
    def test_permission_checked_twice_and_not_configured_denied(self):
        identifier=self.history.remember(self.scope,self.result())
        for authorize in (None,lambda:False,lambda:1):
            with self.assertRaises(PermissionDenied):self.history.xlsx(self.scope,identifier,authorize)
        calls=iter([True,False])
        with self.assertRaises(PermissionDenied):self.history.xlsx(self.scope,identifier,lambda:next(calls))
    def test_export_only_selected_snapshot_and_formula_injection(self):
        identifier=self.history.remember(self.scope,self.result([{'id':'13370','sender':' \t=HYPERLINK("x")','address':'@SUM(1)','amount':-5}]))
        data=self.history.xlsx(self.scope,identifier,lambda:True)
        workbook=load_workbook(BytesIO(data));sheet=workbook['조회 결과']
        fields=[c.value for c in sheet[1]];row=dict(zip(fields,[c.value for c in sheet[2]]))
        self.assertTrue(row['sender'].startswith("'"));self.assertTrue(row['address'].startswith("'"));self.assertEqual(row['amount'],-5)
        self.assertTrue(all(c.data_type!='f' for row in sheet for c in row))
        self.assertEqual(workbook['조회 범위']['B2'].value,'확인되지 않음')
    def test_actual_diff_and_reused_row_are_not_silently_same_order(self):
        self.history.remember(self.scope,self.result())
        self.history.remember(self.scope,self.result([{'id':'13370','stableUid':'new-order','status':'배송완료'}]))
        diff=self.history.compare(self.scope)
        self.assertEqual(diff['appeared'],['13370']);self.assertEqual(diff['absent_from_current_display'],['13370']);self.assertFalse(diff['source_deletions_inferred'])
    def test_changed_fields_come_from_actual_snapshots(self):
        self.history.remember(self.scope,self.result())
        self.history.remember(self.scope,self.result([{'id':'13370','stableUid':'stable','status':'배송완료'}]))
        self.assertEqual(self.history.compare(self.scope)['changed'],[{'id':'13370','fields':['status']}])
    def test_missing_prior_different_query_and_duplicate_id(self):
        self.history.remember(self.scope,self.result())
        with self.assertRaises(NotFound):self.history.compare(self.scope)
        second=self.result();second['query']={};self.history.remember(self.scope,second)
        with self.assertRaises(Conflict):self.history.compare(self.scope)
        self.history.remember(self.scope,self.result([{'id':'1'},{'id':'1'}]))
        self.history.remember(self.scope,self.result([{'id':'1'},{'id':'1'}]))
        with self.assertRaises(Conflict):self.history.compare(self.scope)
    def test_counts_require_explicit_coverage_not_provider_limit(self):
        result=self.result(total_matched=100,truncated=True)
        self.assertIsNone(count_summary(result)['total'])
        result['coverage']={'verified':True,'complete':True,'receipt':'trusted-host-receipt'}
        self.assertEqual(count_summary(result)['total'],100)
        result['coverage']['verified']=1
        self.assertIsNone(count_summary(result)['total'])
    def test_retention_bounded_to_two_and_clear(self):
        first=self.history.remember(self.scope,self.result())
        for _ in range(2):self.history.remember(self.scope,self.result())
        with self.assertRaises(NotFound):self.history.xlsx(self.scope,first,lambda:True)
        self.history.clear(self.scope)
        with self.assertRaises(NotFound):self.history.current(self.scope)
    def test_unavailable_and_oversized_results_are_not_recorded_as_empty(self):
        result=self.result();result['provider_state']='unavailable'
        with self.assertRaises(ValueError):self.history.remember(self.scope,result)
        with self.assertRaises(ValueError):self.history.remember(self.scope,self.result([{'id':str(n)} for n in range(101)]))
