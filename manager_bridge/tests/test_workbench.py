import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from manager_bridge.control import request
from manager_bridge.server import make_server
from manager_bridge.store import Store, Conflict, PermissionDenied
from manager_bridge.workbench import Workbench


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store=Store(self.root/'manager.sqlite3',clock=lambda:1000)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.store.register_project(self.owner,'a','fixture/a')
        self.store.register_project(self.owner,'b','fixture/b')
        self.manager=self.store.add_token('manager','manager',['a'])
        self.worker=self.store.add_token('worker','worker',['a'])
        self.workbench=Workbench(self.store)
        self.task=self.store.submit(self.manager,'one',{'project':'a','repository':'fixture/a','base_sha':'a'*40,
            'description':'<img src=x onerror=alert(1)> untrusted request','checks':['fixture'],'kind':'test'})
        self.package={'summary':'fixture change','impact':'fixture only','risks':'fixture risk',
            'candidate_sha':'a'*40,'rollback_sha':'a'*40,'pr_url':'https://github.com/fixture/a/pull/1',
            'evidence_references':['fixture:tests-pass']}

    def reviewed(self, passed=True):
        task=self.task
        self.store.approve(self.owner,task['id'],1,task['manifest_digest'])
        claim=self.store.claim(self.worker,'a')
        self.store.report(self.worker,task['id'],claim['lease_token'],{
            'head_sha':'a'*40,'summary':'fixture report','checks':[{'name':'fixture','passed':passed}]})
        if passed:
            from manager_bridge.store import digest
            with self.store._transaction() as c:
                reported=self.store._public(self.store._get(c,self.store._authenticate(c,self.manager),task['id']))
                # Fixture-only independent fact. Production facts are created only
                # by supervisor.verify_report after actual artifact/test checks.
                c.execute('INSERT INTO bridge_verified_candidates VALUES (?,?,?,?,?,?)',
                    (task['id'],digest(reported['report']),'a'*40,'f'*64,0,1000))
        return self.store.review(self.manager,task['id'],1,task['manifest_digest'],'fixture:independent')

    def test_dashboard_project_scope_and_worker_denial(self):
        self.assertEqual([row['project'] for row in self.workbench.projects(self.manager)],['a'])
        result=self.workbench.dashboard(self.manager,'a')
        self.assertEqual(result['counts']['pending_approval'],1)
        self.assertNotIn('untrusted request',json.dumps(result))
        self.assertFalse(result['background_execution_guaranteed'])
        for operation in (lambda:self.workbench.dashboard(self.worker,'a'),lambda:self.workbench.card(self.worker,self.task['id'])):
            with self.assertRaises(PermissionDenied):operation()

    def test_owner_development_card_is_exact_scope_and_not_deployment(self):
        card=self.workbench.card(self.owner,self.task['id'])
        self.assertTrue(card['can_approve_development'])
        self.assertEqual(card['manifest_digest'],self.task['manifest_digest'])
        self.assertFalse(card['deployment_executed'])
        self.assertFalse(self.workbench.card(self.manager,self.task['id'])['can_approve_development'])

    def test_failed_or_unreviewed_result_cannot_be_final_review_package(self):
        with self.assertRaises(Conflict):self.workbench.put_package(self.manager,self.task['id'],self.package)
        self.reviewed(False)
        with self.assertRaises(Conflict):self.workbench.put_package(self.manager,self.task['id'],self.package)

    def test_review_card_is_immutable_and_stale_candidate_acknowledgement_is_rejected(self):
        self.reviewed()
        first=self.workbench.put_package(self.manager,self.task['id'],self.package)
        same=self.workbench.put_package(self.manager,self.task['id'],copy.deepcopy(self.package))
        self.assertEqual(first,same)
        with self.assertRaises(PermissionDenied):self.workbench.acknowledge(self.manager,self.task['id'],1,first['package_digest'])
        approved=self.workbench.acknowledge(self.owner,self.task['id'],1,first['package_digest'])
        self.assertFalse(approved['deployment_executed'])
        self.assertEqual(approved['candidate_sha'],'a'*40)
        second=self.workbench.put_package(self.manager,self.task['id'],self.package|{'risks':'updated fixture risk'})
        self.assertEqual(second['revision'],2)
        with self.assertRaises(Conflict):self.workbench.acknowledge(self.owner,self.task['id'],1,first['package_digest'])
        self.assertIsNone(self.workbench.card(self.owner,self.task['id'])['review_acknowledgement'])
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM bridge_review_cards WHERE approval IS NOT NULL').fetchone()[0],1)

    def test_other_candidate_sha_and_uncommitted_artifact_cannot_be_acknowledged(self):
        self.reviewed()
        with self.assertRaises(Conflict):
            self.workbench.put_package(self.manager,self.task['id'],self.package|{'candidate_sha':'b'*40})
        with self.store._transaction() as c:
            c.execute('UPDATE bridge_verified_candidates SET dirty=1 WHERE task=?',(self.task['id'],))
        with self.assertRaises(Conflict):
            self.workbench.put_package(self.manager,self.task['id'],self.package)

    def test_package_requires_matching_repository_and_no_secret_in_content(self):
        self.reviewed()
        for package in (self.package|{'pr_url':'https://github.com/other/repo/pull/1'},
                        self.package|{'summary':self.manager}):
            with self.assertRaises(ValueError):self.workbench.put_package(self.manager,self.task['id'],package)

    def test_real_http_dashboard_is_public_shell_with_private_data_and_security_headers(self):
        server=make_server(self.store,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url='http://127.0.0.1:'+str(server.server_address[1])
        with urlopen(url+'/dashboard') as response:
            html=response.read().decode()
            self.assertIn('태산이 개발 관리',html)
            self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
            self.assertNotIn(self.manager,html)
        self.assertEqual(request(url,self.worker,'GET','/v1/projects')['status'],403)
        board=request(url,self.manager,'GET','/v1/dashboard?project=a')['dashboard']
        self.assertEqual(len(board['tasks']),1)
        self.assertEqual(request(url,self.manager,'GET','/v1/dashboard?project=b')['status'],404)
        card=request(url,self.owner,'GET','/v1/tasks/'+self.task['id']+'/review-card')['card']
        self.assertTrue(card['can_approve_development'])
        self.assertEqual(request(url,self.worker,'GET','/v1/tasks/'+self.task['id']+'/review-card')['status'],403)


if __name__=='__main__':unittest.main()
