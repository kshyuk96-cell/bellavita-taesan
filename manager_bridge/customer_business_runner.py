"""Independent registered harness observations over isolated customer source.

Expected values stay in the manager's FrozenCustomerQAPlan. Harnesses emit actual
values, never verdicts. Command success cannot fill a missing scenario result.
"""
from dataclasses import asdict,dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from .customer_qa_plan import FrozenCustomerQAPlan
from .qa_admission import admit_qa_report
from .store import Conflict,canonical_json,digest
from .three_tier_runner import ReviewProfile,FixedReviewRunner,BubblewrapReviewEngine,ReviewUnavailable
from taesan_core.universal_qa import Scenario,Risk,Adapter,build_contract,run_scenarios

MAX_OBSERVATION_BYTES=128*1024
HARNESSES=Path(__file__).with_name('qa_harnesses')
FILES=('customer_node.mjs','customer_shop.py')
NODE_FLAGS=('--jitless','--disable-wasm-trap-handler','--max-old-space-size=128','--max-semi-space-size=8')


def _physical(path):
    root=Path(path)
    if not root.is_absolute() or '..' in root.parts or any(p.is_symlink() for p in (root,*root.parents)) or not root.is_dir():
        raise ValueError('absolute physical trusted runtime required')
    return root


def install_customer_harnesses(runtime):
    """Trusted-host preparation only; never installs or executes customer code."""
    root=_physical(runtime);target=root/'qa_harnesses'
    if target.is_symlink():raise ValueError('physical harness directory required')
    target.mkdir(mode=0o700,exist_ok=True)
    for filename in FILES:
        source=HARNESSES/filename
        if source.is_symlink() or not source.is_file():raise ValueError('trusted harness source required')
        content=source.read_bytes();out=target/filename
        if out.exists():
            if out.is_symlink() or not out.is_file() or out.stat().st_size!=len(content) or out.read_bytes()!=content:raise Conflict('registered trusted harness differs')
        else:
            with out.open('xb') as stream:stream.write(content)
            out.chmod(0o600)
    return {'harness_digest':digest({f:hashlib.sha256((target/f).read_bytes()).hexdigest() for f in FILES})}


def _verify_harnesses(runtime):
    root=_physical(runtime)
    for filename in FILES:
        target=root/'qa_harnesses'/filename
        expected=(HARNESSES/filename).read_bytes()
        if any(p.is_symlink() for p in (target,*target.parents)) or not target.is_file() or target.stat().st_size!=len(expected) or target.read_bytes()!=expected:
            raise Conflict('registered manager-owned business harness required')
    return root


def _parse_observations(path,project,allowed):
    try:return _parse_observations_file(path,project,allowed)
    except OSError as exc:raise ReviewUnavailable('business observation artifact unavailable') from exc


def _parse_observations_file(path,project,allowed):
    """Reject links, oversized artifacts, duplicate keys and verdict-only rows."""
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>MAX_OBSERVATION_BYTES:
        raise ReviewUnavailable('bounded regular business observation artifact required')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate observation key')
            result[key]=value
        return result
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        opened=os.fstat(stream.fileno())
        if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):raise ReviewUnavailable('business artifact changed')
        content=stream.read(MAX_OBSERVATION_BYTES+1)
    if len(content)>MAX_OBSERVATION_BYTES:raise ReviewUnavailable('business observation artifact too large')
    try:
        body=json.loads(content,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite observation')))
        if type(body) is not dict or set(body)!={'schema_version','project','cases'} or body['schema_version']!='customer-business-actuals/v1' or body['project']!=project or type(body['cases']) is not dict:
            raise ValueError('exact observation envelope required')
        cases=body['cases']
        if set(cases)-set(allowed)-{'__harness__'}:raise ValueError('unregistered business scenario')
        for key,row in cases.items():
            if type(row) is not dict:raise ValueError('typed business observation required')
            if row.get('status')=='observed' and key!='__harness__':
                if set(row)!={'status','actual','evidence'} or type(row['evidence']) is not dict or not row['evidence']:
                    raise ValueError('actual observation evidence required')
            elif row.get('status')=='unavailable':
                if not set(row)<= {'status','reason','error_type'} or type(row.get('reason')) is not str or not 0<len(row['reason'])<=200 or ('error_type' in row and (type(row['error_type']) is not str or len(row['error_type'])>100)):
                    raise ValueError('explicit unavailable reason required')
            else:raise ValueError('observer verdicts are not authoritative')
        return body,hashlib.sha256(content).hexdigest()
    except (ValueError,TypeError,UnicodeError,RecursionError) as exc:
        raise ReviewUnavailable('malformed business observation artifact') from exc


class _CaptureEngine:
    def __init__(self,delegate,project,allowed):
        self.delegate=delegate;self.project=project;self.allowed=allowed;self.observation=None
    def probe(self):return self.delegate.probe()
    def execute_snapshot(self,profile,work,outputs,checks,heartbeat):
        outcomes=self.delegate.execute_snapshot(profile,work,outputs,checks,heartbeat)
        if any(type(row) is not dict or row.get('exit_code')!=0 for row in outcomes):
            raise ReviewUnavailable('registered business harness did not complete')
        self.observation=_parse_observations(outputs/'customer-business.json',self.project,self.allowed)
        return outcomes


@dataclass(frozen=True)
class CustomerBusinessRun:
    payload_json:str
    @property
    def payload(self):return json.loads(self.payload_json)
    @property
    def report(self):return self.payload['report']
    @property
    def business_admitted(self):return self.payload['business_admitted']


class CustomerBusinessRunner:
    def __init__(self,plan,*,runtime,repository,engine=None,clock=time.time,timeout=180):
        if type(plan) is not FrozenCustomerQAPlan:raise ValueError('frozen manager-owned customer QA plan required')
        data=plan.payload
        if not data['mechanical_registration_ready'] or data['source_status']!='current':
            raise Conflict('current exact customer source plan required')
        root=_verify_harnesses(runtime)
        self.plan,self.clock=plan,clock
        self.harness_digest=digest({f:hashlib.sha256((root/'qa_harnesses'/f).read_bytes()).hexdigest() for f in FILES})
        if plan.project=='bellavita-shop':argv=('/usr/bin/python3','-I','-B','/runtime/qa_harnesses/customer_shop.py')
        elif plan.project in ('bellavita-erp','hope-sunrise','funeral-association'):
            argv=('/runtime/node',*NODE_FLAGS,'--experimental-strip-types','/runtime/qa_harnesses/customer_node.mjs',plan.project)
        else:raise ValueError('registered customer business harness required')
        self.profile=ReviewProfile(plan.project,repository,data['source_revision'],data['source_revision'],Path(plan.source_root),root,{'customer-business-oracles':argv},timeout=timeout)
        self.engine=engine if engine is not None else BubblewrapReviewEngine()

    def run(self,reviewer_subject,*,heartbeat):
        if type(reviewer_subject) is not str or not reviewer_subject.strip() or not callable(heartbeat):
            raise ValueError('registered independent reviewer and authorization heartbeat required')
        _verify_harnesses(self.profile.runtime)
        data=self.plan.payload
        rows=data['expected_contract']['scenarios']
        scenarios=[Scenario(row['id'],row['adapter'],row['group'],row['coverage_key'],Risk(**row['risk']),row['inputs'],row['expected'],row['oracle'],row['state_rationale'],row['formula']) for row in rows.values()]
        capture=_CaptureEngine(self.engine,self.plan.project,rows)
        fixed=FixedReviewRunner(self.profile,engine=capture,clock=self.clock)
        manifest=dict(project=self.plan.project,repository=self.profile.repository,base_sha=self.profile.base_sha,
                      description='Independent source-bound customer business observation',checks=['customer-business-oracles'],kind='test')
        task=dict(id=digest({'plan':self.plan.plan_digest,'reviewer':reviewer_subject})[:32],project=self.plan.project,
                  version=1,manifest_digest=digest(manifest),manifest=manifest)
        source=asdict(fixed.observe(task))
        identity=dict(plan_digest=self.plan.plan_digest,task_id=task['id'],source_revision=source['head_sha'],
                      artifact_digest=source['artifact_digest'],runtime_digest=source['runtime_digest'],reviewer_subject=reviewer_subject)
        candidate=dict(task=task,source=source,digest=digest(identity),identity=identity,
                       config_fingerprint=data['config_fingerprint'],developer_subject='source-snapshot-without-development')
        evidence=fixed(candidate,{'subject':reviewer_subject,'project':self.plan.project,'role':'reviewer'},heartbeat=heartbeat)
        if capture.observation is None:raise ReviewUnavailable('actual business observations missing')
        body,observation_digest=capture.observation
        pending=iter(scenarios)
        def observed(inputs):
            # universal_qa calls adapters once in frozen scenario order. Some
            # existing contracts share inputs, so dispatch by that owner order.
            scenario=next(pending)
            if scenario.inputs!=inputs:raise Conflict('frozen scenario execution order changed')
            row=body['cases'].get(scenario.id)
            if not row or row['status']!='observed':raise ReviewUnavailable('actual business entrypoint unavailable')
            return dict(actual=row['actual'],evidence=dict(row['evidence'],observation_digest=observation_digest,
                        plan_digest=self.plan.plan_digest,candidate_digest=candidate['digest'],reviewer_subject=reviewer_subject,
                        source_revision=source['head_sha'],runtime_digest=evidence.runtime_digest,harness_digest=self.harness_digest))
        adapters=[Adapter('customer-business-observation','1',observed)]
        if build_contract(scenarios,adapters)!=data['expected_contract']:raise Conflict('manager business oracle contract changed')
        report=run_scenarios(scenarios,adapters,source_revision=source['head_sha'],seed=self.plan.plan_digest,
                             required_coverage=list(data['required_risk_groups']))
        report['scope']='Isolated actual customer-source business entrypoints with synthetic inputs; no live service, customer UI or operational authorization'
        report['report_digest']=digest({k:v for k,v in report.items() if k!='report_digest'})
        admission=admit_qa_report(report,expected_source_revision=source['head_sha'],required_coverage=list(data['required_risk_groups']),expected_contract=data['expected_contract'])
        missing_formulas=set(data['required_formulas'])-{row['formula']['id'] for row in report['scenarios'] if row.get('formula') and row['status']=='pass'}
        if data['blockers'] or missing_formulas:
            admission['admitted']=False
            admission['reasons']+=['business_plan_incomplete'] if data['blockers'] else []
            admission['reasons']+=['required_business_formula_not_passed:'+key for key in sorted(missing_formulas)]
        result=dict(schema_version='customer-business-run/v1',project=self.plan.project,plan_digest=self.plan.plan_digest,
                    source_revision=source['head_sha'],reviewer_subject=reviewer_subject,candidate_digest=candidate['digest'],
                    runtime_digest=evidence.runtime_digest,harness_digest=self.harness_digest,observed_at=evidence.observed_at,
                    isolated_execution_verified=evidence.isolation_verified,scope='synthetic_only',report=report,admission=admission,
                    actual_case_count=sum(row['status']=='observed' for key,row in body['cases'].items() if key!='__harness__'),
                    unavailable_cases={key:body['cases'].get(key,{'reason':'actual_case_missing'}).get('reason','not_observed') for key in rows if body['cases'].get(key,{}).get('status')!='observed'},
                    unavailable_details={key:row for key,row in body['cases'].items() if row['status']=='unavailable'},
                    business_admitted=admission['admitted'],customer_ui_verified=False,operational_authorization=False)
        return CustomerBusinessRun(canonical_json(result))
