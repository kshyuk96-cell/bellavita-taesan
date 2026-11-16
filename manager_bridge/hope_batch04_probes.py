"""Independent container race and real persistent-manager restart probes."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from docx import Document
from openpyxl import Workbook
from .control import request
from .hope_service_runtime import execute_actual_service
from .hope_registered_runtime import HopeArtifacts
from .qa_jobs import QAJobs
from .qa_worker import QAClient,QAWorker
from .qa_runtime import QAInterrupted
from .store import Store,Conflict,digest
from taesan_core.universal_qa import Scenario,Risk,Adapter,build_contract,run_scenarios


def independent_race(profile):
    stop=threading.Event();evidence={};path=profile.exchange/'inventory.sqlite'
    def fence():
        if stop.is_set():raise QAInterrupted('race_uncertain_stop')
    def run(role):return execute_actual_service(profile.service,profile.adapter_root,timeout=profile.timeout,entrypoint='run-batch04.mjs',exchange=profile.exchange,inventory_contract=profile.inventory_contract,role=role,heartbeat=fence)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(run,role) for role in ('race-a','race-b')]
        try:
            until=time.monotonic()+20;ready=[]
            while time.monotonic()<until:
                with sqlite3.connect(path,timeout=5) as db:ready=[r[0] for r in db.execute('SELECT role FROM inventory_barrier ORDER BY role')]
                if ready==['race-a','race-b']:break
                if any(f.done() for f in futures):raise Conflict('race executor exited before shared barrier')
                time.sleep(.05)
            if ready!=['race-a','race-b']:raise Conflict('independent executor barrier not reached')
            with sqlite3.connect(path,timeout=5) as db:db.execute('UPDATE inventory_gate SET released=1 WHERE id=1')
            results=[f.result(timeout=90) for f in futures]
        except Exception:
            stop.set();raise
    names={r['runtime_provenance']['container_name'] for r in results}
    if len(names)!=2 or {r['role'] for r in results}!={'race-a','race-b'} or any(r['source_revision']!=profile.revision for r in results):raise Conflict('race source/executor mismatch')
    actual=dict(executors=len(names),barrierReady=len(ready),confirmed=sum(r['artifacts']['race']['confirmed'] for r in results),rejected=sum(r['artifacts']['race']['status']==409 for r in results))
    return actual,dict(scope='two independent network-none Docker executors, same SQLite file, release barrier after both ready',results=results,actual=actual)


def persistent_checks(store,manager,owner,worker,outsider,registry,profile):
    host={};evidence={};jobs=QAJobs(store)
    manifest=dict(project=profile.project,adapter_release=profile.release,source_revision=profile.revision,execution_profile_digest=profile.fingerprint,max_seconds=90)
    manager.restart();reopened=Store(store.path) if hasattr(store,'path') else None
    recovered=registry.resolve(manifest).fingerprint==profile.fingerprint
    denied=False
    try:registry.provision(worker,profile)
    except PermissionError:denied=True
    host[76]=dict(profileRecovered=recovered,workerProvisionDenied=denied)
    suite=[Scenario('persistent-probe','probe','persistence','probe',Risk('P0','증거소실','재시작','대조'),{},750000,'1200000-450000','protocol fixture, not customer-service evidence')]
    adapters=[Adapter('probe','persistent-probe-v1',lambda _:dict(actual=750000,evidence={'scope':'manager_protocol_fixture'}))]
    trusted=build_contract(suite,adapters);report=run_scenarios(suite,adapters,source_revision=profile.revision,seed=20261116,required_coverage=['probe'])
    approval=jobs.approve(owner,project=profile.project,source_revision=profile.revision,contract=trusted,coverage=['probe'],adapter_release='persistent-probe-v1',max_seconds=90)
    job=jobs.receive(owner,approval,'restart-probe','restart-probe');jobs.claim(worker,job['id'])
    manager.restart()
    class NeverExecute:
        calls=0
        def execute(self,*_):self.calls+=1;raise AssertionError('uncertain job cannot execute')
        def probe(self):self.calls+=1;raise AssertionError('uncertain job cannot probe')
    engine=NeverExecute();result=QAWorker(QAClient(manager.base,worker),registry,engine=engine).run_once(job['id'])
    replay=request(manager.base,worker,'POST','/v1/qa/jobs/'+job['id']+'/claim',{})
    host[77]=dict(restartUncertain=result['state']=='reconciliation_required',replayDenied=replay.get('status')==409,executionCalls=engine.calls)
    jobs.reconcile(owner,job['id'],evidence_reference='restart-protocol-probe: no execution dispatched; manager process stopped and restarted; independent effects absent',process_stopped=True,effects_absent=True)
    terminal=jobs.receive(owner,approval,'artifact-probe','artifact-probe');jobs.claim(worker,terminal['id'])
    report['job_binding']=dict(project=profile.project,task=terminal['task'],approval=approval,adapter_release='persistent-probe-v1',source_revision=profile.revision);report['report_digest']=digest({k:v for k,v in report.items() if k!='report_digest'})
    jobs.finish(worker,terminal['id'],report);artifacts=HopeArtifacts(store)
    with tempfile.TemporaryDirectory(prefix='persistent-artifact-probe-') as tmp:
        root=Path(tmp);Document().save(root/'customer-report.docx');Workbook().save(root/'customer-report.xlsx')
        artifacts.collect(owner,terminal['id'],root);artifacts.seal(owner,terminal['id'],['customer-report.docx','customer-report.xlsx'])
        manager.restart();round_trip=True
        for name in ('customer-report.docx','customer-report.xlsx'):
            result=request(manager.base,owner,'GET','/v1/qa/jobs/'+terminal['id']+'/artifacts/'+name)
            round_trip &= result.get('ok') is True and base64.b64decode(result['artifact']['data'])==(root/name).read_bytes()
        unauth=request(manager.base,outsider,'GET','/v1/qa/jobs/'+terminal['id']+'/artifacts')
        with store._transaction() as db:
            row=db.execute('SELECT * FROM hope_job_artifacts WHERE job=? AND name=?',(terminal['id'],'customer-report.docx')).fetchone();saved=tuple(row)
            db.execute('UPDATE hope_job_artifacts SET data=? WHERE job=? AND name=?',(b'corrupted',terminal['id'],'customer-report.docx'))
        corrupt=request(manager.base,owner,'GET','/v1/qa/jobs/'+terminal['id']+'/artifacts')
        with store._transaction() as db:
            db.execute('UPDATE hope_job_artifacts SET data=? WHERE job=? AND name=?',(row['data'],terminal['id'],'customer-report.docx'))
            db.execute('DELETE FROM hope_job_artifacts WHERE job=? AND name=?',(terminal['id'],'customer-report.docx'))
        missing=request(manager.base,owner,'GET','/v1/qa/jobs/'+terminal['id']+'/artifacts')
        with store._transaction() as db:db.execute('INSERT INTO hope_job_artifacts VALUES(?,?,?,?,?)',saved)
        with (root/'customer-report.docx').open('wb') as stream:stream.truncate(8*1024*1024+1)
        limit=False
        try:artifacts.collect(owner,terminal['id'],root)
        except ValueError:limit=True
    host[78]=dict(recovered=bool(round_trip),unauthorizedDenied=unauth.get('status') in (403,404),tamperDetected=corrupt.get('status')==409,missingDetected=missing.get('status')==409,limitDenied=limit)
    evidence.update(scope='real separate manager process + file SQLite; protocol jobs never dispatch customer code',checks=host,automatic_retries=False)
    return host,evidence
