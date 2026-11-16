from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from manager_bridge.handoff_intake import TestAccounts,prepare,draft_report,read_checklist,target_url,approve_expectations
from manager_bridge.handoff_gate import VerifiedEvidence,final_review
from manager_bridge.handoff_exports import export_package
from manager_bridge.handoff_probe import probe_url,validate_addresses
from manager_bridge.store import Conflict


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.accounts=TestAccounts([dict(role='member',username='private-user@example.invalid',password='PRIVATE_PASS_7!')])
        self.intake=prepare('https://test.example.invalid/','- [x] 결제 P3 통과\n- [x] 다른 회사 권한 차단',self.accounts,project='고객 서비스')
        self.report=draft_report(self.intake)

    def evidence(self,**changes):
        source='a'*40;rows=[];extensions=[]
        for row in self.intake['plan']['scenarios']:
            rows.append(dict(id=row['id'],source_revision=source,plan_digest=self.intake['plan_digest'],required=row['required'],checks={k:dict(status='pass',scope=k,reference='verified-artifact:'+k) for k in row['required']},independent_expectation_verified=True,actual={'observed':1},steps=['trusted verifier executed'],tested_at='2026-11-16T00:00:00Z',environment=dict(url=self.intake['target'],browser='fixture browser',device='fixture desktop',os='fixture OS')))
        for row in self.intake['plan']['extensions']:extensions.append(dict(id=row['id'],source_revision=source,plan_digest=self.intake['plan_digest'],status='pass',checks={k:dict(status='pass',scope=k,reference='verified-extra:'+k) for k in row['required']}))
        values=dict(plan_digest=self.intake['plan_digest'],source_revision=source,source_window_verified=True,adapter_version='reviewed-host-v1',capabilities=frozenset({'ui','api','ledger','formula','policy','pg','cross_user'}),results=tuple(rows),extension_results=tuple(extensions))
        values.update(changes);return VerifiedEvidence(**values)

    def review(self,report=None,verified=None):return final_review(report or self.report,verified or self.evidence(),trusted_plan=self.intake['plan'])

    def test_developer_pass_not_execution_and_money_cannot_be_lowered(self):
        self.assertEqual('claimed_pass',self.intake['developer_claims'][0]['developer_claimed_status']);self.assertEqual('P0',self.report['scenarios'][0]['priority'])
        self.assertEqual(0,self.report['summary']['counts']['PASS']);self.assertFalse(self.report['summary']['customer_ready'])
        self.assertEqual(14,len(self.report['qa_added_checks']))

    def test_private_fields_never_exported_and_commands_not_executed(self):
        text='- PRIVATE_PASS_7! private-user@example.invalid 가입\n```sh\nrm -rf /\n```\n- token=secret-token 검증'
        intake=prepare('https://example.invalid/',text,self.accounts);serialized=str(intake)
        self.assertNotIn('PRIVATE_PASS_7!',serialized);self.assertNotIn('private-user@example.invalid',serialized);self.assertNotIn('secret-token',serialized);self.assertNotIn('rm -rf',serialized)
        self.assertNotIn('PRIVATE_PASS',repr(self.accounts))
        structured=prepare('https://example.invalid/','{"tests":[{"title":"API 확인","expected":{"token":"PRIVATE_TOKEN_IN_LIST"}}]}',self.accounts)
        self.assertNotIn('PRIVATE_TOKEN_IN_LIST',str(structured))

    def test_hope_reference_keeps_all38_customer_rows(self):
        result=prepare('https://example.invalid/','- 가입 통과',self.accounts,template='hope-tour')
        self.assertEqual(38,len(result['plan']['scenarios']));self.assertEqual('MEM-01',result['plan']['scenarios'][0]['id']);pay=next(r for r in result['plan']['scenarios'] if r['id']=='PAY-10');self.assertIn('pg',pay['required']);self.assertEqual('pg',pay['check_capabilities']['pg'])

    def test_no_missing_or_overlong_requirements_silently_dropped(self):
        for text in ('', '\n'.join('- item'+str(i) for i in range(301)), '- '+('x'*2001)):
            with self.assertRaises(ValueError):prepare('https://example.invalid/',text,self.accounts)
        with self.assertRaises(ValueError):target_url('https://user:pass@example.invalid/')
        with self.assertRaises(ValueError):target_url('https://example.invalid/?token=secret')

    def test_complete_trusted_run_recommends_review_not_customer_approval(self):
        self.assertFalse(self.review()['summary']['customer_ready'])
        self.intake=approve_expectations(self.intake,{r['id']:dict(description='독립 기준 대조',values={'observed':1}) for r in self.intake['plan']['scenarios']},approved_by='qa-manager',basis='reviewed business reference')
        self.report=draft_report(self.intake)
        result=self.review();self.assertTrue(result['summary']['customer_ready']);self.assertFalse(result['summary']['customer_acceptance_approved']);self.assertFalse(result['summary']['operational_authorization']);self.assertEqual([],result['missing_evidence'])
        incomplete=list(deepcopy(self.evidence().results));incomplete[0]['tested_at']=None
        self.assertFalse(self.review(verified=self.evidence(results=tuple(incomplete)))['summary']['customer_ready'])

    def test_source_window_and_missing_capability_withhold(self):
        for evidence in (self.evidence(source_window_verified=False),self.evidence(capabilities=frozenset({'api'}))):self.assertFalse(self.review(verified=evidence)['summary']['customer_ready'])

    def test_claimed_pass_without_actual_results_cannot_survive_final_review(self):
        report=deepcopy(self.report)
        for row in report['scenarios']:row['status']='PASS'
        result=self.review(report,self.evidence(results=(),extension_results=()));self.assertFalse(result['summary']['customer_ready']);self.assertEqual(2,result['summary']['counts']['NOT RUN'])

    def test_contract_removal_priority_downgrade_and_stale_result_rejected(self):
        for attack in ('remove','priority','required'):
            report=deepcopy(self.report)
            if attack=='remove':report['scenarios'].pop()
            elif attack=='priority':report['scenarios'][0]['priority']='P3'
            else:report['required_evidence'][report['scenarios'][0]['test_id']]=[]
            with self.subTest(attack=attack),self.assertRaises(Conflict):self.review(report)
        evidence=self.evidence();rows=list(deepcopy(evidence.results));rows[0]['source_revision']='b'*40
        with self.assertRaises(Conflict):self.review(verified=self.evidence(results=tuple(rows)))
        with self.assertRaises(Conflict):final_review(self.report,{'PASS':True},trusted_plan=self.intake['plan'])

    def test_unexecuted_qa_extensions_and_critical_defects_withhold(self):
        self.assertFalse(self.review(verified=self.evidence(extension_results=()))['summary']['customer_ready'])
        for defect in (dict(id='BUG',severity='S1',status='OPEN'),dict(id='BUG',severity='S2',status='CLOSED')):
            self.assertFalse(self.review(verified=self.evidence(defects=(defect,)))['summary']['customer_ready'])

    def test_word_excel_reopen_and_formula_injection_escaped(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as folder:
            intake=prepare('https://example.invalid/','- =HYPERLINK("malicious")',self.accounts);report=draft_report(intake);report['scenarios'][0]['actual']={'ui':0,'server':0,'pg':0}
            result=export_package(intake,report,folder);self.assertTrue(result['word_reopened']);self.assertTrue(result['excel_reopened'])
            book=load_workbook(Path(folder)/'customer-report.xlsx',read_only=True)
            self.assertTrue(book['시나리오결과']['E2'].value.startswith("'="))
            import zipfile
            with zipfile.ZipFile(Path(folder)/'customer-report.docx') as archive:self.assertIn('화면: 0',archive.read('word/document.xml').decode())
            for path in Path(folder).glob('*.json'):self.assertNotIn('PRIVATE_PASS_7!',path.read_text())

    def test_docx_is_read_as_data(self):
        from docx import Document
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'checklist.docx';doc=Document();doc.add_paragraph('결제 금액 대조');doc.save(path)
            self.assertIn('결제 금액',read_checklist(path))


class ReadOnlyProbeTests(unittest.TestCase):
    def test_private_destinations_refused(self):
        for addresses in (['127.0.0.1'],['169.254.169.254'],['8.8.8.8','10.0.0.1'],['::1']):
            with self.assertRaises(ValueError):validate_addresses(addresses)
        self.assertEqual('127.0.0.1',validate_addresses(['127.0.0.1'],local_fixture=True))

    def test_redirect_not_followed_and_no_post_or_auth(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                seen.append((self.command,self.path,self.headers.get('Authorization'),self.headers.get('Cookie')))
                self.send_response(302);self.send_header('Location','http://169.254.169.254/private');self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            result=probe_url('http://127.0.0.1:'+str(server.server_port)+'/',local_fixture=True)
            self.assertEqual('redirect_not_followed',result['verdict']);self.assertFalse(result['source_revision_verified']);self.assertEqual([('GET','/',None,None)],seen)
        finally:server.shutdown();server.server_close();thread.join(timeout=3)

if __name__=='__main__':unittest.main()
