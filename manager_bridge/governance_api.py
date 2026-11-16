"""Strict authenticated proposals API. All authority resides in registered services."""
from urllib.parse import urlsplit
from .store import NotFound

PREFIX='/v1/representative/runtime/governance/'
POSTS={
    'proposals/create':(('request_key','spec'),()),
    'proposals/list':(('project',),('limit',)),
    'batches/create':(('request_key','proposal_ids'),()),
    'batches/approve':(('batch_id','batch_digest'),()),
}


def dispatch(handler,method,path,token,body=None):
    from .representative_api import _fields,_query
    from .proposal_board import ProposalBoard
    url=urlsplit(path)
    if url.scheme or url.netloc or url.fragment:raise ValueError('relative governance route required')
    if not url.path.startswith(PREFIX):return False
    route=url.path[len(PREFIX):]
    if method=='POST' and route in POSTS:
        if url.query:raise ValueError('POST query rejected')
        args=_fields(body,*POSTS[route])
    elif method=='GET' and route in ('proposals/get','batches/get'):
        args=_query(url.query,('proposal_id',) if route=='proposals/get' else ('batch_id',))
    else:return False
    store=handler.server.store
    with store._transaction() as c:
        actor=store._authenticate(c,token)
        store._role(actor,('owner',) if route=='batches/approve' else ('owner','manager'))
        project=args.get('project')
        if route=='proposals/create':
            if type(args['spec']) is not dict:raise ValueError('proposal spec required')
            project=args['spec'].get('project')
        if project is not None:
            from .maintenance_registry import name
            name(project);store._project_access(c,actor,project)
    if 'limit' in args and (type(args['limit']) is not int or not 1<=args['limit']<=100):raise ValueError('bounded list limit required')
    board=getattr(handler.server,'proposal_board',None)
    if board is None:
        # Reference-only requests cannot be scoped without the durable service.
        if project is None:raise NotFound('proposal service unavailable')
        handler.respond(200,{'ok':True,'result':{'state':'not_configured','execution_started':False,'operational_change':False}})
        return True
    if not isinstance(board,ProposalBoard) or board.store is not store:raise ValueError('exact registered proposal board required')
    names={'proposals/create':'propose','proposals/list':'list_proposals','proposals/get':'get_proposal',
           'batches/create':'create_batch','batches/approve':'approve_batch','batches/get':'get_batch'}
    result=getattr(board,names[route])(token,**args)
    handler.respond(200,{'ok':True,'result':result})
    return True
