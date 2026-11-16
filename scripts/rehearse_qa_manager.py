"""Reproduce real offline adapter -> manager HTTP -> mandatory task acceptance.

Temporary synthetic DB, loopback transport, no deployed service or credentials.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from manager_bridge.control import request
from manager_bridge.qa_jobs import QAJobs
from manager_bridge.server import make_server
from manager_bridge.store import Store, digest
from taesan_core.qa_order_ledger import suite, adapter
from taesan_core.universal_qa import build_contract


def main():
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    path = 'scripts/rehearse_qa_manager.py'
    if subprocess.check_output(['git','show',revision+':'+path],cwd=ROOT) != Path(__file__).read_bytes():
        raise ValueError('commit rehearsal source before execution')
    contract = build_contract(suite(), [adapter()])
    # Independent reviewed contract exists before execution/report intake.
    observed = json.loads(subprocess.check_output(
        [sys.executable, str(ROOT/'scripts/run_order_ledger_qa.py')],cwd=ROOT,text=True,timeout=60))
    if observed['contract'] != contract:
        raise ValueError('adapter contract changed')
    events = []
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder)/'manager.sqlite')
        owner = store.add_token('rehearsal-owner','owner',['*'])
        store.register_project(owner,'bellavita','kshyuk96-cell/bellavita-taesan')
        manager = store.add_token('rehearsal-manager','manager',['bellavita'])
        worker = store.add_token('rehearsal-worker','worker',['bellavita'])
        jobs = QAJobs(store)
        approval = jobs.approve(owner,project='bellavita',source_revision=revision,
            contract=contract,coverage=['ledger-date-time-channel'],
            adapter_release='reviewed-offline-ledger-v1',max_seconds=300)
        task = store.submit(manager,'rehearsal-development',dict(project='bellavita',
            repository='kshyuk96-cell/bellavita-taesan',base_sha=revision,
            description='Synthetic offline normalization verification',
            checks=['offline ledger scenarios'],kind='development'))
        jobs.require_for_task(owner,task['id'],approval)
        store.approve(owner,task['id'],task['version'],task['manifest_digest'])
        lease = store.claim(worker,'bellavita')
        store.report(worker,task['id'],lease['lease_token'],dict(head_sha=revision,
            summary='Offline component only',checks=[dict(name='ledger',passed=True)]))
        server = make_server(store,port=0)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base = 'http://127.0.0.1:'+str(server.server_address[1])
        try:
            review_path = '/v1/tasks/'+task['id']+'/review'
            review_body = dict(version=task['version'],digest=task['manifest_digest'],
                               evidence_reference='rehearsal:offline-component-observations')
            before = request(base,manager,'POST',review_path,review_body)
            assert before['status'] == 409, before
            events.append(dict(check='mandatory_qa_before_review',status=409,passed=True))
            for case in ('pass','critical_unmeasured','coverage_missing','source_mismatch','task_mismatch'):
                task_id = task['id'] if case=='pass' else 'rehearsal-'+case
                received = request(base,manager,'POST','/v1/qa/jobs',dict(
                    approval=approval,request_key='request-'+case,task=task_id))
                job = received['job']
                path = '/v1/qa/jobs/'+job['id']
                assert request(base,worker,'POST',path+'/claim',{})['ok']
                report = copy.deepcopy(observed['report'])
                report['job_binding'] = dict(project='bellavita',task=task_id,approval=approval,
                    adapter_release='reviewed-offline-ledger-v1',source_revision=revision)
                if case=='critical_unmeasured':
                    report['scenarios'][0].update(status='unmeasured',executed=False,asserted=False)
                if case=='coverage_missing': report['coverage']['required'] = []
                if case=='source_mismatch': report['source_revision'] = '0'*40
                if case=='task_mismatch': report['job_binding']['task'] = 'unrelated-task'
                report['report_digest'] = digest({k:v for k,v in report.items() if k!='report_digest'})
                result = request(base,worker,'POST',path+'/result',dict(report=report))['job']
                recovered = request(base,manager,'GET',path)['job']
                assert result == recovered
                expected_state = 'accepted' if case=='pass' else 'blocked'
                assert result['state'] == expected_state, result['decision']
                events.append(dict(check=case,state=result['state'],
                    reasons=result['decision']['reasons'],counts=report['counts'],
                    report_digest=report['report_digest'],recovered_identically=True,passed=True))
                if case=='pass':
                    replay = request(base,worker,'POST',path+'/claim',{})
                    assert replay['status']==409
                    events.append(dict(check='duplicate_execution_rejected',status=409,passed=True))
            reviewed = request(base,manager,'POST',review_path,review_body)
            assert reviewed['task']['state']=='reviewed', reviewed
            events.append(dict(check='mandatory_review_after_current_passing_qa',state='reviewed',passed=True))
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
    print(json.dumps(dict(source_revision=revision,contract_digest=digest(contract),
        adapter_versions=contract['adapter_versions'],scope='local_http_offline_real_component',
        scenario_counts=observed['report']['counts'],checks=events,
        operational_authorization=False,
        unverified=['deployed staging','live manager','UI','sheet writes','isolated executor lifecycle']),
        ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
