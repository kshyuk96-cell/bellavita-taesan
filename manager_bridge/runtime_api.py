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
    if route.startswith(('fleet/','continuous/')):
        from .fleet_api import dispatch as management_dispatch
        return management_dispatch(handler,method,path,token,body)
    if route.startswith('three-tier/'):
        from .three_tier_api import dispatch as hierarchy_dispatch
        return hierarchy_dispatch(handler,method,path,token,body)
    posts={
        'conversation/export':(('project','channel','conversation','snapshot_id'),()),
        'conversation/select':(('project','channel','conversation','request_id','selection_id','generation'),()),
        'conversation/read':(('project','channel','conversation','request_id','text'),('attachment_ids',)),
        'business/read':(('project','action'),('query',)),
        'business/event':(('project','order_id','event_key'),()),
        'receipts/inspect':(('project',),('max_age',)),
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
    if route in {'conversation/export','conversation/select'}:
        from .conversation_gateway import ConversationGateway
        gateway=getattr(server,'conversation_gateway',None)
        registry.get(token,args['project'])
        if not isinstance(gateway,ConversationGateway) or gateway.store is not store:
            raise ValueError('registered conversation gateway required')
        from .agent_directory import _reject_known_tokens
        with store._transaction() as c:_reject_known_tokens(c,args)
        result=(gateway.export(token,**args) if route=='conversation/export' else gateway.select(token,**args))
        handler.respond(200,{'ok':True,'result':result})
        return True
    if route=='conversation/read':
        from .conversation_gateway import ConversationGateway, CHANNELS, CustomerAuthenticationUnavailable
        from taesan_core import Attachment
        project=args['project'];name(project)
        for field in ('channel','conversation','request_id'):name(args[field])
        if args['channel'] not in CHANNELS or type(args['text']) is not str or len(args['text'])>12000:
            raise ValueError('registered channel and bounded question required')
        if not args['text'].strip() and not args.get('attachment_ids'):raise ValueError('question or attachment required')
        config=registry.get(token,project)
        from .agent_directory import _reject_known_tokens
        with store._transaction() as c:_reject_known_tokens(c,args)
        gateway=getattr(server,'conversation_gateway',None)
        if gateway is None:
            result={'status':'unavailable','message':'대화 업무 연결이 아직 없습니다.','data':{},
                    'operational_change':False,'customer_write_enabled':False}
        else:
            if not isinstance(gateway,ConversationGateway) or gateway.store is not store:
                raise ValueError('registered conversation gateway required')
            try:gateway._auth(token,project,config['fingerprint'],channel=args['channel'],conversation=args['conversation'])
            except CustomerAuthenticationUnavailable:
                handler.respond(200,{'ok':True,'result':gateway._authentication_unavailable(project)})
                return True
            if gateway.authorize is None and not gateway.fixture_mode:
                handler.respond(200,{'ok':True,'result':{'status':'unavailable','message':'직원 인증 연결이 아직 없습니다.',
                    'data':{},'operational_change':False,'customer_write_enabled':False,'customer_identity_verified':False}})
                return True
            ids=args.pop('attachment_ids',None);attachments=()
            if ids is not None:
                if type(ids) is not list or not 1<=len(ids)<=5 or any(type(i) is not str for i in ids) or len(set(ids))!=len(ids):
                    raise ValueError('bounded opaque attachment references required')
                for value in ids:name(value)
                loader=getattr(server,'conversation_media_loader',None)
                if loader is None:
                    handler.respond(200,{'ok':True,'result':{'status':'unavailable','message':'사진·음성 수신 연결이 아직 없습니다.',
                        'data':{},'operational_change':False,'customer_write_enabled':False}})
                    return True
                if not callable(loader):raise ValueError('registered media loader required')
                failure=None
                try:attachments=loader(token,project,args['channel'],args['conversation'],tuple(ids))
                except PermissionError:failure='denied'
                except Exception:failure='unavailable'
                current=registry.get(token,project)
                if current['fingerprint']!=config['fingerprint']:
                    from .store import Conflict
                    raise Conflict('project changed during media retrieval')
                if failure:
                    handler.respond(200,{'ok':True,'result':{'status':failure,
                        'message':'사진·음성 접근 권한이 없습니다.' if failure=='denied' else '사진·음성 자료를 읽지 못했습니다.',
                        'data':{},'operational_change':False,'customer_write_enabled':False}})
                    return True
                if type(attachments) is not tuple or len(attachments)!=len(ids) or any(not isinstance(a,Attachment) for a in attachments):
                    raise ValueError('authenticated consented attachments required')
            result=gateway.handle(token,attachments=attachments,**args)
    elif route.startswith('business/'):
        from .project_adapters import ProjectAdapters
        service=ProjectAdapters(store,registry,getattr(server,'project_business_providers',None))
        result=service.read(token,**args) if route.endswith('/read') else service.sync_event(token,**args)
    elif route=='receipts/inspect':
        from .cloud_review_receipts import CloudReviewReceipts
        result=CloudReviewReceipts(store,registry,getattr(server,'cloud_review_probes',None)).inspect(token,**args)
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
