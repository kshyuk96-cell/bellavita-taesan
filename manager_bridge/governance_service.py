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

    def stop(self):
        # Signal handler safe: controller fences observe this before dispatch.
        self.stop_event.set()

    def status(self):
        result=self.controller.status(self.host.manager_token(),self.settings['projects'])
        result['source_watch']=[{'project':project,'state':state} for project,state in sorted(self.source_states.items())]
        return result

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
        if max_ticks is not None and (type(max_ticks) is not int or not 1<=max_ticks<=100000):
            raise ValueError('bounded rehearsal tick count required')
        self._thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.1},daemon=True)
        self._thread.start()
        shutdown_thread=threading.Thread(target=self._shutdown_when_stopped,daemon=True)
        shutdown_thread.start()
        try:
            ticks=0;blocked=0
            while not self.stop_event.is_set() and (max_ticks is None or ticks<max_ticks):
                token=self.host.manager_token()
                ready=self.refresh_sources(token,self.settings['projects']) if hasattr(self.server,'proposal_board') else True
                if ready:
                    self.controller.tick(token,self.settings['projects'],self.settings['instance_id'],stop_event=self.stop_event)
                else:blocked+=1
                ticks+=1
                if max_ticks is None or ticks<max_ticks:self.stop_event.wait(self.settings['poll_seconds'])
            return {'state':'stopped' if self.stop_event.is_set() else 'bounded_run_complete','ticks':ticks,
                    'source_blocked_ticks':blocked,'source_watch':[{'project':p,'state':v} for p,v in sorted(self.source_states.items())],
                    'always_on_verified':False,'operational_change':False}
        finally:
            self.stop()
            try:self.controller.release(self.host.manager_token(),self.settings['projects'],self.settings['instance_id'])
            finally:shutdown_thread.join();self._thread.join();self.server.server_close()

    def _shutdown_when_stopped(self):
        self.stop_event.wait()
        self.server.shutdown()

    def close(self):
        self.stop();self.server.server_close()


def load_service(config_file):
    config=_private_json(config_file)
    if config.get('version')!=1 or type(config.get('version')) is not int or not {'version','host_config_file'}<=set(config) or set(config)-{'version','host_config_file','listen','governance'}:
        raise ValueError('exact governance service configuration required')
    listener=config.get('listen',{})
    if 'governance' in config and (type(config['governance']) is not dict or config['governance']):
        raise ValueError('exact governance settings required')
    host_config=_private_file(config['host_config_file'])
    host,controller,settings=load_host(host_config)
    return build_service(host,controller,settings,listen=listener,governance='governance' in config)


def build_service(host,controller,settings,*,listen=None,governance=False,
                  qa_providers=None,source_probes=None):
    """Compose already trusted host objects; provider arguments never come from HTTP/config."""
    from .continuous_manager import ContinuousManager
    from .three_tier import ThreeTier
    from .source_watch import SourceWatch
    from .governance_gate import GovernanceGate
    from .proposal_board import ProposalBoard
    if not isinstance(host.core,ThreeTier) or host.core.store is not host.store or not isinstance(controller,ContinuousManager) or controller.core is not host.core:
        raise ValueError('same trusted host and controller required')
    if type(governance) is not bool or (qa_providers is not None and not governance):
        raise ValueError('explicit governance registration required')
    poll=settings.get('poll_seconds')
    if type(poll) not in (int,float) or not 1<=poll<=60:raise ValueError('bounded source polling required')
    listener=_listener({} if listen is None else listen)
    # Validate actor scope before creating a socket or exposing any service.
    controller.status(host.manager_token(),settings['projects'])
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
        return GovernanceService(host,controller,settings,server)
    except BaseException:
        server.server_close();raise
