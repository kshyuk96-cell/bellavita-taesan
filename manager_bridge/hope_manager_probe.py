"""Exercise the real QA HTTP API using a temporary nonproduction manager database."""
from copy import deepcopy
from pathlib import Path
import base64
import tempfile
from docx import Document
from openpyxl import Workbook
from .store import Conflict,digest
from .control import request
from .qa_jobs import QAJobs
from .hope_registered_runtime import HopeArtifacts
from taesan_core.universal_qa import Adapter,Scenario,Risk,build_contract,run_scenarios


def integration_checks(store,base,owner,worker,outsider,registry,profile,child_stop_probe):
    jobs=QAJobs(store);checks={};evidence={}
    def denied(fn):
        try:fn();return False
        except (Conflict,PermissionError):return True
    # Owner registration and immutable release; altered adapter hash tested in unit suite.
    registry.provision(owner,profile,enabled=False)
    manifest=dict(project=profile.project,adapter_release=profile.release,source_revision=profile.revision,execution_profile_digest=profile.fingerprint,max_seconds=90)
    revoked=denied(lambda:registry.resolve(manifest));registry.provision(owner,profile)
    workerdenied=denied(lambda:registry.provision(worker,profile))
    # A known release fingerprint is pinned; a caller cannot replace it with another.
    wrong=dict(manifest,execution_profile_digest='0'*64)
    checks[48]=dict(revokedDenied=revoked,immutableRelease=denied(lambda:registry.resolve(wrong)),workerProvisionDenied=workerdenied)
    suite=[Scenario('probe-money','probe','manager-probe','money',Risk('P0','원금 오판','중복요청','대조 필요'),{},750000,'independent literal 1200000-450000','single manager-protocol probe')]
    adapters=[Adapter('probe','probe-v1',lambda _:dict(actual=750000,evidence={'scope':'manager_protocol_fixture_not_customer_service'}))]
    contract=build_contract(suite,adapters);report=run_scenarios(suite,adapters,source_revision=profile.revision,seed=20261116,required_coverage=['money'])
    approval=jobs.approve(owner,project=profile.project,source_revision=profile.revision,contract=contract,coverage=['money'],adapter_release='manager-probe-v1')
    def receive(key):
        r=request(base,owner,'POST','/v1/qa/jobs',dict(approval=approval,request_key=key,task=key))
        if not r.get('ok'):raise Conflict('local HTTP intake failed')
        return r['job']
    def claim(job):return request(base,worker,'POST','/v1/qa/jobs/'+job['id']+'/claim',{})
    def bound(job):
        value=deepcopy(report);value['job_binding']=dict(project=profile.project,task=job['task'],approval=approval,adapter_release='manager-probe-v1',source_revision=profile.revision)
        return value
    def finish(job,value):
        value['report_digest']=digest({k:v for k,v in value.items() if k!='report_digest'})
        r=request(base,worker,'POST','/v1/qa/jobs/'+job['id']+'/result',dict(report=value))
        if not r.get('ok'):raise Conflict('local HTTP result failed')
        return r['job']
    first=receive('intake');cl=claim(first);claimed=cl.get('ok') is True
    remote=request(base,worker,'GET','/v1/qa/jobs/'+first['id']+'/execution')
    checks[49]=dict(httpIntake=True,claim=claimed,sourceBound=remote['execution']['manifest']['source_revision']==profile.revision)
    duplicate=receive('intake')['id']==first['id'];second=receive('concurrent')
    concurrent=claim(second).get('status')==409
    done=finish(first,bound(first));replay=claim(first).get('status')==409
    checks[51]=dict(duplicateSameJob=duplicate,projectConcurrentDenied=concurrent,replayDenied=replay)
    count=0
    for key in ('project','task','approval','adapter_release','source_revision'):
        job=receive('binding-'+key);claim(job);value=bound(job);value['job_binding'][key]='wrong'
        result=finish(job,value);count+=result['state']=='blocked' and 'job_binding_mismatch' in result['decision']['reasons']
    checks[50]=dict(bindingMutationsBlocked=count)
    missing=receive('missing');claim(missing);value=bound(missing);value['scenarios']=[];mr=finish(missing,value)
    tamper=receive('expected-tamper');claim(tamper);value=bound(tamper);value['scenarios'][0]['expected']=0;tr=finish(tamper,value)
    checks[54]=dict(missingCoverageBlocked=mr['state']=='blocked',expectedTamperBlocked=tr['state']=='blocked')
    cancel=receive('cancel');claim(cancel);cancelled=request(base,owner,'POST','/v1/qa/jobs/'+cancel['id']+'/cancel',{})
    checks[52]=dict(cancelUncertain=cancelled['job']['state']=='reconciliation_required',replayDenied=claim(cancel).get('status')==409,localChildStopped=child_stop_probe())
    jobs.reconcile(owner,cancel['id'],evidence_reference='local-api-probe:no-child-started; actual-source-stop-separately-recorded',process_stopped=True,effects_absent=True)
    # Queued probe did not execute; free it without retry.
    jobs.cancel(owner,second['id'])
    artifacts=HopeArtifacts(store)
    with tempfile.TemporaryDirectory(prefix='hope-artifact-probe-') as tmp:
        root=Path(tmp);Document().save(root/'customer-report.docx');Workbook().save(root/'customer-report.xlsx')
        artifacts.collect(owner,first['id'],root)
        fetched={}
        for name in ('customer-report.docx','customer-report.xlsx'):
            r=request(base,owner,'GET','/v1/qa/jobs/'+first['id']+'/artifacts/'+name)
            fetched[name]=base64.b64decode(r['artifact']['data'])==(root/name).read_bytes()
        unauth=request(base,outsider,'GET','/v1/qa/jobs/'+first['id']+'/artifacts')
        (root/'customer-report.docx').write_bytes(b'changed')
        immutable=denied(lambda:artifacts.collect(owner,first['id'],root))
    checks[53]=dict(wordRoundTrip=fetched['customer-report.docx'],excelRoundTrip=fetched['customer-report.xlsx'],unauthorizedDenied=unauth.get('status') in (403,404),immutable=immutable)
    evidence.update(protocol_fixture_scope='temporary real manager HTTP API; one-literal probe is not service evidence',checks=checks,final_probe_state=done['state'],automatic_retries=False)
    return checks,evidence
