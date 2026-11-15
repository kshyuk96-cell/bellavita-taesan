"""Strict authenticated transport for registered three-tier development services.

No callbacks, commands, source paths, proof flags, or worker success claims are
accepted. The host installs the execution service; client JSON never chooses it.
"""
from urllib.parse import urlsplit
from .store import PermissionDenied

PREFIX='/v1/representative/runtime/three-tier/'
POSTS={
    'members/register':(('project','member_id','credential_hash','role'),('agent_id',)),
    'limits':((),('project','max_active','max_review','max_reviewers')),
    'start':(('task_id','developer_id','request_key'),()),
    'development/claim':(('task_id',),('lease_seconds',)),
    'candidate':(('task_id',),()),
    'review/claim':(('task_id',),('lease_seconds',)),
    'review/reconcile':(('task_id',),()),
    'review/verify':(('task_id','lease_token'),()),
    'decision':(('task_id','candidate_digest'),('decision',)),
    'rework/claim':(('task_id',),('lease_seconds',)),
    'next':(('current_task_id','next_task_id','expected_parent_digest','request_key'),('developer_id',)),
    'overview':(('projects',),()),
    'dashboard':(('projects',),()),
}


def dispatch(handler,method,path,token,body=None):
    from .representative_api import _fields,_query
    from .three_tier import ThreeTier
    from .three_tier_dashboard import build_summary,render_dashboard
    url=urlsplit(path)
    if url.scheme or url.netloc or url.fragment:raise ValueError('relative three-tier path required')
    if not url.path.startswith(PREFIX):return False
    route=url.path[len(PREFIX):]
    if method=='POST' and route in POSTS:
        if url.query:raise ValueError('POST query rejected')
        args=_fields(body,*POSTS[route])
    elif method=='GET' and route=='task':args=_query(url.query,('task_id',))
    else:return False
    store=handler.server.store;store.authenticate(token)
    service=getattr(handler.server,'three_tier',None)
    if service is None:
        handler.respond(200,{'ok':True,'result':{'state':'not_configured','execution_started':False,
            'operational_change':False,'existing_chat_control_verified':False}})
        return True
    if not isinstance(service,ThreeTier) or service.store is not store:raise ValueError('registered three-tier service required')
    if route in {'review/verify','development/claim','review/claim','rework/claim'}:
        actor=store.authenticate(token)
        if actor['role']!='worker':raise PermissionDenied('assigned worker action required')
    if route=='members/register':result=service.register(token,**args)
    elif route=='limits':result=service.set_limits(token,**args)
    elif route=='start':result=service.start(token,**args)
    elif route=='development/claim':result=service.claim_development(token,**args)
    elif route=='candidate':result=service.submit_candidate(token,**args)
    elif route=='review/claim':result=service.claim_review(token,**args)
    elif route=='review/reconcile':result=service.reconcile_review(token,**args)
    elif route=='review/verify':result=service.verify_review(token,**args)
    elif route=='decision':result=service.decide(token,**args)
    elif route=='rework/claim':result=service.claim_rework(token,**args)
    elif route=='next':result=service.approve_next(token,**args)
    elif route=='task':result=service.get(token,**args)
    else:
        result=build_summary(service,token,**args)
        if route=='dashboard':result={'html':render_dashboard(result),'content_type':'text/html; charset=utf-8',
             'operational_change':False,'existing_chat_control_verified':False}
    handler.respond(200,{'ok':True,'result':result})
    return True
