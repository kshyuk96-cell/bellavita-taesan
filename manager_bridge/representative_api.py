"""Authenticated main-manager API dispatch; hosting supplies auth/CSRF and JSON parsing.

Callbacks, source observers and aliases are trusted server configuration. The
request cannot select code, credentials, transports, environments or executors.
Unknown routes return False so the existing server supplies its ordinary 404.
"""
from urllib.parse import parse_qs, urlsplit
from .maintenance_registry import Registry
from .manager_intake import Intake
from .delegation import Delegation
from .agent_directory import AgentDirectory
from .meeting_log import Meetings
from .manager_review import DecisionPackets, ManagerReview

PREFIX = '/v1/representative/'


def _fields(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or not value.keys() <= set(required) | set(optional):
        raise ValueError('exact representative request fields required')
    return dict(value)


def _query(query, required, optional=()):
    values=parse_qs(query,keep_blank_values=True,strict_parsing=True)
    result={}
    for key,items in values.items():
        if len(items)!=1 or not items[0]:raise ValueError('one exact query value required')
        result[key]=items[0]
    return _fields(result,required,optional)


def dispatch(handler, method, path, token, body=None):
    """Return True for a handled route, otherwise False. SDK authorization remains final.

    The caller must obtain token through the existing authenticated Handler.token
    path and parse body through its strict JSON/size/CSRF gate. This function also
    authenticates to remain safe for other trusted transport hosts.
    """
    url=urlsplit(path)
    if url.scheme or url.netloc or url.fragment:raise ValueError('relative API path required')
    if not url.path.startswith(PREFIX):return False
    if url.path.startswith(PREFIX+'runtime/'):
        from .runtime_api import dispatch as runtime_dispatch
        return runtime_dispatch(handler,method,path,token,body)
    route=url.path[len(PREFIX):]
    get_specs={
        'intake':('id',), 'delegation':('id',), 'agents':('project',),
        'agents/delivery':('id',), 'agents/poll':('project','agent_id'),
        'meetings/followup':('id',), 'packets':('id',),
    }
    post_specs={
        'intake':(('request_key','conversation','text'),('source','project_hint','follow_up')),
        'intake/task':(('id','manifest'),()),
        'delegation/propose':(('project','manager_hash','allowed_paths','allowed_checks','kinds','max_tasks','expires_at'),()),
        'delegation/activate':(('id','fingerprint'),()),
        'delegation/authorize':(('grant_id','task_id','version','manifest_digest','requested_paths'),('dependencies',)),
        'delegation/revoke':(('id',),()),
        'agents/register':(('project','agent_id','worker_hash','session_id'),('enabled','expected_revision')),
        'agents/probe':(('project','agent_id'),()),
        'agents/enqueue':(('project','agent_id','request_key','task_id','version','manifest_digest'),()),
        'agents/send':(('id',),()),
        'agents/ack':(('id','lease_token'),()),
        'agents/report':(('id','lease_token','report'),()),
        'agents/control':(('id','action'),('request_key','manifest','reason')),
        'meetings/prepare':(('project','request_key','agenda','decisions','task_ids'),()),
        'meetings/confirm':(('id','fingerprint'),()),
        'packets/prepare':(('request_key','task_ids'),('ttl',)),
        'packets/decide':(('id','fingerprint'),('decision',)),
        'review/verify':(('task_id',),()),
        'review/changes':(('projects',),('max_age',)),
        'review/blocker':(('task_id','code'),()),
    }
    if method=='GET' and route in get_specs:args=_query(url.query,get_specs[route])
    elif method=='POST' and route in post_specs:
        if url.query:raise ValueError('POST query parameters are not accepted')
        args=_fields(body,*post_specs[route])
    else:return False
    server=handler.server;store=server.store
    store.authenticate(token)
    registry=Registry(store)
    def intake():return Intake(store,registry,aliases=getattr(server,'manager_aliases',None))
    def delegation():return Delegation(store,registry,source_observers=getattr(server,'delegation_source_observers',None))
    def agents():return AgentDirectory(store,getattr(server,'agent_connectors',None))
    def meetings():return Meetings(store,registry)
    def packets():return DecisionPackets(store,registry)
    def review():return ManagerReview(store,registry,
        verification_hosts=getattr(server,'manager_verification_hosts',None),
        change_observers=getattr(server,'manager_change_observers',None),
        rehearsals=getattr(server,'manager_merge_rehearsals',None))
    if method=='GET':
        if route=='intake':result=intake().get(token,args['id'])
        elif route=='delegation':result=delegation().get(token,args['id'])
        elif route=='agents':result=agents().list(token,args['project'])
        elif route=='agents/delivery':result=agents().get(token,args['id'])
        elif route=='agents/poll':result=agents().poll(token,args['project'],args['agent_id'])
        elif route=='meetings/followup':result=meetings().follow_up(token,args['id'])
        else:result=packets().get(token,args['id'])
    elif route=='intake':result=intake().receive(token,**args)
    elif route=='intake/task':result=intake().submit_task(token,args['id'],args['manifest'])
    elif route=='delegation/propose':result=delegation().propose(token,**args)
    elif route=='delegation/activate':result=delegation().activate(token,args['id'],args['fingerprint'])
    elif route=='delegation/authorize':result=delegation().authorize(token,**args)
    elif route=='delegation/revoke':result=delegation().revoke(token,args['id'])
    elif route=='agents/register':result=agents().register(token,**args)
    elif route=='agents/probe':result=agents().probe(token,**args)
    elif route=='agents/enqueue':result=agents().enqueue(token,**args)
    elif route=='agents/send':result=agents().send(token,args['id'])
    elif route=='agents/ack':result=agents().acknowledge(token,args['id'],args['lease_token'])
    elif route=='agents/report':result=agents().report(token,args['id'],args['lease_token'],args['report'])
    elif route=='agents/control':
        key=args.pop('id');result=agents().control(token,key,**args)
    elif route=='meetings/prepare':result=meetings().prepare(token,**args)
    elif route=='meetings/confirm':result=meetings().confirm(token,args['id'],args['fingerprint'])
    elif route=='packets/prepare':result=packets().prepare(token,**args)
    elif route=='packets/decide':result=packets().decide(token,args['id'],args['fingerprint'],decision=args.get('decision','approve'))
    elif route=='review/verify':result=review().verify(token,args['task_id'])
    elif route=='review/changes':result=review().changes(token,**args)
    else:result=review().blocker(token,args['task_id'],args['code'])
    handler.respond(200,{'ok':True,'result':result})
    return True
