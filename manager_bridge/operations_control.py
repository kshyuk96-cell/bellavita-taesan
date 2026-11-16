"""Registered operations adapters; host bindings are never selected by clients."""
from .store import NotFound
from .control_center import ControlCenter


class OperationsControl:
    def __init__(self,control,preflight,reporting,budget,*,worker_links=None,recovery=None,readiness=None):
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
