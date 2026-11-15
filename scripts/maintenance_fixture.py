"""Run the maintenance pipeline on synthetic local data; never access live services."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.store import Store
from manager_bridge.regression_selection import RegressionSelector
from manager_bridge.maintenance_registry import Registry, Observation, ReadProbe, CredentialObservation
from manager_bridge.maintenance_checks import Checks, CheckHost, LocalFixtureExecutor
from manager_bridge.maintenance_data import BackupMetadata, BackupSpec, DataChecks, MigrationSpec
from manager_bridge.maintenance_cycle import Action, ActionResult, CheckOutcome, Cycle, Digest


def run():
    with tempfile.TemporaryDirectory(prefix='taesan-maintenance-proof-') as tmp:
        root=Path(tmp); checkout=root/'synthetic';checkout.mkdir()
        (checkout/'.taesan-synthetic-fixture').write_text('synthetic-only')
        (checkout/'check.py').write_text("import os,sqlite3\nassert os.environ['TAESAN_FIXTURE_ONLY']=='1'\nassert 'OPENAI_API_KEY' not in os.environ\nassert 'GOOGLE_APPLICATION_CREDENTIALS' not in os.environ\nc=sqlite3.connect(':memory:')\nc.execute('CREATE TABLE probe (n INTEGER)')\nc.execute('INSERT INTO probe VALUES (7)')\nassert c.execute('SELECT n FROM probe').fetchone()[0]==7\n")
        def git(*args):
            return subprocess.check_output(['/usr/bin/git','-c','core.hooksPath=/dev/null','-C',str(checkout),*args],stderr=subprocess.DEVNULL,text=True).strip()
        git('init');git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','Synthetic maintenance fixture')
        sha=git('rev-parse','HEAD');project='maintenance-fixture';source='fixture/maintenance'
        store=Store(root/'private'/'manager.sqlite3',clock=lambda:1000)
        owner=store.add_token('fixture-owner','owner',['*'])
        registry=Registry(store,probes={(project,'source'):ReadProbe(source,lambda *_:Observation('ready',source,'fixture/source',1000))},
            credential_observers={(project,'fixture'):lambda *_:CredentialObservation('fixture',project,'fixture',('read','test'),2000,'fixture/auth')},
            fixtures={'synthetic-repro':{'project':project,'check':'authorization_regression','source_sha':sha}})
        registry.register(owner,project,{'company':'fixture','name':'Synthetic maintenance','source_kind':'github','source_ref':source,'branch':'main','services':[],'maintenance_enabled':True})
        manager=store.add_token('fixture-manager','manager',[project])
        worker=store.add_token('fixture-worker','worker',[project],capabilities=('test',))
        health=registry.inspect(manager,project)
        credential=registry.credential(manager,project,'fixture',required=('read','test'))
        incident=registry.incident(manager,project,code='fixture_failure',target=source,severity='normal',reference='fixture/repro')
        reproduction=registry.reproduction(manager,project,incident['id'],fixture_id='synthetic-repro')
        checks={name:(sys.executable,str(checkout/'check.py')) for name in ('authorization_regression','approval_regression')}
        selector=RegressionSelector(checks,{'check.py':tuple(checks)})
        runner=Checks(store,registry,{project:CheckHost(checkout,sha,selector,LocalFixtureExecutor(root))})
        plan=runner.propose(manager,project,'fixture-checks',['check.py'])
        runner.approve(owner,plan['id'],plan['fingerprint'])
        def execute(context):
            report=runner.run(worker,plan['id'],parent_task_id=context.task_id)
            return ActionResult(sha,tuple(CheckOutcome(c['name'],'pass' if c['passed'] else 'fail') for c in report['checks']),'fixture/actual-subprocess')
        cycle=Cycle(store,registry,actions={(project,'fixture-tests'):Action('test',execute,lambda _:git('rev-parse','HEAD'),enabled=True)},enabled=True)
        task=store.submit(manager,'fixture-task',{'project':project,'repository':source,'base_sha':sha,'kind':'test','description':'Run synthetic isolated maintenance checks','checks':list(checks)})
        task=store.approve(owner,task['id'],task['version'],task['manifest_digest'])
        job=cycle.enqueue(manager,project,'fixture-job',task['id'],'fixture-tests')
        cycle.approve(owner,job['id'],job['fingerprint'])
        outcome=cycle.run_once(worker,project)
        backup=root/'synthetic-backup.sqlite3'
        with sqlite3.connect(backup) as c:
            c.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)')
            c.execute("INSERT INTO records VALUES (1,'synthetic-only')")
        content=backup.read_bytes()
        metadata=BackupMetadata('fixture',project,'fixture-backup','fixture-source',hashlib.sha256(content).hexdigest(),len(content),False,900,2000,'fixture/backup',readable=True)
        data=DataChecks(store,registry,backups={(project,'fixture-backup'):BackupSpec(project,'fixture-backup',lambda *_:metadata,str(backup),True)},
            migrations={(project,'add-column'):MigrationSpec(project,'add-column',('ALTER TABLE records ADD COLUMN tag TEXT',),{'records':('id','value','tag')})})
        facts=data.inspect(manager,project)
        restore=data.prepare(manager,project,'fixture-backup','add-column')
        data.approve(owner,restore['id'],restore['digest'])
        restored=data.run(manager,restore['id'],'fixture-restore')
        assert backup.read_bytes()==content
        digest=Digest(store,registry);report=digest.report(manager)
        digest.acknowledge(manager,report['fingerprint'])
        unchanged=digest.report(manager)
        assert outcome['state']=='done' and restored['state']=='passed'
        assert not unchanged['changed']
        return {'scope':'synthetic_local_fixture','health':health['state'],'credential_reference':credential['state'],
            'reproduction_state':reproduction['state'],'job_state':outcome['state'],
            'actual_subprocess_checks':outcome['result']['counts'],'worker_result_independently_verified':False,
            'backup_encrypted':facts['backups'][0]['encrypted'],'restore_state':restored['state'],
            'source_backup_unchanged':True,'ack_suppresses_duplicate':not unchanged['changed'],
            'live_ai_score':None,'operational_change':False,'schedule_created':False}


if __name__=='__main__':print(json.dumps(run(),ensure_ascii=False,indent=2))
