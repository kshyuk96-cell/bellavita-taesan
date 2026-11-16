"""One scoped control surface over the existing authority and execution services.

This is an adapter, not a second scheduler, tester, or source of approval.
Trusted host composition fixes projects and instance identity before HTTP starts.
"""
import threading
from .continuous_manager import ContinuousManager
from .governance_portfolio import Portfolio
from .proposal_board import ProposalBoard
from .three_tier import ThreeTier
from .maintenance_registry import name
from .store import Conflict, PermissionDenied, NotFound


class ControlCenter:
    def __init__(self,core,controller,board,portfolio,projects,instance_id,*,onboarding=None,summary=None,stop_event=None,source_preflight=None):
        if not isinstance(core,ThreeTier) or not isinstance(controller,ContinuousManager) or controller.core is not core:
            raise ValueError('same trusted core and controller required')
        if not isinstance(board,ProposalBoard) or board.store is not core.store or not isinstance(portfolio,Portfolio) or portfolio.core is not core:
            raise ValueError('same registered board and portfolio required')
        self._validate_projects(projects);name(instance_id)
        if stop_event is not None and not isinstance(stop_event,threading.Event):raise ValueError('trusted stop event required')
        if source_preflight is not None and not callable(source_preflight):raise ValueError('trusted source preflight required')
        if onboarding is not None:
            from .control_onboarding import ControlOnboarding
            if not isinstance(onboarding,ControlOnboarding) or onboarding.store is not core.store:
                raise ValueError('same registered onboarding required')
        if summary is not None:
            from .control_digest import ControlDigest
            if not isinstance(summary,ControlDigest) or summary.portfolio is not portfolio:
                raise ValueError('same registered summary required')
        self.core,self.controller,self.store,self.registry=core,controller,core.store,core.registry
        self.board,self.portfolio,self.projects,self.instance_id=board,portfolio,tuple(projects),instance_id
        self.onboarding,self.summary=onboarding,summary
        self.stop_event=stop_event
        self.source_preflight=source_preflight

    @staticmethod
    def _validate_projects(projects):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):
            raise ValueError('explicit bounded unique projects required')
        for project in projects:name(project)

    def _scope(self,token,projects,*,roles=('owner','manager')):
        self._validate_projects(projects)
        if any(p not in self.projects for p in projects):raise NotFound('project outside registered control host')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,roles)
            return {p:self.registry._get(c,token,p,roles)['fingerprint'] for p in projects}

    def prepare_bundle(self,token,request_key,specs):
        if type(specs) is not list or not 1<=len(specs)<=20 or any(type(s) is not dict for s in specs):
            raise ValueError('one to twenty structured proposals required')
        if any(type(s.get('project')) is not str for s in specs):raise ValueError('exact proposal projects required')
        projects=list(dict.fromkeys(s.get('project') for s in specs))
        self._scope(token,projects)
        return self.board.propose_bundle(token,request_key,specs)

    def _bundle_projects(self,token,batch_id):
        name(batch_id)
        with self.store._transaction() as c:
            self.board._actor(c,token)
            row=c.execute('SELECT snapshot FROM manager_proposal_batches WHERE id=?',(batch_id,)).fetchone()
            if row is None:raise NotFound('batch unavailable')
            import json
            projects=[]
            for item in json.loads(row['snapshot']):
                proposal=self.board._proposal(c,token,item['id'])
                if proposal['project'] not in self.projects:raise NotFound('batch outside registered control host')
                if proposal['project'] not in projects:projects.append(proposal['project'])
        return projects

    def get_bundle(self,token,batch_id):
        self._scope(token,self._bundle_projects(token,batch_id))
        return self.board.get_batch(token,batch_id)

    def approve_bundle(self,token,batch_id,batch_digest):
        # No new implicit delegation: existing owner decision remains authoritative.
        self._scope(token,self._bundle_projects(token,batch_id),roles=('owner',))
        return self.board.approve_batch(token,batch_id,batch_digest)

    def step(self,token,projects,*,limit=1):
        if type(limit) is not int or not 1<=limit<=20:raise ValueError('bounded control step required')
        before=self._scope(token,projects,roles=('manager',))
        if self.stop_event is not None and self.stop_event.is_set():
            return {'state':'stopped','actions':[],'execution_started':False}
        if self.source_preflight is None:
            return {'state':'source_preflight_not_configured','actions':[],'execution_started':False}
        ready=self.source_preflight(token,projects)
        if self._scope(token,projects,roles=('manager',))!=before:raise Conflict('control source preflight scope changed')
        if ready is not True:
            return {'state':'source_observation_blocked','actions':[],'execution_started':False}
        return self.controller.tick(token,projects,self.instance_id,limit=limit,stop_event=self.stop_event)

    def task(self,token,task_id):
        # Scope before get_task's lease-expiry maintenance: an out-of-host read
        # must not mutate another project's tasks even for a broader actor.
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            row=self.store._get(c,actor,task_id)
            if row['project'] not in self.projects:raise NotFound('task outside registered control host')
        task=self.store.get_task(token,task_id)
        self._scope(token,[task['project']])
        try:return self.core.get(token,task_id)
        except NotFound:
            self._scope(token,[task['project']])
            return {'task_id':task['id'],'project':task['project'],'state':task['state'],
                    'manifest_digest':task['manifest_digest'],'technical_accepted':False,
                    'hierarchy_registered':False}

    def report(self,token,projects):
        before=self._scope(token,projects)
        result=self.summary.report(token,projects) if self.summary is not None else self.portfolio.report(token,projects)
        if self._scope(token,projects)!=before:raise Conflict('control scope changed during report')
        return result

    def inventory(self,token,projects):
        self._scope(token,projects)
        if self.onboarding is None:raise NotFound('control onboarding not configured')
        return self.onboarding.inventory(token,projects)

    def plan_connections(self,token,request_key,projects,expected_bindings):
        self._scope(token,projects)
        if self.onboarding is None:raise NotFound('control onboarding not configured')
        return self.onboarding.plan(token,request_key,projects,expected_bindings)

    def get_connection_plan(self,token,plan_id):
        if self.onboarding is None:raise NotFound('control onboarding not configured')
        result=self.onboarding.get_plan(token,plan_id)
        self._scope(token,[item['project'] for item in result['plan']['projects']])
        return result
