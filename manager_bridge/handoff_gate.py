"""Final customer release recommendation from host-verified evidence only."""
from copy import deepcopy
from dataclasses import dataclass
import re
from .store import Conflict,digest


@dataclass(frozen=True)
class VerifiedEvidence:
    """Host verifier output; never deserialize developer claims into this class."""
    plan_digest: str
    source_revision: str
    source_window_verified: bool
    adapter_version: str
    capabilities: frozenset
    results: tuple
    defects: tuple = ()
    extension_results: tuple = ()

    def __post_init__(self):
        if type(self.source_window_verified) is not bool or not isinstance(self.capabilities,frozenset) or not isinstance(self.results,tuple) or not isinstance(self.defects,tuple) or not isinstance(self.extension_results,tuple):raise ValueError("typed host verification receipt required")


def final_review(report,verified,*,trusted_plan):
    if not isinstance(verified,VerifiedEvidence):raise Conflict('registered host verification required; checklist PASS is not evidence')
    if verified.plan_digest!=report['plan_digest'] or not re.fullmatch('[a-f0-9]{40}',verified.source_revision) or not verified.adapter_version:raise Conflict('handoff source/plan/adapter mismatch')
    if digest(trusted_plan)!=verified.plan_digest:raise Conflict('trusted plan mismatch')
    authoritative={r['id']:r for r in trusted_plan['scenarios']}
    if len(report['scenarios'])!=len(authoritative) or {r['test_id'] for r in report['scenarios']}!=set(authoritative):raise Conflict('required scenarios missing or duplicated')
    for row in report['scenarios']:
        approved=authoritative[row['test_id']]
        if (row['priority'],row['group'],row['title'],row['expected'],row['risk'],row['preconditions'])!=(approved['risk']['priority'],approved['group'],approved['title'],approved['expected'],approved['risk'],approved['inputs']):raise Conflict('scenario contract altered')
    if report['required_evidence']!={r['id']:r['required'] for r in trusted_plan['scenarios']}:raise Conflict('required checks altered')
    outcome=deepcopy(report);index={r['id']:r for r in verified.results}
    if len(index)!=len(verified.results) or set(index)-{r['test_id'] for r in report['scenarios']}:raise Conflict('duplicate/unknown test evidence')
    for row in outcome['scenarios']:
        observation=index.get(row['test_id'])
        row.update(status='NOT RUN',actual=None,evidence=[],execution_steps=None,tested_at=None,performer=None,deployed_revision=None)
        if observation is None:continue
        if observation.get('source_revision')!=verified.source_revision:raise Conflict('stale observed source')
        if observation.get('plan_digest')!=verified.plan_digest:raise Conflict('observed contract changed')
        required=set(observation.get('required',[]))
        # Mandatory requirements come from the manager plan, not worker results.
        expected_required=set(outcome['required_evidence'][row['test_id']])
        if required!=expected_required:raise Conflict('required evidence altered')
        checks=observation.get('checks',{})
        if set(checks)-expected_required:raise Conflict('unregistered evidence kind')
        check_caps=authoritative[row['test_id']].get('check_capabilities',{})
        missing=[s for s in expected_required if check_caps.get(s,s) not in verified.capabilities or s not in checks or checks[s].get('status')!='pass' or checks[s].get('scope')!=check_caps.get(s,s) or not checks[s].get('reference')]
        fail=any(c.get('status')=='fail' and check_caps.get(scope,scope) in verified.capabilities and c.get('scope')==check_caps.get(scope,scope) and c.get('reference') for scope,c in checks.items())
        row.update(status='FAIL' if fail else 'BLOCKED' if missing or not verified.source_window_verified or observation.get('independent_expectation_verified') is not True else 'PASS',tested_at=observation.get('tested_at'),performer='독립 검증기',deployed_revision=verified.source_revision,execution_steps=observation.get('steps'),actual=observation.get('actual'),evidence=checks,note='미확보 증거: '+', '.join(sorted(missing)))
    defects=[dict(d) for d in verified.defects]
    if any(d.get('severity') not in ('S1','S2','S3','S4') or not d.get('status') for d in defects):raise Conflict('defect severity/state required')
    critical=[r for r in outcome['scenarios'] if r['priority'] in ('P0','P1')]
    counts={s:sum(r['status']==s for r in outcome['scenarios']) for s in ('PASS','FAIL','BLOCKED','NOT RUN')}
    residuals_ok=all(r['status']=='PASS' or all(index.get(r['test_id'],{}).get('approved_residual_plan',{}).get(k) for k in ('impact','mitigation','owner','approved_by')) for r in outcome['scenarios'] if r['priority'] in ('P2','P3'))
    closed_verified=all(d.get('fixed_revision')==verified.source_revision and d.get('retest_verified') is True for d in defects if d['status']=='CLOSED' and d['severity'] in ('S1','S2'))
    extension_index={r['id']:r for r in verified.extension_results}
    planned_extensions={r['id']:r for r in trusted_plan['extensions']}
    if len(extension_index)!=len(verified.extension_results) or set(extension_index)-set(planned_extensions):raise Conflict('unknown or duplicated QA extra checks')
    extra_ok=True
    outcome['qa_added_checks']=deepcopy(trusted_plan['extensions'])
    for row in outcome['qa_added_checks']:
        observed=extension_index.get(row['id']);needed=set(planned_extensions[row['id']]['required'])
        passed=bool(observed) and observed.get('status')=='pass' and observed.get('source_revision')==verified.source_revision and observed.get('plan_digest')==verified.plan_digest and needed<=verified.capabilities and all(observed.get('checks',{}).get(k,{}).get('status')=='pass' and observed['checks'][k].get('scope')==k and observed['checks'][k].get('reference') for k in needed)
        row['execution_status']='PASS' if passed and verified.source_window_verified else 'NOT RUN' if not observed else 'BLOCKED'
        extra_ok &= row['execution_status']=='PASS'
    ready=extra_ok and residuals_ok and closed_verified and bool(outcome['scenarios']) and all(r['status']=='PASS' for r in critical) and verified.source_window_verified is True and not any(d['severity'] in ('S1','S2') and d['status']!='CLOSED' for d in defects)
    outcome['defects']=defects;outcome['document_status']='VERIFIED_FOR_REVIEW' if ready else 'WITHHELD'
    outcome['summary'].update(counts=counts,executed=counts['PASS']+counts['FAIL']+sum(r['status']=='BLOCKED' and bool(r['execution_steps']) for r in outcome['scenarios']),customer_ready=ready,operational_authorization=False,customer_acceptance_approved=False,source_revision=verified.source_revision,recommendation='고객 검토용 인계 가능; 발주사 최종 승인 별도' if ready else '고객 인계 보류·개발사 보완/추가 검증 필요')
    outcome['approval_gates']=[dict(stage=stage,status='PASS' if ready else 'BLOCKED',reason='독립 증거·source·미해결 결함 조건 충족' if ready else '필수 실행/추가검사/결함/버전 증거 미충족') for stage in ('준비','실행','재시험','고객 인계')]
    if ready:outcome['policy_decisions_pending']=[]
    outcome['verification_binding']=dict(plan_digest=verified.plan_digest,source_revision=verified.source_revision,adapter_version=verified.adapter_version,source_window_verified=verified.source_window_verified)
    return outcome
