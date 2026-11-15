"""Typed project runtime API; all providers/executors are host registrations.

This transport does not start workers or allow a request to configure code,
credentials, a sandbox, callbacks, source URLs, or an operational deployment.
"""
from urllib.parse import urlsplit
from .maintenance_registry import Registry, name, scoped
from .store import NotFound

PREFIX='/v1/representative/runtime/'


def dispatch(handler,method,path,token,body=None):
    from .representative_api import _fields, _query
    url=urlsplit(path)
    if url.scheme or url.netloc or url.fragment:raise ValueError('relative runtime path required')
    if not url.path.startswith(PREFIX):return False
    route=url.path[len(PREFIX):]
    posts={
        'business/read':(('project','action'),('query',)),
        'business/event':(('project','order_id','event_key'),()),
        'source/inspect':(('project',),()),
        'source/pending':(('projects',),('limit',)),
        'source/ack':(('event_id',),()),
        'delegations/activate':(('items',),()),
        'delegations/inventory':(('projects',),()),
        'failures/collect':(('project',),('max_age',)),
        'failures/review':(('project','incident_id','selectors'),()),
        'failures/evaluate':(('project',),('max_age',)),
        'receiver/check':(('project','agent_id'),()),
        'receiver/recovery':(('project','agent_id'),()),
    }
    if method=='POST' and route in posts:
        if url.query:raise ValueError('POST query rejected')
        args=_fields(body,*posts[route])
    elif method=='GET' and route=='failures':args=_query(url.query,('project',))
    else:return False
    server=handler.server;store=server.store
    store.authenticate(token);registry=Registry(store)
    if route.startswith('business/'):
        from .project_adapters import ProjectAdapters
        service=ProjectAdapters(store,registry,getattr(server,'project_business_providers',None))
        result=service.read(token,**args) if route.endswith('/read') else service.sync_event(token,**args)
    elif route.startswith('source/'):
        from .source_watch import SourceWatch
        service=SourceWatch(store,registry,getattr(server,'project_source_probes',None))
        if route=='source/inspect':result=service.inspect(token,**args)
        elif route=='source/pending':result=service.pending(token,**args)
        else:result=service.acknowledge(token,args['event_id'])
    elif route.startswith('delegations/'):
        from .delegation import Delegation
        service=Delegation(store,registry)
        result=service.activate_batch(token,args['items']) if route.endswith('/activate') else service.inventory(token,args['projects'])
    elif route.startswith('failures'):
        from .failure_regressions import FailureRegressions
        service=FailureRegressions(store,registry,getattr(server,'project_failure_profiles',None))
        if route=='failures':result=service.incidents(token,**args)
        elif route.endswith('/collect'):result=service.collect(token,**args)
        elif route.endswith('/review'):result=service.review(token,**args)
        else:result=service.evaluate(token,**args)
    else:
        from .project_runtime import ProjectReceiver
        project=args['project'];agent_id=args['agent_id'];name(project);name(agent_id)
        with store._transaction() as c:
            scoped(store,c,token,project,('worker',))
            if c.execute('SELECT 1 FROM maintenance_projects WHERE project=?',(project,)).fetchone() is None:
                raise NotFound('runtime project not registered')
        receiver=getattr(server,'project_receivers',{}).get((project,agent_id))
        if receiver is None:
            result={'project':project,'state':'not_configured','execution_started':False,'automatically_replayed':False}
        else:
            if not isinstance(receiver,ProjectReceiver) or receiver.store is not store or receiver.profile.project!=project:
                raise ValueError('exact trusted project receiver required')
            result=receiver.check(token,agent_id) if route.endswith('/check') else receiver.recovery(token,agent_id)
    handler.respond(200,{'ok':True,'result':result})
    return True
