import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from manager_bridge.qa_runtime import QAProfile, QARegistry, QASandbox, QAInterrupted
from manager_bridge.qa_worker import QAWorker, QAClient
from manager_bridge.qa_jobs import QAJobs
from manager_bridge.store import Store, Conflict, digest
from manager_bridge.server import make_server
from manager_bridge.tests import test_qa_admission


class QAFoundationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.root/'source';self.source.mkdir()
        (self.source/'sample.py').write_text('print("reviewed fixture")\n')
        def git(*args):
            return subprocess.check_output(['git','-C',str(self.source),*args],stderr=subprocess.DEVNULL,text=True).strip()
        git('init');git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fixture')
        self.revision = git('rev-parse','HEAD')
        self.profile = QAProfile('shop','release1',self.revision,self.source,Path(sys.prefix),'order-ledger-v1',1)
        self.registry = QARegistry([self.profile])
        self.store = Store(self.root/'manager.sqlite')
        self.owner = self.store.add_token('owner','owner',['*'])
        self.store.register_project(self.owner,'shop','fixture/shop')
        self.worker = self.store.add_token('worker','worker',['shop'])
        self.manager = self.store.add_token('manager','manager',['shop'])
        self.jobs = QAJobs(self.store)
        fixture = test_qa_admission.QAAdmissionTests()
        self.report = fixture.fixture();self.contract=fixture.expected_contract
        self.report['source_revision']=self.revision
        self.report['report_digest']=digest(self.report)
        self.approval = self.jobs.approve(self.owner,project='shop',source_revision=self.revision,
            contract=self.contract,coverage=['money'],adapter_release='release1',max_seconds=1,
            execution_profile_digest=self.profile.fingerprint)
        self.job=self.jobs.receive(self.owner,self.approval,'request1','task1')['id']

    def client(self):
        server=make_server(self.store,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def close():
            server.shutdown();thread.join(timeout=3);server.server_close()
        self.addCleanup(close)
        return QAClient('http://127.0.0.1:'+str(server.server_address[1]),self.worker)

    def engine(self, *, interrupt=False, available=True):
        outer=self
        class FixtureEngine:
            calls=0
            def probe(self):return dict(available=available,isolation_verified=available)
            def execute(self,profile,heartbeat):
                self.calls+=1
                heartbeat()
                if interrupt: raise QAInterrupted('fixture')
                report=copy.deepcopy(outer.report)
                report.pop('report_digest',None);report['report_digest']=digest(report)
                receipt=dict(schema='qa-execution-receipt/v1',profile_digest=profile.fingerprint,
                    source_revision=profile.revision,isolation_verified=True,network='none',process_exited=True,
                    report_observation_digest=digest(report))
                return report,receipt
        return FixtureEngine()

    def test_registry_rejects_duplicate_and_changed_source_or_profile(self):
        with self.assertRaises(ValueError):QARegistry([self.profile,self.profile])
        manifest=self.jobs.inspect(self.owner,self.job)['manifest']
        changed=dict(manifest,execution_profile_digest='0'*64)
        with self.assertRaises(Conflict):self.registry.resolve(changed)
        (self.source/'sample.py').write_text('changed')
        with self.assertRaises(Conflict):self.registry.resolve(manifest)

    def test_unavailable_isolation_leaves_job_unclaimed(self):
        engine=self.engine(available=False)
        outcome=QAWorker(self.client(),self.registry,engine=engine).run_once(self.job)
        self.assertEqual('sandbox_unavailable',outcome['state'])
        self.assertEqual('queued',self.jobs.get(self.owner,self.job)['state'])
        self.assertEqual(0,engine.calls)

    def test_worker_http_executes_and_preserves_receipt(self):
        engine=self.engine()
        outcome=QAWorker(self.client(),self.registry,engine=engine).run_once(self.job)
        self.assertEqual('accepted',outcome['state'])
        saved=self.jobs.get(self.owner,self.job)
        self.assertEqual(self.profile.fingerprint,saved['report']['execution_receipt']['profile_digest'])
        self.assertEqual(1,engine.calls)
        self.assertEqual('accepted',QAWorker(self.client(),self.registry,engine=engine).run_once(self.job)['state'])
        self.assertEqual(1,engine.calls)

    def test_interruption_requires_owner_reconciliation_not_retry(self):
        engine=self.engine(interrupt=True)
        self.assertEqual('reconciliation_required',QAWorker(self.client(),self.registry,engine=engine).run_once(self.job)['state'])
        with self.assertRaises(PermissionError):
            self.jobs.reconcile(self.manager,self.job,evidence_reference='fixture:stop',process_stopped=True,effects_absent=True)
        with self.assertRaises(ValueError):
            self.jobs.reconcile(self.owner,self.job,evidence_reference='fixture:stop',process_stopped=True,effects_absent=False)
        self.assertEqual('reconciled',self.jobs.reconcile(self.owner,self.job,
            evidence_reference='fixture:independent-stop-and-effects',process_stopped=True,effects_absent=True)['state'])
        self.assertFalse(self.jobs.get(self.owner,self.job)['decision']['admitted'])
        with self.assertRaises(Conflict):self.jobs.claim(self.worker,self.job)
        second=self.jobs.receive(self.owner,self.approval,'request2','task2')
        self.assertEqual('running',self.jobs.claim(self.worker,second['id'])['job']['state'])

    def test_worker_restart_and_local_stop_never_replay(self):
        self.jobs.claim(self.worker,self.job)
        engine=self.engine()
        self.assertEqual('reconciliation_required',QAWorker(self.client(),self.registry,engine=engine).run_once(self.job)['state'])
        self.assertEqual(0,engine.calls)

    def test_missing_execution_receipt_blocks_approval(self):
        self.jobs.claim(self.worker,self.job)
        report=copy.deepcopy(self.report)
        report['job_binding']=dict(project='shop',task='task1',approval=self.approval,
            adapter_release='release1',source_revision=self.revision)
        report.pop('report_digest',None);report['report_digest']=digest(report)
        result=self.jobs.finish(self.worker,self.job,report)
        self.assertEqual('blocked',result['state'])
        self.assertIn('execution_receipt_mismatch',result['decision']['reasons'])

    def test_lost_result_ack_is_read_back_without_resubmit(self):
        client=self.client();original=client.call;submits=[]
        def call(method,job,action=None,payload=None):
            result=original(method,job,action,payload)
            if action=='result':
                submits.append(job)
                return {'ok':False,'error':'connection_unverified'}
            return result
        client.call=call
        result=QAWorker(client,self.registry,engine=self.engine()).run_once(self.job)
        self.assertEqual('accepted',result['state']);self.assertTrue(result['result_recovered'])
        self.assertEqual([self.job],submits)

    def test_timeout_kills_child_and_keeps_untrusted_stdout_private(self):
        engine=QASandbox()
        real=subprocess.Popen
        children=[]
        with patch.object(engine,'probe',return_value={'isolation_verified':True}), \
             patch.object(engine.base,'_base',return_value=[]), \
             patch('manager_bridge.qa_runtime._archive_source'), \
             patch('manager_bridge.qa_runtime.subprocess.Popen', wraps=subprocess.Popen) as popen:
            # Override only fixture argv at process creation; no sandbox claim.
            def fixture(*args,**kwargs):
                child=real([sys.executable,'-c','import time; time.sleep(20)'],**kwargs)
                children.append(child)
                return child
            popen.side_effect=fixture
            with self.assertRaises(QAInterrupted):engine.execute(self.profile,lambda:None)
            self.assertEqual(1,len(children))
            self.assertIsNotNone(children[0].poll())

    def test_actual_sandbox_probe_is_typed(self):
        probe=QASandbox().probe()
        self.assertIs(type(probe['isolation_verified']),bool)
        self.assertEqual(probe['available'],probe['isolation_verified'])

    def test_output_limit_and_child_failure_kill_real_process(self):
        real=subprocess.Popen
        for script in ('import sys; sys.exit(9)', 'print("x"*2048); import time; time.sleep(20)'):
            children=[];engine=QASandbox()
            def fixture(*args,**kwargs):
                child=real([sys.executable,'-u','-c',script],**kwargs)
                children.append(child)
                return child
            with patch.object(engine,'probe',return_value={'isolation_verified':True}), \
                 patch.object(engine.base,'_base',return_value=[]), \
                 patch('manager_bridge.qa_runtime._archive_source'), \
                 patch('manager_bridge.qa_runtime.MAX_REPORT_BYTES',1024), \
                 patch('manager_bridge.qa_runtime.subprocess.Popen',side_effect=fixture):
                with self.assertRaises(QAInterrupted):engine.execute(self.profile,lambda:None)
                self.assertIsNotNone(children[0].poll())

    def test_expiry_persists_even_when_finish_conflicts(self):
        self.jobs.claim(self.worker,self.job)
        with self.store._transaction() as c:
            c.execute('UPDATE qa_jobs SET deadline=0 WHERE id=?',(self.job,))
        with self.assertRaises(Conflict):self.jobs.finish(self.worker,self.job,{})
        with self.store._transaction() as c:
            state=c.execute('SELECT state FROM qa_jobs WHERE id=?',(self.job,)).fetchone()[0]
        self.assertEqual('reconciliation_required',state)

    def test_http_reconciliation_keeps_audit_record(self):
        self.jobs.claim(self.worker,self.job)
        client=self.client()
        self.assertTrue(client.call('POST',self.job,'uncertain',dict(reason='worker_restarted'))['ok'])
        client.token=self.owner
        response=client.call('POST',self.job,'reconcile',dict(evidence_reference='fixture:independent-observation',
            process_stopped=True,effects_absent=True))
        self.assertEqual('reconciled',response['job']['state'])
        inspection=client.call('GET',self.job,'execution')['execution']
        self.assertEqual('fixture:independent-observation',inspection['reconciliation']['evidence'])

    def test_lost_claim_ack_never_starts_or_reclaims(self):
        client=self.client();original=client.call;claims=[]
        def call(method,job,action=None,payload=None):
            result=original(method,job,action,payload)
            if action=='claim':
                claims.append(job)
                return {'ok':False}
            return result
        client.call=call;engine=self.engine();worker=QAWorker(client,self.registry,engine=engine)
        self.assertEqual('claim_uncertain',worker.run_once(self.job)['state'])
        self.assertEqual('reconciliation_required',worker.run_once(self.job)['state'])
        self.assertEqual([self.job],claims);self.assertEqual(0,engine.calls)

    def test_stop_before_claim_does_not_consume_job(self):
        stop=threading.Event();stop.set();engine=self.engine()
        result=QAWorker(self.client(),self.registry,engine=engine,stop=stop).run_once(self.job)
        self.assertEqual('stopped',result['state'])
        self.assertEqual('queued',self.jobs.get(self.owner,self.job)['state'])
        self.assertEqual(0,engine.calls)

    def test_receipt_is_revalidated_and_bound_to_observed_report(self):
        from manager_bridge.qa_jobs import execution_receipt_matches
        result=QAWorker(self.client(),self.registry,engine=self.engine()).run_once(self.job)
        self.assertEqual('accepted',result['state'])
        saved=self.jobs.get(self.owner,self.job)['report']
        manifest=self.jobs.inspect(self.owner,self.job)['manifest']
        for key,value in [('isolation_verified',1),('profile_digest','0'*64),
                          ('report_observation_digest','0'*64),('network','host')]:
            altered=copy.deepcopy(saved);altered['execution_receipt'][key]=value
            self.assertFalse(execution_receipt_matches(altered,manifest))

    def test_timeout_remains_bounded_when_manager_heartbeat_stalls(self):
        import time
        real=subprocess.Popen;children=[];calls=[];release=threading.Event()
        def heartbeat():
            calls.append(1)
            if len(calls)>1:release.wait(5)
        def fixture(*args,**kwargs):
            child=real([sys.executable,'-c','import time; time.sleep(20)'],**kwargs)
            children.append(child);return child
        engine=QASandbox();started=time.monotonic()
        try:
            with patch.object(engine,'probe',return_value={'isolation_verified':True}), \
                 patch.object(engine.base,'_base',return_value=[]), \
                 patch('manager_bridge.qa_runtime._archive_source'), \
                 patch('manager_bridge.qa_runtime.subprocess.Popen',side_effect=fixture):
                with self.assertRaises(QAInterrupted):engine.execute(self.profile,heartbeat)
            self.assertLess(time.monotonic()-started,3)
            self.assertIsNotNone(children[0].poll())
        finally:release.set()
