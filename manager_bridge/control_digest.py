"""One scoped user summary from bounded governance reports; never sends messages."""
from .store import Conflict
from .governance_portfolio import Portfolio
from .governance_routing import GovernanceRouting
from .governance_supervision import GovernanceSupervision


class ControlDigest:
    def __init__(self,portfolio,*,routing=None,supervision=None):
        if not isinstance(portfolio,Portfolio):raise ValueError('registered portfolio required')
        if routing is not None and (not isinstance(routing,GovernanceRouting) or routing.store is not portfolio.store or routing.core is not portfolio.core):
            raise ValueError('same registered routing core and store required')
        if supervision is not None and (not isinstance(supervision,GovernanceSupervision) or supervision.store is not portfolio.store or supervision.host.core is not portfolio.core):
            raise ValueError('same registered supervision core and store required')
        self.portfolio,self.routing,self.supervision=portfolio,routing,supervision
        self.core,self.store,self.registry=portfolio.core,portfolio.store,portfolio.registry

    def _scope(self,token,projects):
        values=self.portfolio._scope(token,projects)
        return {p:row['fingerprint'] for p,row in values.items()}

    def _recheck(self,token,projects,bindings):
        if self._scope(token,projects)!=bindings:raise Conflict('control digest configuration changed')

    @staticmethod
    def _problem(project,item):
        key=item['item']
        if key=='source':
            return 'unknown' if project['source']['state'] in ('not_checked','not_configured','unknown','stale') else 'blocked'
        if key=='connections':
            facts=project['connections'].get('capabilities',{})
            return 'blocked' if any(state in ('permission_denied','unavailable') for state in facts.values()) else 'unknown'
        if key in ('qa_provider','service','routing'):
            if key=='routing' and project['routing']['state']=='blocked':return 'blocked'
            return 'unknown'
        return 'blocked'

    def report(self,token,projects,*,decision_limit=20):
        if type(decision_limit) is not int or not 1<=decision_limit<=100:raise ValueError('bounded owner decision window required')
        bindings=self._scope(token,projects)
        # Exactly one Portfolio call: decision_packet would repeat freshness callbacks.
        portfolio=self.portfolio.report(token,projects)
        self._recheck(token,projects,bindings)
        routing=None;supervision=None
        if self.routing is not None:
            routing=self.routing.plan(token,projects)
            self._recheck(token,projects,bindings)
        if self.supervision is not None:
            supervision=self.supervision.report(token,projects)
            self._recheck(token,projects,bindings)
        counters={'completed_current':0,'completion_unchecked':0,'blocked':0,'unknown':0,'registered':0,'verified':0}
        project_summaries=[]
        for project in portfolio['projects']:
            completed=sum(task['state']=='accepted' and task['technical_accepted'] is True for task in project['tasks'])
            unchecked=sum(task['state']=='accepted' and task['technical_accepted'] is not True for task in project['tasks'])
            counts={'blocked':0,'unknown':0,'registered':0,'verified':0}
            problems=[]
            for item in project['progress']:
                state=self._problem(project,item) if item['state']=='blocked' else item['state']
                if state not in counts:state='unknown'
                counts[state]+=1
                if state in ('blocked','unknown'):
                    problem={'item':item['item'],'state':state}
                    if item['item']=='qa_provider':problem['reason']='external_qa_integration_unavailable'
                    problems.append(problem)
            counters['completed_current']+=completed;counters['completion_unchecked']+=unchecked
            for state,amount in counts.items():counters[state]+=amount
            project_summaries.append({'project':project['project'],'completed_current':completed,'completion_unchecked':unchecked,
                'displayed_tasks':len(project['tasks']),'recorded_tasks':project['total_tasks'],'tasks_partial':project['tasks_partial'],
                'checks':counts,'problems':problems})
        decisions=[{'kind':'business_batch','batch_id':batch['id'],'digest':batch['digest'],'proposal_count':batch['proposal_count'],
                    'action':'owner_business_approval','cost_estimate_state':'not_measured','additional_spend_authorized':False,
                    'execution_authorized':False}
                   for batch in portfolio['batches'] if batch['state']=='pending_owner_approval' and batch['required_decision']=='owner_business_approval']
        decisions.extend({'kind':'qa_policy_setup','project':project['project'],'action':'owner_register_qa_policy','execution_authorized':False}
                         for project in portfolio['projects'] if project['qa']['policy']=='missing')
        decisions.sort(key=lambda item:(item['kind'],item.get('project',''),item.get('batch_id','')))
        decision_partial=len(decisions)>decision_limit or portfolio['batch_window']['partial']
        unknown_supervision=0;blocked_supervision=0
        if supervision is not None:
            unknown_supervision=sum(job['stale'] or job['state'] in ('running','uncertain','unavailable') for job in supervision['jobs'])
            blocked_supervision=sum(job['state']=='retention_required' for job in supervision['jobs'])
        recovery={'state':'not_configured','reconciliation_required':None,'partial':False}
        if routing is not None:
            value=routing['recovery']
            recovery={'state':value['state'],'reconciliation_required':value.get('reconciliation_required'),
                      'partial':value.get('truncated',False)}
        complete=not portfolio['task_window']['partial'] and not decision_partial and portfolio['batch_window']['configured'] and not recovery['partial']
        result={'projects':list(projects),'summary':{'completed_current':counters['completed_current'],
                'completion_unchecked':counters['completion_unchecked'],'blocked_checks':counters['blocked'],
                'unknown_checks':counters['unknown'],'owner_decisions_in_collected_window':len(decisions),
                'supervision_state':'not_configured' if supervision is None else 'observed',
                'supervision_unknown_jobs':unknown_supervision if supervision is not None else None,'supervision_blocked_jobs':blocked_supervision if supervision is not None else None},
                'project_summaries':project_summaries,'owner_decisions':decisions[:decision_limit],
                'task_window':{**portfolio['task_window'],'pagination':'core_overview_cursor','continuation_route':'/v1/representative/runtime/three-tier/overview'},'batch_window':portfolio['batch_window'],
                'decision_window':{'limit':decision_limit,'displayed':min(len(decisions),decision_limit),
                    'total_in_collected_window':len(decisions),'partial':decision_partial,
                    'pagination':'bounded_portfolio_batch_window','next_cursor':None},
                'recovery':recovery,'summary_scope':'bounded_current_observations','summary_complete':complete,'per_task_messages_sent':0,'messages_sent':False,
                'operational_authorization':False,'completion_percentage':None,'always_on_verified':False}
        with self.store._transaction() as c:
            for project in projects:
                if self.registry._get(c,token,project)['fingerprint']!=bindings[project]:raise Conflict('control digest configuration changed')
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,result)
        return result
