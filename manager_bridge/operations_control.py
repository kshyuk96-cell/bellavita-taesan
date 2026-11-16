"""Registered operations adapters; host bindings are never selected by clients."""
from .store import NotFound
from .control_center import ControlCenter


class OperationsControl:
    def __init__(self,control,preflight,reporting,budget,*,worker_links=None,recovery=None,readiness=None,transition=None,link_plan=None,backup_health=None,
                 baselines=None,quality_report=None,quality_handoff=None,workflow_trace=None):
        from .project_connection_preflight import ProjectConnectionPreflight
        from .operations_reporting import OperationsReporting
        from .operating_budget import OperatingBudget
        if not isinstance(control,ControlCenter):raise ValueError('registered central control required')
        if not isinstance(preflight,ProjectConnectionPreflight) or preflight.store is not control.store:
            raise ValueError('same registered project preflight required')
        if not isinstance(reporting,OperationsReporting) or reporting.core is not control.core:
            raise ValueError('same registered operations reporting required')
        if not isinstance(budget,OperatingBudget) or budget.core is not control.core:
            raise ValueError('same registered admission budget required')
        self.control,self.preflight,self.reporting,self.budget=control,preflight,reporting,budget
        self.store=control.store
        if worker_links is not None:
            from .worker_links import NativeWorkerLinks
            if not isinstance(worker_links,NativeWorkerLinks) or worker_links.core is not control.core:
                raise ValueError('same registered native worker links required')
        self.worker_links=worker_links
        from .recovery_readiness import RecoveryReadiness
        from .portfolio_readiness import PortfolioReadiness
        for value,kind,expected in ((recovery,'recovery',RecoveryReadiness),(readiness,'readiness',PortfolioReadiness)):
            if value is not None and (not isinstance(value,expected) or value.core is not control.core):
                raise ValueError('same registered '+kind+' required')
        from .runtime_transition import RuntimeTransition
        from .project_link_plan import ProjectLinkPlan
        from .backup_health import BackupHealth
        for value,expected in ((transition,RuntimeTransition),(link_plan,ProjectLinkPlan),(backup_health,BackupHealth)):
            if value is not None and (not isinstance(value,expected) or value.core is not control.core):
                raise ValueError('same registered transition operations required')
        from .business_baselines import BusinessBaselines
        from .quality_delivery_report import QualityDeliveryReport
        from .worker_quality_handoff import WorkerQualityHandoff
        for value,expected in ((baselines,BusinessBaselines),(quality_report,QualityDeliveryReport),(quality_handoff,WorkerQualityHandoff)):
            if value is not None and (not isinstance(value,expected) or value.core is not control.core):raise ValueError('same registered quality service required')
        self.baselines,self.quality_report,self.quality_handoff=baselines,quality_report,quality_handoff
        from .workflow_trace import WorkflowTrace
        if workflow_trace is not None and (not isinstance(workflow_trace,WorkflowTrace) or workflow_trace.core is not control.core):
            raise ValueError('same registered workflow trace required')
        self.workflow_trace=workflow_trace
        self.transition,self.link_plan,self.backup_health=transition,link_plan,backup_health
        self.recovery,self.readiness=recovery,readiness

    def inspect(self,token,projects):
        self.control._scope(token,projects)
        return self.preflight.inspect(token,projects)

    def workers(self,token,projects):
        self.control._scope(token,projects)
        if self.worker_links is None:raise NotFound('native worker links not configured')
        return self.worker_links.inspect(token,projects)

    def owner_packet(self,token,request_key,projects,*,expected_fingerprints=None):
        self.control._scope(token,projects)
        return self.preflight.owner_packet(token,request_key,projects,expected_fingerprints=expected_fingerprints)

    def report(self,token,projects,request_key,*,after=None,limit=20):
        self.control._scope(token,projects)
        return self.reporting.report(token,projects,request_key,after=after,limit=limit)

    def batches(self,token,projects,*,cursor=None,limit=20):
        self.control._scope(token,projects)
        return self.control.board.page_batches(token,projects,cursor=cursor,limit=limit)

    def budget_report(self,token,projects):
        self.control._scope(token,projects)
        return self.budget.report(token,projects)

    def install_budget(self,token,project,policy,*,expected_revision=0):
        from .operating_budget import BudgetPolicy
        self.control._scope(token,[project],roles=('owner',))
        if type(policy) is not dict:raise ValueError('exact admission policy required')
        try:value=BudgetPolicy(**policy)
        except TypeError:raise ValueError('exact admission policy fields required') from None
        return self.budget.install(token,project,value,expected_revision=expected_revision)

    def refresh_auth(self,token,project):
        self.control._scope(token,[project])
        return self.budget.refresh_auth(token,project)

    def reconcile_budget(self,token,project,reservation_id):
        self.control._scope(token,[project])
        with self.store._transaction() as c:
            row=self.budget._get(c,token,reservation_id)
            if row['project']!=project:raise NotFound('reservation outside selected project')
        return self.budget.reconcile(token,reservation_id)

    def register_artifact(self,token,project,kind,artifact_key,*,retention_seconds=86400):
        self.control._scope(token,[project])
        return self.reporting.register_artifact(token,project,kind,artifact_key,retention_seconds=retention_seconds)

    def artifact(self,token,reference):
        # The stored reference resolves scope without reading a host path.
        value=self.reporting.get_artifact(token,reference)
        self.control._scope(token,[value['project']])
        return value

    def host_report(self,token,projects):
        self.control._scope(token,projects)
        gate=self.budget.host_admission
        if gate is None:raise NotFound('local host admission not configured')
        return {'projects':[gate.check(token,p) for p in projects],'always_on_verified':False}

    def install_host(self,token,project,requirement,*,expected_revision=0):
        from .host_admission import HostRequirement
        self.control._scope(token,[project],roles=('owner',))
        gate=self.budget.host_admission
        if gate is None:raise NotFound('local host admission not configured')
        if type(requirement) is not dict:raise ValueError('exact host requirement required')
        try:value=HostRequirement(**requirement)
        except TypeError:raise ValueError('exact host requirement fields required') from None
        return gate.install(token,project,value,expected_revision=expected_revision)

    def refresh_host(self,token,project):
        self.control._scope(token,[project])
        gate=self.budget.host_admission
        if gate is None:raise NotFound('local host admission not configured')
        return gate.refresh(token,project)

    def recovery_report(self,token,projects,*,limit=20):
        self.control._scope(token,projects)
        if self.recovery is None:raise NotFound('recovery inspection not configured')
        return self.recovery.inspect(token,projects,limit=limit)

    def readiness_report(self,token,projects):
        self.control._scope(token,projects)
        if self.readiness is None:raise NotFound('portfolio readiness not configured')
        return self.readiness.assess(token,projects)

    def exception_report(self,token,projects,request_key,*,after=None,limit=20):
        self.control._scope(token,projects)
        if self.readiness is None:raise NotFound('portfolio readiness not configured')
        return self.readiness.exceptions(token,projects,request_key,after=after,limit=limit)

    def register_projects(self,token,request_key,registrations):
        if self.readiness is None:raise NotFound('portfolio readiness not configured')
        return self.readiness.register_batch(token,request_key,registrations)

    def transition_status(self,token,projects):
        self.control._scope(token,projects)
        if self.transition is None:raise NotFound('runtime transition not configured')
        return self.transition.status(token,projects)

    def drain(self,token,projects,expected_revisions,request_key,*,reason='planned_update'):
        self.control._scope(token,projects,roles=('owner',))
        if self.transition is None:raise NotFound('runtime transition not configured')
        return self.transition.drain(token,projects,expected_revisions,request_key,reason=reason)

    def resume(self,token,projects,expected_revisions,request_key):
        self.control._scope(token,projects,roles=('owner',))
        if self.transition is None:raise NotFound('runtime transition not configured')
        return self.transition.resume(token,projects,expected_revisions,request_key)

    def link_inventory(self,token,projects):
        if self.link_plan is None:raise NotFound('project link planning not configured')
        return self.link_plan.inventory(token,projects)

    def create_link_plan(self,token,request_key,projects,expected_bindings,role_mappings,expected_host_map_digest,broker_refs):
        if self.link_plan is None:raise NotFound('project link planning not configured')
        return self.link_plan.plan(token,request_key,projects,expected_bindings,role_mappings,expected_host_map_digest,broker_refs)

    def get_link_plan(self,token,plan_id):
        if self.link_plan is None:raise NotFound('project link planning not configured')
        return self.link_plan.get_plan(token,plan_id)

    def backup_report(self,token,*,verify_latest=False):
        if self.backup_health is None:raise NotFound('backup health not configured')
        return self.backup_health.inspect(token,verify_latest=verify_latest)

    def install_backup_policy(self,token,policy,*,expected_revision=0):
        from .backup_health import BackupHealthPolicy
        if self.backup_health is None:raise NotFound('backup health not configured')
        if type(policy) is not dict:raise ValueError('exact backup health policy required')
        try:value=BackupHealthPolicy(**policy)
        except TypeError:raise ValueError('exact backup health policy fields required') from None
        return self.backup_health.install(token,value,expected_revision=expected_revision)

    def rehearse_backup(self,token,backup_id,request_key):
        if self.backup_health is None:raise NotFound('backup health not configured')
        return self.backup_health.rehearse(token,backup_id,request_key)

    def baseline_draft(self,token,project,request_key,contract):
        self.control._scope(token,[project])
        if self.baselines is None:raise NotFound('business baselines not configured')
        return self.baselines.draft(token,project,request_key,contract)

    def baseline_current(self,token,project):
        self.control._scope(token,[project])
        if self.baselines is None:raise NotFound('business baselines not configured')
        return self.baselines.current(token,project)

    def baseline_approve(self,token,draft_id,draft_digest,*,expected_revision=0):
        from .maintenance_registry import name
        name(draft_id)
        if self.baselines is None:raise NotFound('business baselines not configured')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner',))
            row=c.execute('SELECT project FROM business_baseline_drafts WHERE id=?',(draft_id,)).fetchone()
            if row is None:raise NotFound('business baseline draft unavailable')
        self.control._scope(token,[row['project']],roles=('owner',))
        return self.baselines.approve(token,draft_id,draft_digest,expected_revision=expected_revision)

    def quality_delivery(self,token,projects,*,limit=20,cursor=None,freshness_limit=3):
        self.control._scope(token,projects)
        if self.quality_report is None:raise NotFound('quality delivery reporting not configured')
        return self.quality_report.report(token,projects,limit=limit,cursor=cursor,freshness_limit=freshness_limit)

    def handoff_packet(self,token,task_id,role,*,candidate_digest=None):
        task=self.store.get_task(token,task_id)
        self.control._scope(token,[task['project']])
        if self.quality_handoff is None:raise NotFound('quality handoff not configured')
        return self.quality_handoff.generate(token,task_id,role,candidate_digest)

    def workflow(self,token,task_id,*,limit=30):
        # get_task() expires leases as a side effect. This endpoint is a
        # recorded snapshot and must not trigger task transitions while reading.
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            task=self.store._get(c,actor,task_id)
        self.control._scope(token,[task['project']])
        if self.workflow_trace is None:raise NotFound('workflow trace not configured')
        return self.workflow_trace.report(token,task_id,limit=limit)
