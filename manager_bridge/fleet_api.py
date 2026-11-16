"""Authenticated fleet and continuous-manager controls, never user executors."""
from urllib.parse import urlsplit

PREFIX='/v1/representative/runtime/'
FLEET={
 'hosts/register':(('project','host_ref'),()),
 'templates/register':(('project','template_ref','host_ref','max_sessions','expires_at'),()),
 'discover':(('project','host_ref'),()),
 'enroll':(('project','host_ref','external_ref'),()),
 'create':(('task_id','template_ref','request_key'),()),
 'reserve':(('task_id','template_ref','request_key'),()),
 'reserved/begin':(('reservation_id',),()),
 'reserved/bind':(('reservation_id',),()),
 'inventory':(('projects',),()),
 'delivery/verify':(('project','host_ref','external_ref','task_id'),()),
}
CONTINUOUS={
 'tick':(('projects','instance_id'),('limit','lease_seconds')),
 'status':(('projects',),('limit',)),
 'reconcile':(('projects','instance_id','action_key'),('lease_seconds',)),
 'release':(('projects','instance_id'),()),
}


def dispatch(handler,method,path,token,body=None):
    from .representative_api import _fields,_query
    from .fleet import Fleet
    from .continuous_manager import ContinuousManager
    url=urlsplit(path)
    if url.scheme or url.netloc or url.fragment:raise ValueError('relative management route required')
    if not url.path.startswith(PREFIX):return False
    route=url.path[len(PREFIX):]
    family=route.split('/',1)[0]
    if family not in {'fleet','continuous'} or '/' not in route:return False
    action=route.split('/',1)[1];spec=FLEET if family=='fleet' else CONTINUOUS
    if method=='POST' and action in spec:
        if url.query:raise ValueError('POST query rejected')
        args=_fields(body,*spec[action])
    elif method=='GET' and family=='fleet' and action=='creation':args=_query(url.query,('creation_id',))
    else:return False
    store=handler.server.store
    with store._transaction() as c:
        actor=store._authenticate(c,token);store._role(actor,('owner','manager'))
        projects=args.get('projects')
        if projects is not None:
            if type(projects) not in (list,tuple) or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):raise ValueError('bounded explicit projects required')
        elif 'project' in args:projects=[args['project']]
        elif 'task_id' in args:projects=[store._get(c,actor,args['task_id'])['project']]
        elif 'creation_id' in args or 'reservation_id' in args:
            from .store import NotFound
            row=c.execute('SELECT project FROM fleet_creates WHERE id=?',(args.get('creation_id',args.get('reservation_id')),)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_creates'").fetchone() else None
            if row is None:raise NotFound('creation unavailable')
            projects=[row['project']]
        for project in projects or ():
            from .maintenance_registry import name
            name(project);store._project_access(c,actor,project)
    for field,low,high in (('limit',1,20 if family=='continuous' and action=='tick' else 100),('lease_seconds',5,300)):
        if field in args and (type(args[field]) is not int or not low<=args[field]<=high):raise ValueError('bounded management integer required')
    service=getattr(handler.server,'fleet' if family=='fleet' else 'continuous_manager',None)
    if service is None:
        handler.respond(200,{'ok':True,'result':{'state':'not_configured','execution_started':False,
            'operational_change':False,'existing_chat_control_verified':False,'always_on_host_verified':False}})
        return True
    expected=Fleet if family=='fleet' else ContinuousManager
    if not isinstance(service,expected) or service.store is not store:raise ValueError('exact registered management service required')
    stop_event=getattr(handler.server,'governance_stop_event',None)
    if family=='continuous' and action in ('tick','reconcile') and stop_event is not None and stop_event.is_set():
        handler.respond(200,{'ok':True,'result':{'state':'stopped','actions':[],'execution_started':False,'automatic_replay':False,'operational_change':False}})
        return True
    governance=getattr(handler.server,'governance_service',None)
    if family=='continuous' and action in ('tick','reconcile') and governance is not None and hasattr(handler.server,'proposal_board'):
        if not governance.refresh_sources(token,args['projects']):
            handler.respond(200,{'ok':True,'result':{'state':'source_unavailable','actions':[],'execution_started':False,'automatic_replay':False,'operational_change':False}})
            return True
    if family=='fleet':
        names={'hosts/register':'register_host','templates/register':'register_template','discover':'discover',
            'enroll':'enroll','create':'create_for_task','reserve':'reserve_for_task','reserved/begin':'begin_reserved_execution','reserved/bind':'bind_reserved_execution','creation':'get_creation','inventory':'inventory','delivery/verify':'verify_delivery'}
        result=getattr(service,names[action])(token,**args)
    elif action in ('tick','reconcile') and stop_event is not None:result=getattr(service,action)(token,stop_event=stop_event,**args)
    else:result=getattr(service,action)(token,**args)
    handler.respond(200,{'ok':True,'result':result})
    return True
