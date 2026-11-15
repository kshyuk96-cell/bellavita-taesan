from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from manager_bridge.store import Store, PermissionDenied, NotFound
from manager_bridge.maintenance_registry import Registry
from manager_bridge.representative_api import dispatch
from manager_bridge.cloud_review_receipts import ReviewProbe,ReviewSnapshot,ReviewComment


class Handler:
    def __init__(self,server):self.server=server;self.result=None
    def respond(self,status,result):self.status,self.result=status,result


class CloudReceiptAPI(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.now=1000
        self.store=Store(self.root/'db.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*']);self.registry=Registry(self.store)
        self.registry.register(self.owner,'a',{'company':'a','name':'fixture','source_kind':'github','source_ref':'fixture/a',
            'branch':'work','services':[],'maintenance_enabled':False})
        self.manager=self.store.add_token('manager','manager',['a'])
        self.worker=self.store.add_token('worker','worker',['a']);self.server=SimpleNamespace(store=self.store)

    def call(self,body,token=None):
        h=Handler(self.server)
        self.assertTrue(dispatch(h,'POST','/v1/representative/runtime/receipts/inspect',token or self.manager,body))
        self.assertEqual(h.status,200);return h.result['result']

    def test_default_is_unconnected_without_execution_claim(self):
        r=self.call({'project':'a'})
        self.assertEqual(r['state'],'not_configured');self.assertFalse(r['development_execution_verified'])
        self.assertFalse(r['existing_cloud_windows_connected'])

    def test_request_cannot_inject_a_review_or_callback(self):
        for field in ('probes','review_verified','head_sha','repository','observe'):
            with self.subTest(field=field),self.assertRaises(ValueError):self.call({'project':'a',field:'anything'})

    def test_worker_and_other_project_do_not_read_manager_receipts(self):
        with self.assertRaises(PermissionDenied):self.call({'project':'a'},self.worker)
        with self.assertRaises(NotFound):self.call({'project':'other'})

    def test_only_host_snapshot_can_prove_exact_head_review(self):
        body='<!-- codex-review-state:v1 {"code_review":{"commit_sha":"'+('a'*40)+'","has_any_findings":false,"has_unresolved_findings":false,"status":"completed"}} -->'
        snapshot=ReviewSnapshot('a','fixture/a','work',1,'a'*40,
            (ReviewComment(2,'chatgpt-codex-connector[bot]',body,self.now),),(),self.now)
        self.server.cloud_review_probes={'a':ReviewProbe('fixture/a','work',1,'a'*40,1,lambda _:snapshot)}
        r=self.call({'project':'a'})
        self.assertEqual(r['state'],'review_completed');self.assertTrue(r['review_verified'])
        self.assertFalse(r['development_execution_verified']);self.assertFalse(r['sandbox_verified'])
