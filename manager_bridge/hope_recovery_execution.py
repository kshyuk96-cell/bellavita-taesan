"""One claimed QA run: real exits at five phases, source query recovery and CAS race."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import json
import threading
import time
from .hope_service_runtime import execute_actual_service
from .hope_persistent_review import certify_stopped
from .qa_runtime import QAInterrupted
from .store import Conflict,digest

PHASES=('before-hold','after-hold','before-store','after-store','before-response')


def execute_recovery_suite(profile,heartbeat,secrets):
    receipts={}
    def run(role):
        report=execute_actual_service(profile.service,profile.adapter_root,timeout=profile.timeout,entrypoint='run-batch05.mjs',exchange=profile.exchange,inventory_contract=profile.inventory_contract,business=profile.business,business_contract=profile.business_contract,runtime_secrets=secrets,role=role,heartbeat=heartbeat)
        report['stop_certificate']=certify_stopped(profile.exchange,report,profile.business_contract)
        return report
    receipts['setup']=run('setup')
    for phase in PHASES:
        report=run('crash-'+phase)
        if report.get('schema')!='hope-persistent-interruption/v1' or report.get('exit_code')!=86:raise Conflict('controlled process interruption not observed')
        with sqlite3.connect(profile.exchange/'inventory.sqlite') as db:
            db.row_factory=sqlite3.Row
            op=db.execute('SELECT * FROM recovery_operations WHERE executor=?',(report['runtime_provenance']['container_name'],)).fetchone()
            if op is None or op['phase']!=phase:raise Conflict('interruption checkpoint not committed at expected phase')
            lease=db.execute('SELECT status FROM inventory_leases WHERE key=?',(op['key'],)).fetchone()
        with sqlite3.connect(profile.business/'review.sqlite') as db:
            row=db.execute('SELECT revision,payload FROM review_workspaces WHERE id=?',(op['workspace'],)).fetchone()
        document=json.loads(row[1]);record=next((r for r in document['meta']['requests'] if r['id']==op['request_id']),None)
        expected_recorded=phase in ('after-store','before-response')
        if bool(record)!=expected_recorded or record and record['fingerprint']!=op['fingerprint']:raise Conflict('native business checkpoint differs from interruption phase')
        report['checkpoint']=dict(phase=op['phase'],request_id=op['request_id'],workspace=op['workspace'],generation=op['generation'],revision_before=op['revision_before'],revision_after=row[0],native_request_recorded=bool(record),lease_status=lease['status'] if lease else None)
        receipts[phase]=report
    receipts['recovery']=run('recover')
    stop=threading.Event()
    def raced(role):
        def fence():
            heartbeat()
            if stop.is_set():raise QAInterrupted('persistent_race_fence')
        report=execute_actual_service(profile.service,profile.adapter_root,timeout=profile.timeout,entrypoint='run-batch05.mjs',exchange=profile.exchange,inventory_contract=profile.inventory_contract,business=profile.business,business_contract=profile.business_contract,runtime_secrets=secrets,role=role,heartbeat=fence)
        report['stop_certificate']=certify_stopped(profile.exchange,report,profile.business_contract);return report
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(raced,role) for role in ('race-a','race-b')]
        try:
            until=time.monotonic()+20;barrier=[]
            while time.monotonic()<until:
                heartbeat()
                with sqlite3.connect(profile.exchange/'inventory.sqlite',timeout=5) as db:
                    db.row_factory=sqlite3.Row;barrier=[dict(r) for r in db.execute('SELECT * FROM recovery_barrier ORDER BY role')]
                if len(barrier)==2:break
                if any(f.done() for f in futures):raise Conflict('worker exited before business CAS barrier')
                time.sleep(.05)
            if len(barrier)!=2 or len({r['revision'] for r in barrier})!=1 or len({r['request_id'] for r in barrier})!=1:raise Conflict('same business revision/request race not established')
            with sqlite3.connect(profile.exchange/'inventory.sqlite') as db:db.execute('UPDATE recovery_gate SET released=1 WHERE id=1')
            receipts['race']=[f.result(timeout=90) for f in futures]
        except Exception:
            stop.set();raise
    with sqlite3.connect(profile.exchange/'inventory.sqlite') as db:
        db.row_factory=sqlite3.Row;receipts['cas_barrier']=[dict(r) for r in db.execute('SELECT * FROM recovery_barrier ORDER BY role')]
    receipts['unknown']=run('complete-unknown')
    main=run('main');main['recovery_execution']=receipts
    return main


class PersistentRecoveryEngine:
    def __init__(self,registry,manifest,evaluator,secrets):
        from .hope_registered_runtime import HopeEngine
        self.registry,self.manifest,self.evaluator,self.secrets=registry,manifest,evaluator,secrets
        self.ready=HopeEngine(registry,manifest,evaluator);self.raw=None
    def probe(self):return self.ready.probe()
    def execute(self,profile,heartbeat):
        pin=profile.fingerprint
        def fenced():
            heartbeat()
            if self.registry.resolve(self.manifest).fingerprint!=pin:raise QAInterrupted('persistent_profile_changed')
        self.raw=execute_recovery_suite(profile,fenced,self.secrets)
        report=self.evaluator(self.raw)
        return report,dict(schema='qa-execution-receipt/v1',profile_digest=pin,source_revision=profile.revision,isolation_verified=self.raw['artifacts']['confinement']['verified'],network='none',process_exited=True,report_observation_digest=digest(report))
