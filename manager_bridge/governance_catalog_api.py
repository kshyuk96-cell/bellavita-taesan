"""Owner-only catalog registration; no activation, source paths or credentials."""
from urllib.parse import urlsplit
from .maintenance_registry import name
from .project_onboarding import Onboarding
from .store import NotFound, PermissionDenied

PREFIX='/v1/representative/runtime/governance/catalog/'


def dispatch(handler,method,path,token,body=None):
    from .representative_api import _fields
    url=urlsplit(path)
    if url.scheme or url.netloc or url.fragment:raise ValueError('relative catalog path required')
    if not url.path.startswith(PREFIX):return False
    action=url.path[len(PREFIX):]
    if method!='POST' or action not in ('list','register'):return False
    if url.query:raise ValueError('POST query rejected')
    args=_fields(body,('projects','expected_revisions') if action=='register' else ('projects',),())
    projects=args['projects'];store=handler.server.store
    if (type(projects) is not list or not 1<=len(projects)<=4
            or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects)):
        raise ValueError('explicit unique catalog projects required')
    with store._transaction() as c:
        actor=store._authenticate(c,token)
        store._role(actor,('owner',) if action=='register' else ('owner','manager'))
        for project in projects:
            name(project)
            if not store._scope(actor,project):raise PermissionDenied('catalog project outside actor scope')
    service=getattr(handler.server,'governance_onboarding',None)
    if not isinstance(service,Onboarding) or service.store is not store:raise NotFound('catalog service unavailable')
    if action=='register':result=service.register_catalog(token,projects,args['expected_revisions'])
    else:
        catalog=service.catalog()
        if any(p not in catalog for p in projects):raise ValueError('unknown catalog project')
        result={'projects':[{'project':p,'config':catalog[p]} for p in projects],
                'registration_performed':False,'execution_authorized':False}
    handler.respond(200,{'ok':True,'result':result})
    return True
