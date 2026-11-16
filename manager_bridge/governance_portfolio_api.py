"""Read-only portfolio API; registration is a trusted server concern."""
from urllib.parse import urlsplit
from .governance_portfolio import Portfolio,render_dashboard

PREFIX='/v1/representative/runtime/governance/portfolio/'


def dispatch(handler,method,path,token,body=None):
    from .representative_api import _fields
    from .maintenance_registry import Registry
    url=urlsplit(path)
    if url.scheme or url.netloc or url.fragment:raise ValueError('relative portfolio route required')
    if not url.path.startswith(PREFIX):return False
    action=url.path[len(PREFIX):]
    if method!='POST' or action not in ('report','decision-packet','dashboard'):return False
    if url.query:raise ValueError('POST query rejected')
    args=_fields(body,('projects',),())
    projects=args['projects'];store=handler.server.store
    if type(projects) is not list or not 1<=len(projects)<=100 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):
        raise ValueError('explicit bounded projects required')
    registry=Registry(store)
    with store._transaction() as c:
        for project in projects:registry._get(c,token,project)
    service=getattr(handler.server,'governance_portfolio',None)
    if service is None:
        handler.respond(200,{'ok':True,'result':{'state':'not_configured','operational_authorization':False}})
        return True
    if not isinstance(service,Portfolio) or service.store is not store:raise ValueError('registered portfolio required')
    result=(service.decision_packet(token,projects) if action=='decision-packet' else service.report(token,projects))
    if action=='dashboard':result={'html':render_dashboard(result),'content_type':'text/html; charset=utf-8','operational_authorization':False}
    handler.respond(200,{'ok':True,'result':result})
    return True
