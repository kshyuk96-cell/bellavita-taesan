"""Strict control-center routes; credentials and executable bindings stay on host."""
from urllib.parse import urlsplit
from .control_center import ControlCenter
from .store import NotFound

PREFIX='/v1/representative/runtime/governance/control/'
ROUTES={
    'bundles/prepare':('prepare_bundle',('request_key','specs'),()),
    'bundles/get':('get_bundle',('batch_id',),()),
    'bundles/approve':('approve_bundle',('batch_id','batch_digest'),()),
    'step':('step',('projects',),('limit',)),
    'tasks/get':('task',('task_id',),()),
    'report':('report',('projects',),()),
    'connections/inventory':('inventory',('projects',),()),
    'connections/plan':('plan_connections',('request_key','projects','expected_bindings'),()),
    'connections/get':('get_connection_plan',('plan_id',),()),
}


def dispatch(handler,method,path,token,body=None):
    from .representative_api import _fields
    url=urlsplit(path)
    if url.scheme or url.netloc or url.fragment:raise ValueError('relative control route required')
    if not url.path.startswith(PREFIX):return False
    route=url.path[len(PREFIX):]
    if route.startswith('operations/'):
        from .operations_control_api import dispatch as operations_dispatch
        return operations_dispatch(handler,method,path,token,body)
    if method!='POST' or route not in ROUTES:return False
    if url.query:raise ValueError('POST query rejected')
    action,required,optional=ROUTES[route];args=_fields(body,required,optional)
    store=handler.server.store
    with store._transaction() as c:
        actor=store._authenticate(c,token)
        store._role(actor,('owner',) if route=='bundles/approve' else (('manager',) if route=='step' else ('owner','manager')))
    control=getattr(handler.server,'control_center',None)
    if control is None:raise NotFound('control center not configured')
    if not isinstance(control,ControlCenter) or control.store is not store:raise ValueError('registered control center required')
    result=getattr(control,action)(token,**args)
    handler.respond(200,{'ok':True,'result':result})
    return True
