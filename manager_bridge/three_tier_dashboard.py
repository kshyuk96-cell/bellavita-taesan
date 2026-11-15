"""Read-only three-tier progress and owner-decision views.

The core supplies server-observed state after authorization. This projection keeps
only bounded identifiers, statuses and evidence summaries; it never renders task
prompts, provider bodies, source logs, credentials or executable browser content.
A technical acceptance is always distinct from an operational release approval.
"""
from html import escape
import re

from .maintenance_registry import Registry, name
from .store import Store, Conflict, canonical_json
from .agent_directory import _reject_known_tokens

STATES = {
    'development_queued':'개발 대기', 'developing':'개발 중',
    'candidate_ready':'검토 대기', 'reviewing':'검토 중',
    'review_passed':'독립 검토 통과', 'review_failed':'검토 실패',
    'rework_queued':'보완 대기', 'rework_developing':'보완 중',
    'needs_verification':'실행 대조 필요', 'accepted':'기술 인수 완료',
}
REASONS = {
    'capacity_missing':'작업 상한 설정 필요', 'capacity_not_configured':'작업 상한 설정 필요',
    'capacity_limit':'작업 상한 도달', 'review_backlog':'검토 대기 적체',
    'rework_limit':'태산이 원인 재분석 필요', 'rework_limit_reached':'태산이 원인 재분석 필요',
    'candidate_changed':'후보 변경으로 재검토 필요', 'source_changed':'소스 변경으로 재검토 필요',
    'configuration_changed':'설정 변경으로 재검토 필요', 'config_changed':'설정 변경으로 재검토 필요',
    'independent_check_failed':'독립 검증 실패·보완 필요',
    'review_lease_expired':'검토 만료·실행 대조 필요',
    'review_execution_uncertain':'검토 실행 결과 대조 필요',
    'review_expired':'검토 만료·실행 대조 필요', 'lease_expired':'작업 만료·실행 대조 필요',
    'missing_connection':'작업자 연결 필요', 'connection_unavailable':'작업자 연결 확인 필요',
    'authentication':'인증 연결 확인 필요', 'authorization_required':'별도 권한 결정 필요',
    'scope_change':'신규 범위 결정 필요', 'new_scope':'신규 범위 결정 필요',
    'usage_limit':'사용 한도 결정 필요', 'budget_limit':'추가 비용 결정 필요',
    'operational_approval_required':'운영 적용 승인 필요',
}
_REFS = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_SHA = re.compile(r'[a-f0-9]{40}')
_DIGEST = re.compile(r'[a-f0-9]{64}')


def _reference(value):
    return value if type(value) is str and _REFS.fullmatch(value) else None


def _hex(value, pattern):
    return value if type(value) is str and pattern.fullmatch(value) else None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _checks(values):
    if type(values) is not list or len(values)>100:return {'observed':False,'passed':0,'failed':0,'unmeasured':0}
    passed=failed=unmeasured=0
    for item in values:
        # Exit status is a server observation, not a model's boolean claim.
        code=item.get('exit_code') if type(item) is dict else None
        if type(code) is int:
            if code==0:passed+=1
            else:failed+=1
        else:unmeasured+=1
    return {'observed':bool(values),'passed':passed,'failed':failed,'unmeasured':unmeasured}


def _task(project,value):
    if type(value) is not dict or _reference(value.get('task_id')) is None:
        raise ValueError('bounded core task record required')
    state=value.get('state');state=state if type(state) is str and state in STATES else 'unknown'
    review=value.get('review_state');review=review if review in {'passed','failed','not_measured'} else 'not_measured'
    checks=_checks(value.get('checks',[]))
    source=value.get('source') if type(value.get('source')) is dict else {}
    # Contradictory or generic legacy reviewed status is never promoted.
    accepted=(state=='accepted' and value.get('technical_accepted') is True and
              review=='passed' and checks['observed'] and not checks['failed'] and not checks['unmeasured'] and
              _hex(source.get('head_sha'),_SHA) is not None and
              _hex(source.get('artifact_digest'),_DIGEST) is not None and
              _hex(value.get('candidate_digest'),_DIGEST) is not None)
    reason=value.get('blocked_reason')
    reason=reason if type(reason) is str and reason in REASONS else ('unknown' if reason is not None else None)
    return {'project':project,'task_id':value['task_id'],'state':state,
            'state_label':STATES.get(state,'확인 필요'),'version':_count(value.get('version')),
            'manifest_digest':_hex(value.get('manifest_digest'),_DIGEST),
            'candidate_digest':_hex(value.get('candidate_digest'),_DIGEST),
            'base_sha':_hex(source.get('base_sha'),_SHA),'head_sha':_hex(source.get('head_sha'),_SHA),
            'artifact_digest':_hex(source.get('artifact_digest'),_DIGEST),
            'developer_id':_reference(value.get('developer_id')),'reviewer_id':_reference(value.get('reviewer_id')),
            'rework_count':_count(value.get('rework_count')),'review_state':review,'checks':checks,
            'technical_accepted':accepted,'operational_approved':False,'deployment_verified':False,
            'connection_verified':value.get('connection_verified') is True,
            'requires_owner_decision':value.get('requires_owner_decision') is True,
            'blocked_reason':reason,'blocked_label':REASONS.get(reason,'태산이 확인 필요') if reason else None,
            'acceptance_needs_verification':state=='accepted' and not accepted}


def build_summary(core,token,projects):
    """Project exactly one freshly authorized core overview into a safe view.

    Every requested project is authorized before the callback and rechecked after
    it. A revoked token or changed config aborts the whole snapshot, rather than
    returning a partial cross-company report or silently dropping a project.
    """
    store=getattr(core,'store',None);registry=getattr(core,'registry',None)
    if not isinstance(store,Store) or not isinstance(registry,Registry) or registry.store is not store or not callable(getattr(core,'overview',None)):
        raise ValueError('registered shared hierarchy core required')
    if type(projects) is not list or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):
        raise ValueError('explicit unique bounded project list required')
    for project in projects:name(project)
    configs={project:registry.get(token,project) for project in projects}
    raw=core.overview(token,projects)
    if type(raw) is not dict or raw.get('operational_change') is not False or type(raw.get('projects')) is not list:
        raise ValueError('read-only core overview required')
    entries=raw['projects']
    if len(entries)!=len(projects) or any(type(entry) is not dict for entry in entries):raise ValueError('exact project snapshot required')
    keyed={entry.get('project'):entry for entry in entries}
    if len(keyed)!=len(projects) or set(keyed)!=set(projects):raise ValueError('project snapshot binding changed')
    result=[];decisions=[];attention=[];totals={state:0 for state in STATES};totals['unknown']=0
    for project in projects:
        entry=keyed[project]
        if entry.get('config_fingerprint')!=configs[project]['fingerprint']:raise Conflict('hierarchy configuration changed')
        rows=entry.get('tasks')
        if type(rows) is not list or len(rows)>10000:raise ValueError('bounded core task snapshot required')
        tasks=[_task(project,row) for row in rows[:200]]
        capacity=entry.get('capacity') if type(entry.get('capacity')) is dict else {}
        max_active=_count(capacity.get('max_active'));max_review=_count(capacity.get('max_review'))
        configured=(capacity.get('configured') is True and max_active is not None and max_active>0 and max_review is not None and max_review>0)
        members=entry.get('members')
        if type(members) is not list or len(members)>1000:raise ValueError('bounded member snapshot required')
        roles={role:0 for role in ('developer','reviewer','taesan')}
        connected=0
        for member in members:
            if type(member) is not dict:raise ValueError('typed member required')
            role=member.get('role')
            if role in roles:roles[role]+=1
            connected+=member.get('connection_verified') is True
        review_pending=sum(task['state'] in {'candidate_ready','reviewing'} for task in tasks)
        developing=sum(task['state'] in {'developing','rework_developing','needs_verification'} for task in tasks)
        backlog=bool(configured and review_pending>=max_review)
        project_view={'project':project,'name':configs[project]['config']['name'],
                      'company':configs[project]['config']['company'],'config_fingerprint':configs[project]['fingerprint'],
                      'capacity':{'configured':configured,'max_active':max_active if configured else None,
                                  'max_review':max_review if configured else None,'review_backlog_observed':backlog,
                                  'active_displayed':developing,'review_pending_displayed':review_pending},
                      'roles':roles,'registered_members':len(members),'connection_verified_members':connected,
                      'tasks':tasks,'displayed_tasks':len(tasks),'tasks_truncated':len(rows)>200 or entry.get('tasks_truncated') is True,
                      'operational_change':False,'operational_approved':False,'deployment_verified':False}
        result.append(project_view)
        for task in tasks:
            totals[task['state']]+=1
            if task['requires_owner_decision']:
                decisions.append({'project':project,'task_id':task['task_id'],'reason':task['blocked_reason'],
                                  'message':task['blocked_label'] or '별도 대표 결정 필요','head_sha':task['head_sha'],
                                  'scope':'decision_required_only','operational_approved':False})
            elif task['state'] in {'review_failed','needs_verification','unknown'} or task['acceptance_needs_verification']:
                attention.append({'project':project,'task_id':task['task_id'],'reason':task['blocked_reason'],
                                  'message':task['blocked_label'] or '태산이 검토·보완 필요'})
    # No model/CPU/billing metrics are invented from task count or elapsed time.
    response={'projects':result,'counts_displayed':totals,'owner_decisions':decisions,'manager_attention':attention,
              'owner_decision_count':len(decisions),'manager_attention_count':len(attention),
              'operational_change':False,'operational_approved':False,'deployment_verified':False,
              'existing_chat_control_verified':False,'background_management_verified':False,
              'usage_or_cost_measured':False,'snapshot_scope':'authorized_registered_hierarchy'}
    for project in projects:
        if registry.get(token,project)['fingerprint']!=configs[project]['fingerprint']:
            raise Conflict('project changed during dashboard collection')
    with store._transaction() as c:
        actor=store._authenticate(c,token);store._role(actor,('owner','manager'))
        for project in projects:store._project_access(c,actor,project)
        _reject_known_tokens(c,response)
    return response


def render_dashboard(summary):
    """Render escaped, script-free HTML; no action buttons imply approval rights."""
    if type(summary) is not dict or summary.get('snapshot_scope')!='authorized_registered_hierarchy' or summary.get('operational_change') is not False:
        raise ValueError('safe hierarchy summary required')
    projects=summary.get('projects')
    if type(projects) is not list or len(projects)>100:raise ValueError('bounded projects required')
    out=['<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
         '<title>태산이 개발 관리 현황</title><style>body{font:18px/1.65 system-ui,sans-serif;color:#17243b;background:#f7f9fc;margin:0;padding:24px}main{max-width:1100px;margin:auto}section{background:white;border:1px solid #d8e0ec;border-radius:12px;padding:20px;margin:20px 0}table{border-collapse:collapse;width:100%;font-size:16px}th,td{padding:12px;border-bottom:1px solid #e3e8f0;text-align:left;vertical-align:top;overflow-wrap:anywhere}code{font-size:14px}.table-scroll{overflow-x:auto}small{color:#46566e}h1,h2{line-height:1.35}a{color:#173f81}</style><main>',
         '<h1>태산이 개발 관리 현황</h1><p>개발 → 독립 검토 → 태산이 기술 인수의 현재 기록입니다. 기술 인수는 운영 배포 승인이 아닙니다.</p>',
         '<p>기존 별도 Codex 채팅 전체 제어·상시 감독·사용 비용은 이 화면에서 입증되지 않습니다.</p>',
         '<nav aria-label="현황 바로가기"><a href="#owner-decisions">대표 결정</a> · <a href="#projects">프로젝트 현황</a></nav>',
         '<section id="owner-decisions"><h2>대표 결정이 필요한 항목</h2>']
    decisions=summary.get('owner_decisions',[])
    if type(decisions) is not list or len(decisions)>20000:raise ValueError('bounded decisions required')
    if not decisions:out.append('<p>현재 전달된 기록에 대표 결정 요청이 없습니다.</p>')
    else:
        out.append('<ul>')
        for item in decisions:
            if type(item) is not dict:raise ValueError('typed decision required')
            out.append('<li>'+escape(str(item.get('project','')))+' / '+escape(str(item.get('task_id','')))+': '+escape(str(item.get('message','별도 결정 필요')))+' — 아직 승인되지 않았습니다.</li>')
        out.append('</ul>')
    out.append('</section><div id="projects">')
    for project in projects:
        if type(project) is not dict or type(project.get('tasks')) is not list or len(project['tasks'])>200:raise ValueError('safe bounded project required')
        title=escape(str(project.get('name',project.get('project',''))))
        out.append('<section><h2>'+title+'</h2>')
        capacity=project.get('capacity',{})
        out.append('<p>연결 확인 '+escape(str(project.get('connection_verified_members',0)))+' / 등록 '+escape(str(project.get('registered_members',0)))+'명. ')
        if capacity.get('configured') is not True:out.append('실행·검토 상한이 확인되지 않았습니다.')
        elif capacity.get('review_backlog_observed') is True:out.append('표시된 검토 대기가 상한에 도달했습니다.')
        else:out.append('실행·검토 상한이 설정돼 있습니다.')
        out.append('</p><div class="table-scroll"><table><caption>'+title+'의 표시된 작업 '+str(len(project['tasks']))+'건</caption><thead><tr><th scope="col">작업</th><th scope="col">상태</th><th scope="col">후보</th><th scope="col">검증</th></tr></thead><tbody>')
        for task in project['tasks']:
            if type(task) is not dict:raise ValueError('safe task required')
            check=task.get('checks',{})
            detail='통과 '+str(check.get('passed',0))+' / 실패 '+str(check.get('failed',0))+' / 미측정 '+str(check.get('unmeasured',0))
            status=str(task.get('state_label','확인 필요'))
            if task.get('acceptance_needs_verification') is True:status+=' · 인수 근거 재확인 필요'
            out.append('<tr><td>'+escape(str(task.get('task_id','')))+'<br><small>보완 '+escape(str(task.get('rework_count') or 0))+'회</small></td><td>'+escape(status)+'<br><small>'+escape(str(task.get('blocked_label') or ''))+'</small></td><td><code>'+escape(str(task.get('head_sha') or '후보 미확인'))+'</code></td><td>'+escape(detail)+'<br><small>운영 배포 미확인</small></td></tr>')
        if not project['tasks']:out.append('<tr><td colspan="4">현재 표시할 작업이 없습니다.</td></tr>')
        out.append('</tbody></table></div>')
        if project.get('tasks_truncated') is True:out.append('<p>일부 작업만 표시했습니다. 전체 건수로 해석하지 마십시오.</p>')
        out.append('</section>')
    out.append('</div></main></html>')
    return ''.join(out)
