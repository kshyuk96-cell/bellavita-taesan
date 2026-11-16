"""Bounded, scoped portfolio evidence; diagnostics never grant execution authority."""
import math
import json
from .store import Conflict,digest
from .maintenance_registry import name
from .three_tier import ThreeTier
from .connection_readiness import Connections
from .proposal_board import ProposalBoard

GOOD_SOURCE={'baseline','unchanged','changed'}

def _fresh(now,observed,age=300):
    return type(observed) in (int,float) and math.isfinite(observed) and 0<=now-observed<=age


class Portfolio:
    def __init__(self,core,*,connections=None,board=None,routing=None,health=None):
        if not isinstance(core,ThreeTier):raise ValueError('registered hierarchy required')
        for service,kind in ((connections,Connections),(board,ProposalBoard)):
            if service is not None and (not isinstance(service,kind) or service.store is not core.store):
                raise ValueError('same registered store required')
        if health is not None and not callable(health):raise ValueError('trusted measured health callback required')
        if routing is not None:
            from .governance_routing import GovernanceRouting
            if not isinstance(routing,GovernanceRouting) or routing.store is not core.store:
                raise ValueError('same registered routing service required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.connections,self.board,self.routing,self.health=connections,board,routing,health

    def _scope(self,token,projects):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):
            raise ValueError('explicit bounded projects required')
        for project in projects:name(project)
        with self.store._transaction() as c:
            return {p:self.registry._get(c,token,p) for p in projects}

    def _source(self,c,project,config,now):
        tables={r['name'] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        row=c.execute('SELECT * FROM manager_source_heads WHERE project=?',(project,)).fetchone() if 'manager_source_heads' in tables else None
        status=c.execute('SELECT * FROM manager_source_observation_status WHERE project=?',(project,)).fetchone() if 'manager_source_observation_status' in tables else None
        if not row:return {'state':'not_checked','fresh':False}
        fresh=row['config_fingerprint']==config['fingerprint'] and _fresh(now,row['observed_at'])
        state='observed' if fresh else 'stale'
        if status:
            fresh=fresh and status['config_fingerprint']==config['fingerprint'] and status['state'] in GOOD_SOURCE and _fresh(now,status['checked_at'])
            state=status['state'] if status['state'] in GOOD_SOURCE|{'not_configured','unavailable','permission_denied','unknown','stale'} else 'unknown'
            if not fresh and state in GOOD_SOURCE:state='stale'
        return {'state':state,'fresh':fresh}

    def _connections(self,token,project):
        required=('source_snapshot','receiver','sandbox','host','model')
        if self.connections is None:return {'state':'not_configured','ready':0,'total':len(required)}
        value=self.connections.latest(token,project,max_age=300)
        states={row['capability']:row['state'] for row in value.get('facts',[]) if row.get('capability') in required}
        fresh=not value.get('stale',True) and _fresh(self.store._now(),value.get('checked_at'))
        ready=sum(states.get(cap)=='verified' for cap in required) if fresh else 0
        return {'state':'ready' if fresh and ready==len(required) else ('stale' if states and not fresh else 'blocked'),
                'ready':ready,'total':len(required),
                'capabilities':{cap:states.get(cap,'not_configured') for cap in required}}

    def _health(self,token,projects):
        unavailable={'state':'unavailable','http_state':'unknown','manager_state':'unknown','fresh':False}
        if self.health is None:return unavailable
        try:value=self.health(token,tuple(projects))
        except Exception:return unavailable
        if type(value) is not dict:return unavailable
        state=value.get('state');http=value.get('http_state');manager=value.get('manager_state')
        if state not in ('running','stopping','unavailable') or http not in ('listening','stopped','unknown') or manager not in ('running','idle','stopped','unknown') or not _fresh(self.store._now(),value.get('observed_at'),60):return unavailable
        return {'state':state,'http_state':http,'manager_state':manager,'fresh':True,'_observed_at':value['observed_at']}

    def report(self,token,projects):
        configs=self._scope(token,projects)
        health=self._health(token,projects)
        # Only health and hierarchy freshness invoke external host callbacks.
        overview=self.core.overview(token,projects,limit=20,freshness_limit=3)
        # These typed services read durable state without invoking providers.
        connections={p:self._connections(token,p) for p in projects}
        route=None
        if self.routing is not None:
            try:route=self.routing.plan(token,projects)
            except Exception:route=None
        routes={}
        if type(route) is dict and type(route.get('projects')) is list:
            for item in route['projects'][:100]:
                if type(item) is not dict or item.get('project') not in projects:continue
                roles=item.get('eligible_roles',{})
                routes[item['project']]={'state':'ready' if item.get('ready') is True else 'blocked',
                    'eligible_role_counts':{role:len(roles.get(role,[])) if type(roles.get(role)) is list else 0 for role in ('taesan','developer','reviewer')}}
        batches={};seen=set();batch_partial=False;batch_counts={}
        if self.board is not None:
            for p in projects:
                listed=self.board.list_batches(token,p,limit=20)
                with self.store._transaction() as c:
                    self.registry._get(c,token,p)
                    count=c.execute("""SELECT COUNT(*) FROM manager_proposal_batches b WHERE EXISTS
                        (SELECT 1 FROM json_each(b.snapshot) item JOIN manager_proposals proposal
                         ON proposal.id=json_extract(item.value,'$.id') WHERE proposal.project=?)""",(p,)).fetchone()[0]
                batch_counts[p]=count
                if count>20 or len(listed)<min(count,20):batch_partial=True
                for batch in listed:
                    if batch['id'] not in seen:
                        seen.add(batch['id']);batches[batch['id']]={
                            'id':batch['id'],'digest':batch['digest'],'state':batch['state'],
                            'required_decision':batch['required_decision'],'proposal_count':len(batch['items'])}
        current=self._scope(token,projects)
        if any(current[p]['fingerprint']!=configs[p]['fingerprint'] for p in projects):raise Conflict('portfolio configuration changed')
        items=[];by_project={p['project']:p for p in overview['projects']}
        with self.store._transaction() as c:
            # Recheck after every host/provider callback before returning evidence.
            for p in projects:
                if self.registry._get(c,token,p)['fingerprint']!=configs[p]['fingerprint']:
                    raise Conflict('portfolio configuration changed')
            now=self.store._now()
            observed=health.pop('_observed_at',None)
            if health['fresh'] and not _fresh(now,observed,60):
                health={'state':'unavailable','http_state':'unknown','manager_state':'unknown','fresh':False}
            for p in projects:
                row=by_project[p];source=self._source(c,p,current[p],now)
                policy=c.execute('SELECT * FROM governance_qa_policies WHERE project=?',(p,)).fetchone()
                if policy:
                    try:
                        content=json.loads(policy['policy'])
                        if content['config_fingerprint']!=current[p]['fingerprint'] or digest(content)!=policy['policy_digest']:policy=None
                    except (ValueError,KeyError,TypeError):policy=None
                qa={'policy':'registered' if policy else 'missing','provider':'registered' if p in self.core.governance_gate.providers else 'missing'}
                roles=row['role_counts'];ownership=all(roles.get(role,0)>0 for role in ('taesan','developer','reviewer'))
                tasks=[{'id':t['task_id'] if 'task_id' in t else t.get('id'), 'state':t['state'],
                        'source_freshness':t.get('source_freshness','not_checked'),
                        'technical_accepted':t.get('technical_accepted',False) is True} for t in row['tasks']]
                progress=[
                    ('registration','registered'),('source','verified' if source['fresh'] else 'blocked'),
                    ('connections','registered' if connections[p]['state']=='ready' else 'blocked'),
                    ('ownership','registered' if ownership else 'blocked'),
                    ('capacity','registered' if row['capacity']['configured'] else 'blocked'),
                    ('qa_policy','registered' if policy else 'blocked'),
                    ('qa_provider','registered' if qa['provider']=='registered' else 'blocked'),
                    ('proposals','registered' if self.board is not None else 'blocked'),
                    ('routing','registered' if routes.get(p,{}).get('state')=='ready' else 'blocked'),
                    ('service','verified' if health['fresh'] and health['state']=='running' and health['http_state']=='listening' else 'blocked')]
                items.append({'project':p,'registry_revision':current[p]['revision'],'source':source,
                    'connections':connections[p],'qa':qa,'role_counts':{r:roles.get(r,0) for r in ('taesan','developer','reviewer')},
                    'capacity':row['capacity'],'routing':routes.get(p,{'state':'not_configured','eligible_role_counts':{}}),'tasks':tasks,'total_tasks':row['total_tasks'],
                    'tasks_partial':row['tasks_truncated'],'progress':[{'item':k,'state':v} for k,v in progress]})
        return {'projects':items,'batches':sorted(batches.values(),key=lambda b:b['id']),
                'batch_window':{'limit_per_project':20,'partial':batch_partial,'candidate_counts':batch_counts,'configured':self.board is not None},
                'task_window':{'limit':20,'freshness_checks':overview['freshness_checks'],'partial':overview['truncated'],
                               'next_cursor':overview['next_cursor']},
                'service_health':health,'routing_state':'observed' if routes else 'unavailable',
                'operational_authorization':False,'always_on_verified':False,'account_wide_control_verified':False}

    def decision_packet(self,token,projects):
        report=self.report(token,projects)
        decisions=[{'kind':'business_batch','batch_id':b['id'],'digest':b['digest'],'proposal_count':b['proposal_count'],
                    'action':'owner_business_approval','execution_authorized':False,
                    'cost_estimate_state':'not_measured','additional_spend_authorized':False}
                   for b in report['batches'] if b['state']=='pending_owner_approval' and b['required_decision']=='owner_business_approval']
        decisions.extend({'kind':'qa_policy_setup','project':p['project'],'action':'owner_register_qa_policy','execution_authorized':False}
                         for p in report['projects'] if p['qa']['policy']=='missing')
        decisions.sort(key=lambda x:(x['kind'],x.get('project',''),x.get('batch_id','')))
        return {'projects':list(projects),'owner_required':decisions,
                'batch_window':report['batch_window'],'task_window':report['task_window'],
                'summary_complete':report['batch_window']['configured'] and not report['batch_window']['partial'] and not report['task_window']['partial'],
                'diagnostic_count':sum(row['state']=='blocked' for p in report['projects'] for row in p['progress']),
                'operational_authorization':False,'technical_acceptance':False}


def render_dashboard(report):
    """Render a bounded snapshot; escaped text only, without executable page data."""
    from html import escape
    labels={'registration':'프로젝트 등록','source':'소스 관찰','connections':'개발 연결',
            'ownership':'역할 등록','capacity':'실행 한도','qa_policy':'QA 정책',
            'qa_provider':'QA 제공자','proposals':'제안 보드','routing':'업무 배정','service':'서비스 관찰'}
    states={'registered':'등록됨','blocked':'차단됨','verified':'관찰 확인됨'}
    e=lambda value:escape(str(value),quote=True)
    parts=['<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>태산 중앙 거버넌스</title><style>body{font:16px/1.6 system-ui,sans-serif;max-width:1100px;margin:32px auto;padding:0 20px;color:#173049;background:#f6f8fb}section{background:white;padding:20px;margin:20px 0;border-radius:12px}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px;border-bottom:1px solid #ddd}code{overflow-wrap:anywhere}.blocked{color:#9c3e15}</style>',
           '<h1>중앙 거버넌스 현황</h1><p>등록과 관찰 결과를 구분합니다. 이 화면은 업무 실행·배포 승인이나 상시 가동 보증을 제공하지 않습니다.</p>']
    for project in report.get('projects',[])[:100]:
        parts.append('<section><h2>'+e(project.get('project',''))+'</h2><table><thead><tr><th>점검 항목</th><th>상태</th></tr></thead><tbody>')
        for row in project.get('progress',[])[:10]:
            state=row.get('state','blocked');label=states.get(state,'차단됨')
            parts.append('<tr><td>'+e(labels.get(row.get('item'),'점검'))+'</td><td class="'+('blocked' if state=='blocked' else 'observed')+'">'+e(label)+'</td></tr>')
        parts.append('</tbody></table><p>기록된 업무 '+e(project.get('total_tasks',0))+'건 · 표시 '+e(len(project.get('tasks',[])))+'건'+(' · 일부만 표시' if project.get('tasks_partial') else '')+'</p></section>')
    pending=[b for b in report.get('batches',[]) if b.get('state')=='pending_owner_approval' and b.get('required_decision')=='owner_business_approval']
    parts.append('<section><h2>소유자 업무 승인 대기</h2><p>정확한 배치와 digest를 검토한 뒤 인증된 승인 API를 사용합니다. 진단 차단 항목은 자동 승인 대상이 아닙니다.</p>')
    if report.get('batch_window',{}).get('partial'):
        parts.append('<p class="blocked">승인 목록은 프로젝트별 최신 20건만 표시하거나 일부 권한 범위를 생략했습니다. 오래된 승인 대기 배치가 더 있을 수 있습니다.</p>')
    if not pending:parts.append('<p>현재 표시 범위에 승인 대기 배치가 없습니다.</p>')
    for batch in pending[:100]:
        parts.append('<p>배치 <code>'+e(batch.get('id',''))+'</code> · 제안 '+e(batch.get('proposal_count',0))+'건<br>digest <code>'+e(batch.get('digest',''))+'</code></p>')
    parts.append('</section><p>업무 목록은 전역 20건, 최신성 재검사는 최대 3건입니다. 미확인 항목을 완료로 계산하지 않습니다.</p></html>')
    return ''.join(parts)
