"""Private host composition. Configuration and executable bindings never enter HTTP."""
import threading
from urllib.parse import urlsplit
from .continuous_host import load_host, _private_json, _private_file
from .server import make_server


def _listener(value):
    if type(value) is not dict or set(value)-{'host','port','public_origin'}:
        raise ValueError('exact listener settings required')
    host=value.get('host','127.0.0.1');port=value.get('port',8791);origin=value.get('public_origin')
    if type(host) is not str or not host or type(port) is not int or not 0<=port<=65535:
        raise ValueError('bounded listener required')
    if origin is not None:
        parsed=urlsplit(origin) if type(origin) is str else None
        if parsed is None or parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise ValueError('explicit HTTPS public origin required')
    if host not in ('127.0.0.1','localhost','::1') and origin is None:
        raise ValueError('external listener requires explicit public origin')
    return host,port,origin


class GovernanceService:
    def __init__(self,host,controller,settings,server):
        self.host,self.controller,self.settings,self.server=host,controller,settings,server
        self.stop_event=threading.Event();server.governance_stop_event=self.stop_event
        self._thread=None;self.source_states={};server.governance_service=self
        self._manager_running=False
        self.supervision=None
        self.recovery=None
        self.runtime_transition=None

    def stop(self):
        # Signal handler safe: controller fences observe this before dispatch.
        self.stop_event.set()

    def status(self):
        result=self.controller.status(self.host.manager_token(),self.settings['projects'])
        result['source_watch']=[{'project':project,'state':state} for project,state in sorted(self.source_states.items())]
        if self.supervision is not None:result['supervision']=self.supervision.report(self.host.manager_token(),self.settings['projects'])
        if self.runtime_transition is not None:result['runtime_transition']=self.runtime_transition.status(self.host.manager_token(),self.settings['projects'])
        return result

    def health(self,token,projects):
        """Current process observations only; never attests durable hosting or uptime."""
        from .store import PermissionDenied
        self.controller.status(token,projects)
        if any(p not in self.settings['projects'] for p in projects):
            raise PermissionDenied('health outside configured service scope')
        alive=self._thread is not None and self._thread.is_alive()
        stopped=self.stop_event.is_set()
        return {'state':'stopping' if stopped else ('running' if alive else 'unavailable'),
                'http_state':'listening' if alive and not stopped else ('stopped' if stopped else 'unknown'),
                'manager_state':'running' if self._manager_running and not stopped else ('stopped' if stopped else 'idle'),
                'observed_at':self.host.store._now()}

    def refresh_sources(self,token,projects):
        from .store import PermissionDenied
        if any(project not in self.settings['projects'] for project in projects):
            raise PermissionDenied('registered service project required')
        states={}
        for project in projects:
            if self.stop_event.is_set():return False
            observation=self.server.source_watch.inspect(token,project)
            states[project]=observation['state']
        self.source_states.update(states)
        return bool(states) and all(state in ('baseline','unchanged','changed') for state in states.values())

    def run(self,*,max_ticks=None):
        if self.recovery is None: return self._run(max_ticks=max_ticks)
        with self.recovery.instance_guard(self.host.manager_token(),self.settings['projects'],self.settings['instance_id']):
            return self._run(max_ticks=max_ticks)

    def _run(self,*,max_ticks=None):
        if max_ticks is not None and (type(max_ticks) is not int or not 1<=max_ticks<=100000):
            raise ValueError('bounded rehearsal tick count required')
        self._thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.1},daemon=True)
        self._thread.start()
        shutdown_thread=threading.Thread(target=self._shutdown_when_stopped,daemon=True)
        shutdown_thread.start()
        try:
            self._manager_running=True
            ticks=0;blocked=0;dispatch_scope=None;inactive=[]
            while not self.stop_event.is_set() and (max_ticks is None or ticks<max_ticks):
                token=self.host.manager_token()
                fully_drained=False
                if self.runtime_transition is not None:
                    transition=self.runtime_transition.status(token,self.settings['projects'])
                    fully_drained=all(item['new_admission_blocked'] for item in transition['projects'])
                if self.supervision is not None and not fully_drained:self.supervision.poll(token,stop_event=self.stop_event)
                if self.stop_event.is_set():break
                if self.controller.admission_budget is not None:
                    self.controller.admission_budget.poll_auth(token,self.settings['projects'])
                    host_gate=self.controller.admission_budget.host_admission
                    if host_gate is not None:host_gate.poll(token,self.settings['projects'])
                if self.stop_event.is_set():break
                active=tuple(p for p in self.settings['projects'] if self.host.registry.get(token,p)['config']['maintenance_enabled'])
                inactive=[p for p in self.settings['projects'] if p not in active]
                if dispatch_scope is not None and active!=dispatch_scope:
                    self.controller.release(token,dispatch_scope,self.settings['instance_id'])
                    dispatch_scope=None
                if hasattr(self.server,'proposal_board'):
                    self.refresh_sources(token,self.settings['projects'])
                    ready=bool(active) and all(self.source_states.get(p) in ('baseline','unchanged','changed') for p in active)
                else:ready=bool(active)
                if ready:
                    dispatch_scope=active
                    self.controller.tick(token,active,self.settings['instance_id'],stop_event=self.stop_event)
                elif active:blocked+=1
                ticks+=1
                if max_ticks is None or ticks<max_ticks:self.stop_event.wait(self.settings['poll_seconds'])
            return {'state':'stopped' if self.stop_event.is_set() else 'bounded_run_complete','ticks':ticks,
                    'source_blocked_ticks':blocked,'source_watch':[{'project':p,'state':v} for p,v in sorted(self.source_states.items())],
                    'inactive_projects':inactive,
                    'always_on_verified':False,'operational_change':False}
        finally:
            self._manager_running=False
            self.stop()
            try:self.controller.release(self.host.manager_token(),dispatch_scope or self.settings['projects'],self.settings['instance_id'])
            finally:shutdown_thread.join();self._thread.join();self.server.server_close()

    def _shutdown_when_stopped(self):
        self.stop_event.wait()
        self.server.shutdown()

    def close(self):
        self.stop();self.server.server_close()


def load_service(config_file,*,subscription_runtime=None):
    config=_private_json(config_file)
    if config.get('version')!=1 or type(config.get('version')) is not int or not {'version','host_config_file'}<=set(config) or set(config)-{'version','host_config_file','listen','governance','supervision','local_host_admission'}:
        raise ValueError('exact governance service configuration required')
    listener=config.get('listen',{})
    if 'governance' in config and (type(config['governance']) is not dict or config['governance']):
        raise ValueError('exact governance settings required')
    if 'local_host_admission' in config and ('governance' not in config or type(config['local_host_admission']) is not dict or config['local_host_admission']):
        raise ValueError('local host admission requires exact governance registration')
    host_config=_private_file(config['host_config_file'])
    if subscription_runtime is not None:
        from .managed_subscription_runtime import ManagedSubscriptionRuntime
        if type(subscription_runtime) is not ManagedSubscriptionRuntime:
            raise ValueError('trusted managed subscription runtime required')
        host,controller,settings=load_host(host_config,profile_guard_factory=subscription_runtime.guard_factory)
    else:
        host,controller,settings=load_host(host_config)
    supervision=None
    if 'supervision' in config:
        from .governance_supervision import SupervisionSettings
        if type(config['supervision']) is not dict:raise ValueError('explicit supervision settings required')
        supervision=SupervisionSettings(**config['supervision'])
    subscription_broker=None
    if subscription_runtime is not None:
        subscription_broker=subscription_runtime.bind(host,settings,supervision)
    host_admission=None
    if 'local_host_admission' in config:
        from .host_admission import HostAdmission,LocalHostObserver
        host_settings=_private_json(host_config)
        host_admission=HostAdmission(host.core,observer=LocalHostObserver(
            application_root=host_settings['application_root'],state_root=host_settings['data_root']))
    return build_service(host,controller,settings,listen=listener,governance='governance' in config,
        supervision=supervision,host_admission=host_admission,
        owner_token_loader=None if subscription_runtime is None else subscription_runtime.owner_token,
        subscription_broker=subscription_broker,
        link_templates=({e['project']:e for e in _private_json(host_config).get('projects',[])} if 'governance' in config else None))


def build_service(host,controller,settings,*,listen=None,governance=False,
                  qa_providers=None,source_probes=None,connection_profiles=None,
                  supervision=None,owner_token_loader=None,expiration_provider=None,subscription_broker=None,
                  operating_budget=None,operations_metadata=(),artifact_sources=None,worker_links=None,host_admission=None,
                  backup_health=None,project_link_plan=None,link_templates=None):
    """Compose already trusted host objects; provider arguments never come from HTTP/config."""
    from .continuous_manager import ContinuousManager
    from .three_tier import ThreeTier
    from .source_watch import SourceWatch
    from .governance_gate import GovernanceGate
    from .proposal_board import ProposalBoard
    from .project_onboarding import Onboarding
    from .connection_readiness import Connections
    from .governance_routing import GovernanceRouting
    from .governance_portfolio import Portfolio
    if not isinstance(host.core,ThreeTier) or host.core.store is not host.store or not isinstance(controller,ContinuousManager) or controller.core is not host.core:
        raise ValueError('same trusted host and controller required')
    if type(governance) is not bool or (any(v is not None for v in (qa_providers,connection_profiles)) and not governance):
        raise ValueError('explicit governance registration required')
    if not governance and (operating_budget is not None or operations_metadata or artifact_sources is not None or worker_links is not None or host_admission is not None or backup_health is not None or project_link_plan is not None or link_templates is not None):
        raise ValueError('explicit governance operations registration required')
    if governance:
        from .operating_budget import OperatingBudget
        if controller.admission_budget is not None and host.core.admission_budget is not None and controller.admission_budget is not host.core.admission_budget:
            raise ValueError('core and controller admission bindings differ')
        existing_budget=controller.admission_budget or host.core.admission_budget
        if operating_budget is not None and (not isinstance(operating_budget,OperatingBudget) or operating_budget.core is not host.core):
            raise ValueError('same registered operating budget required')
        if existing_budget is not None and operating_budget is not None and existing_budget is not operating_budget:
            raise ValueError('controller already has a different admission budget')
        budget=operating_budget or existing_budget or OperatingBudget(host.core,host_admission=host_admission)
        if host_admission is not None and budget.host_admission is not host_admission:
            raise ValueError('budget and local host admission bindings differ')
    poll=settings.get('poll_seconds')
    if type(poll) not in (int,float) or not 1<=poll<=60:raise ValueError('bounded source polling required')
    listener=_listener({} if listen is None else listen)
    # Validate actor scope before creating a socket or exposing any service.
    controller.status(host.manager_token(),settings['projects'])
    if supervision is not None:
        from .governance_supervision import GovernanceSupervision
        GovernanceSupervision.validate_registration(host.core,settings['projects'],supervision,owner_token_loader,subscription_broker)
    elif subscription_broker is not None:raise ValueError('explicit supervision settings required')
    watch=SourceWatch(host.store,host.registry,source_probes)
    gate=GovernanceGate(host.store,host.registry,providers=qa_providers) if governance else None
    if gate is not None:gate.enroll(host.manager_token(),settings['projects'])
    server=make_server(host.store,listener[0],listener[1],public_origin=listener[2])
    try:
        server.three_tier=host.core;server.fleet=host.fleet;server.continuous_manager=controller
        server.source_watch=watch;server.project_source_probes=dict(source_probes or {})
        if governance:
            host.core.governance_gate=gate
            server.proposal_board=ProposalBoard(host.store,host.registry);server.governance_gate=gate
            server.governance_onboarding=Onboarding(host.store,host.registry)
            connections=Connections(host.store,host.registry,connection_profiles)
            routing=GovernanceRouting(host.core,controller)
            server.governance_portfolio=Portfolio(host.core,connections=connections,board=server.proposal_board,routing=routing)
        service=GovernanceService(host,controller,settings,server)
        if governance:server.governance_portfolio.health=service.health
        if supervision is not None:
            from .governance_supervision import GovernanceSupervision
            service.supervision=GovernanceSupervision(host,controller,settings['projects'],supervision,
                health=service.health,owner_token_loader=owner_token_loader,expiration_provider=expiration_provider,subscription_broker=subscription_broker)
            server.governance_supervision=service.supervision
        elif owner_token_loader is not None or expiration_provider is not None:
            raise ValueError('explicit supervision settings required')
        if governance:
            from .control_onboarding import ControlOnboarding
            from .control_digest import ControlDigest
            from .control_center import ControlCenter
            onboarding=ControlOnboarding(host.store,host.registry,connections=connections,onboarding=server.governance_onboarding)
            summary=ControlDigest(server.governance_portfolio,routing=routing,supervision=service.supervision)
            server.control_center=ControlCenter(host.core,controller,server.proposal_board,server.governance_portfolio,
                settings['projects'],settings['instance_id'],onboarding=onboarding,summary=summary,
                stop_event=service.stop_event,source_preflight=service.refresh_sources)
            from .project_connection_preflight import ProjectConnectionPreflight
            from .operations_reporting import OperationsReporting
            from .operations_control import OperationsControl
            preflight=ProjectConnectionPreflight(host.store,host.registry,onboarding,metadata=operations_metadata)
            reporting=OperationsReporting(host.core,summary,controller=controller,artifact_sources=artifact_sources,budget=budget)
            from .recovery_readiness import RecoveryReadiness
            from .portfolio_readiness import PortfolioReadiness
            service.recovery=RecoveryReadiness(host.core,controller=controller,supervision=service.supervision)
            from .runtime_transition import RuntimeTransition
            from .project_link_plan import ProjectLinkPlan
            from .continuous_host import ContinuousHost
            service.runtime_transition=RuntimeTransition(host.core,supervision=service.supervision)
            links=project_link_plan or ProjectLinkPlan(host.core,host if isinstance(host,ContinuousHost) else None,
                brokers=() if subscription_broker is None else (subscription_broker,),templates=link_templates)
            from .business_baselines import BusinessBaselines
            from .quality_delivery_report import QualityDeliveryReport
            from .worker_quality_handoff import WorkerQualityHandoff
            from .workflow_trace import WorkflowTrace
            baselines=BusinessBaselines(host.core)
            quality_report=QualityDeliveryReport(host.core,baselines)
            quality_handoff=WorkerQualityHandoff(host.core)
            readiness=PortfolioReadiness(host.core,connections=connections,budget=budget,operations_reporting=reporting)
            server.operations_control=OperationsControl(server.control_center,preflight,reporting,budget,
                worker_links=worker_links,recovery=service.recovery,readiness=readiness,
                transition=service.runtime_transition,link_plan=links,backup_health=backup_health,
                baselines=baselines,quality_report=quality_report,quality_handoff=quality_handoff,workflow_trace=WorkflowTrace(host.core))
            controller.admission_budget=budget
            host.core.admission_budget=budget
        return service
    except BaseException:
        server.server_close();raise
