"""Registered operations adapters; host bindings are never selected by clients."""
from .store import NotFound
from .control_center import ControlCenter


class OperationsControl:
    def __init__(self,control,preflight,reporting,budget):
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

    def inspect(self,token,projects):
        self.control._scope(token,projects)
        return self.preflight.inspect(token,projects)

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
