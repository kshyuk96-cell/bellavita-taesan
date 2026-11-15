"""Project-bound business reads and local event review projections.

Callbacks are trusted host configuration, never model-selected URLs/SQL. They
must use the source application's authenticated APIs; an owner/manager token
here does not confer a customer employee identity. No customer data is written.
"""
from dataclasses import dataclass
import datetime as dt
import json
import math
import re
from .maintenance_registry import name, receipt
from .agent_directory import _reject_known_tokens
from .store import Conflict, PermissionDenied, canonical_json, digest, _sha, _reject_credentials


ACTIONS = {
    'bellavita-erp': {'orders.list'},
    'bellavita-shop': {'orders.list', 'order.event'},
    'hope-sunrise': {'products.list', 'inquiries.list'},
    'funeral-association': {'access.status', 'obituary.get'},
}
SOURCE_COMMITS = {
    'bellavita-erp':'2c10b03db14c607fb4c61e09cbdf52ec7b406f40',
    'bellavita-shop':'d5047f3345b757edfcfbd81a73c609c0392007dd',
    'hope-sunrise':'d4da64b3791bb8dd7a96cf2f974c784385e79687',
    'funeral-association':'8f1e4fa331dbf460f0fcbea432c0fa360cc1b4ba',
}
SOURCE_KINDS = {p:('sites' if p in {'bellavita-erp','hope-sunrise'} else 'github') for p in ACTIONS}
ERP_STATES = {'접수','제작완료','배송완료','미확인'}
SHOP_STATES = {'requesting','waiting','failed','paid','partial_cancel','cancelled'}
STAGES = {'상담 접수','상담 중','견적 작성','견적 전달','예약 요청','종결'}


def _string(value, limit=150, *, empty=False):
    if type(value) is not str or len(value)>limit or '\x00' in value or (not empty and not value.strip()):
        raise ValueError('bounded source field required')
    return value


def _integer(value, low=0, high=10**12):
    if type(value) is not int or not low<=value<=high:raise ValueError('integer source field required')
    return value


def _date(value):
    _string(value,10)
    if dt.date.fromisoformat(value).isoformat()!=value:raise ValueError('ISO date required')
    return value


def _optional_text(row,key,limit=300):
    value=row.get(key)
    if value is None:return None
    return _string(value,limit,empty=True)


def _money(value):
    if value is None:return None
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=10**12:
        raise ValueError('source amount required')
    return value


@dataclass(frozen=True)
class ProviderResult:
    state:str
    reference:str
    observed_at:float
    payload:object=None
    source_commit:str|None=None

    def __post_init__(self):
        if self.state not in {'ok','unavailable','permission_denied','not_configured','ambiguous'}:
            raise ValueError('typed provider state required')
        receipt(self.reference)
        if self.source_commit is not None:_sha(self.source_commit)
        if type(self.observed_at) not in (int,float) or not math.isfinite(self.observed_at) or self.observed_at<=0:
            raise ValueError('provider observation time required')


@dataclass(frozen=True)
class ProjectProvider:
    project:str
    company:str
    source_ref:str
    branch:str
    source_commit:str
    read:object
    bound_subject:str|None=None
    read_only:bool=True

    def __post_init__(self):
        if self.project not in ACTIONS:raise ValueError('registered business project required')
        name(self.company)
        _sha(self.source_commit)
        if self.source_commit!=SOURCE_COMMITS[self.project]:raise ValueError('reviewed source commit required')
        _string(self.source_ref,200);_string(self.branch,128)
        if not callable(self.read) or self.read_only is not True:raise ValueError('registered read-only callback required')
        if self.bound_subject is not None:_string(self.bound_subject)


def _query(project,action,query):
    if type(query) is not dict:raise ValueError('exact query required')
    if action not in ACTIONS.get(project,set()):raise ValueError('unsupported project action')
    keys={'id','date','status','limit'} if action=='orders.list' else {'id','status','limit'} if action in {'products.list','inquiries.list'} else {'id'} if action=='obituary.get' else {'order_id','event_key'} if action=='order.event' else set()
    if not set(query)<=keys:raise ValueError('unsupported query field')
    if project=='bellavita-shop' and 'date' in query:raise ValueError('shop event date filter not connected')
    out=dict(query)
    for key in ('id','order_id','event_key'):
        if key in out:_string(out[key])
    if action=='obituary.get' and set(out)!={'id'}:raise ValueError('exact obituary required')
    if action=='order.event' and set(out)!={'order_id','event_key'}:raise ValueError('exact event required')
    if 'date' in out:_date(out['date'])
    if 'status' in out:
        allowed=ERP_STATES if project=='bellavita-erp' else SHOP_STATES if project=='bellavita-shop' else {'draft','published'} if action=='products.list' else STAGES
        if out['status'] not in allowed:raise ValueError('source status required')
    if 'limit' in out:_integer(out['limit'],1,100)
    if action.endswith('.list'):out.setdefault('limit',30)
    return out


def _records(payload,key):
    if type(payload) is not dict or key not in payload or type(payload[key]) is not list or len(payload[key])>1000:
        raise ValueError('bounded source collection required')
    rows=payload[key]
    if any(type(r) is not dict for r in rows):raise ValueError('source object required')
    return rows


def _erp(row):
    out={'id':_string(row.get('id')),'date':_date(row.get('date')),
         'status':row.get('status'),'revision':_integer(row.get('revision'),1),
         'stableUid':_optional_text(row,'stableUid',150),'sourceRow':row.get('sourceRow'),
         'time':_optional_text(row,'time',20),'recipient':_optional_text(row,'recipient'),
         'sender':_optional_text(row,'sender'),'address':_optional_text(row,'address',1000),
         'driver':_optional_text(row,'driver'),'amount':_money(row.get('amount')),'paid':_money(row.get('paid'))}
    if out['status'] not in ERP_STATES:raise ValueError('unknown ERP status')
    if out['sourceRow'] is not None:_integer(out['sourceRow'],1)
    return out


def _shop(row):
    out={'id':_string(row.get('id')),'state':row.get('state')}
    if out['state'] not in SHOP_STATES:raise ValueError('unknown payment state')
    detail=row.get('details')
    if type(detail) is str:
        if len(detail)>100000:raise ValueError('bounded details required')
        detail=json.loads(detail)
    if type(detail) is not dict:raise ValueError('order details required')
    out.update({k:_optional_text(detail,k,1000 if k=='venue' else 300) for k in ('event_time','sender','title','venue')})
    out['amount']=_money(detail.get('amount'))
    return out


def _product(row):
    status=row.get('status')
    if status not in {'draft','published'}:raise ValueError('unknown product status')
    return {'id':_string(row.get('id')),'title':_string(row.get('title'),140),'status':status,
            'location':_optional_text(row,'location',100),'category':_optional_text(row,'category',100),
            'price':_money(row.get('price')),'updated':_optional_text(row,'updated',60)}


def _inquiry(row):
    # Same legacy status fallback as Hope src/operations.mjs:stageOf.
    stage=row.get('stage')
    if stage is not None and stage not in STAGES:raise ValueError('unknown inquiry stage')
    if stage is None:
        if row.get('status') not in {'접수','상담 중','상담 완료'}:raise ValueError('unknown inquiry status')
        stage='상담 중' if row['status']=='상담 중' else '종결' if row['status']=='상담 완료' else '상담 접수'
    out={'id':_string(row.get('id')),'stage':stage,'productId':_string(row.get('productId')),
         'productTitle':_optional_text(row,'productTitle',140),'departureDate':_optional_text(row,'departureDate',10),
         'travelerCount':row.get('travelerCount'),'quote':_money(row.get('quote')),
         'assignee':_optional_text(row,'assignee',50),'due':_optional_text(row,'due',10)}
    if out['travelerCount'] is not None:_integer(out['travelerCount'],1,999)
    for key in ('departureDate','due'):
        if out[key]:_date(out[key])
    return out


class ProjectAdapters:
    def __init__(self,store,registry,providers=None):
        if registry.store is not store:raise ValueError('shared registry required')
        self.store,self.registry=store,registry
        self.providers=dict(providers or {})
        if any(not isinstance(p,ProjectProvider) or key!=p.project for key,p in self.providers.items()):
            raise ValueError('exact provider registration required')
        with store._transaction() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS manager_business_events (
                project TEXT,event_key TEXT,order_id TEXT,revision INTEGER,fingerprint TEXT,
                state TEXT,reference TEXT,observed REAL, PRIMARY KEY(project,order_id,event_key));
                CREATE TABLE IF NOT EXISTS manager_business_order_review (
                project TEXT,order_id TEXT,revision INTEGER,state TEXT,reference TEXT,updated REAL,
                PRIMARY KEY(project,order_id));''')

    def _configuration(self,token,project):
        # _get authenticates roles/project on every use, including after callback.
        with self.store._transaction() as c:return self.registry._get(c,token,project)

    def _observe(self,token,project,action,query):
        query=_query(project,action,query)
        config=self._configuration(token,project)
        with self.store._transaction() as c:
            _reject_credentials(query,token);_reject_known_tokens(c,query)
        provider=self.providers.get(project)
        if provider is None:return config,None,None
        expected=config['config']
        if expected['source_kind']!=SOURCE_KINDS[project] or any(getattr(provider,k)!=expected[k] for k in ('company','source_ref','branch')):
            raise Conflict('registered provider source changed')
        request={'project':project,'company':expected['company'],'action':action,'query':query}
        try:result=provider.read(json.loads(canonical_json(request)))
        except PermissionDenied:result=ProviderResult('permission_denied','provider/denied',self.store._now())
        except Exception:result=ProviderResult('unavailable','provider/unavailable',self.store._now())
        current=self._configuration(token,project)
        if current['fingerprint']!=config['fingerprint']:raise Conflict('project changed during observation')
        if not isinstance(result,ProviderResult):return config,provider,None
        try:
            if len(canonical_json(result.payload))>2000000:raise ValueError('bounded source response required')
            _reject_credentials(result.__dict__,token)
            with self.store._transaction() as c:_reject_known_tokens(c,result.__dict__)
        except (ValueError,TypeError):
            return config,provider,ProviderResult('ambiguous','provider/redacted',self.store._now())
        if result.state=='ok' and result.source_commit!=provider.source_commit:
            return config,provider,ProviderResult('ambiguous',result.reference,result.observed_at)
        age=self.store._now()-result.observed_at
        if age<0 or age>300:return config,provider,ProviderResult('ambiguous',result.reference,result.observed_at)
        return config,provider,result

    def read(self,token,project,action,query=None):
        if action=='order.event':raise ValueError('use sync_event for event review')
        query=_query(project,action,{} if query is None else query)
        config,provider,result=self._observe(token,project,action,query)
        base={'project':project,'company':config['config']['company'],'action':action,
              'state':'not_configured' if provider is None else 'ambiguous' if result is None else result.state,
              'count':None,'items':[],'operational_change':False,'source_content_trusted_as_instruction':False}
        if result is not None:base.update(reference=result.reference,observed_at=result.observed_at)
        if result is None or result.state!='ok':return base
        try:
            payload=result.payload
            if project=='funeral-association':
                rows=self._association(provider,payload,action,query)
            else:
                key='orders' if action=='orders.list' else 'products' if action=='products.list' else 'requests'
                normalize=_erp if project=='bellavita-erp' else _shop if project=='bellavita-shop' else _product if action=='products.list' else _inquiry
                rows=[normalize(row) for row in _records(payload,key)]
                if len({r['id'] for r in rows})!=len(rows):raise ValueError('ambiguous duplicate source identity')
                for field in ('id','date','status'):
                    if field not in query:continue
                    source_field='state' if project=='bellavita-shop' and field=='status' else 'stage' if action=='inquiries.list' and field=='status' else field
                    rows=[r for r in rows if r.get(source_field)==query[field]]
                total=len(rows);rows=rows[:query['limit']]
                base.update(total_matched=total,truncated=total>len(rows))
            base.update(items=rows,count=len(rows),state='ok' if rows else 'empty')
        except (ValueError,TypeError,KeyError):base.update(state='ambiguous',items=[],count=None)
        try:
            _reject_credentials(base,token)
            with self.store._transaction() as c:_reject_known_tokens(c,base)
        except ValueError:
            base.update(state='ambiguous',items=[],count=None,reference='provider/redacted')
        return base

    def _association(self,provider,payload,action,query):
        if type(payload) is not dict or type(payload.get('user')) is not dict or not provider.bound_subject:
            raise ValueError('bound customer identity required')
        user=payload['user']
        if user.get('id')!=provider.bound_subject or user.get('role') not in {'staff','mourner','expert'} or payload.get('active_session') is not True:
            raise ValueError('verified current customer session required')
        if action=='access.status':return [{'authenticated':True,'role':user['role']}]
        # A host callback must run the source authorize(user,id) function for this
        # exact principal/resource. Home match alone is insufficient in source.
        if user['role'] not in {'staff','mourner'} or payload.get('authorization_verified') is not True or payload.get('authorized_id')!=query['id']:
            raise ValueError('exact source authorization required')
        row=payload.get('obituary')
        if type(row) is not dict or row.get('id')!=query['id'] or type(row.get('home')) is not dict:
            raise ValueError('exact obituary result required')
        if user['role']=='staff' and user.get('home_id')!=row['home'].get('id'):
            raise ValueError('customer home mismatch')
        status=_string(row.get('status'),30)
        # Closed records deliberately exclude deceased/mourner details.
        if status=='closed':return [{'id':query['id'],'status':'closed'}]
        if status!='active':raise ValueError('unknown obituary state')
        return [{'id':query['id'],'status':status,'version':_integer(row.get('version'),1),
                 'roomName':_optional_text(row,'roomName',100),'departureAt':_optional_text(row,'departureAt',60)}]

    def sync_event(self,token,project,order_id,event_key):
        """Observe a source-verified event+snapshot; record local review only.

        revision is a source-side monotonic snapshot revision supplied by a future
        adapter. It is not an existing PayApp revision or authority to refund.
        """
        if project!='bellavita-shop':raise ValueError('shop event project required')
        query=_query(project,'order.event',{'order_id':order_id,'event_key':event_key})
        config,provider,result=self._observe(token,project,'order.event',query)
        output={'project':project,'state':'not_configured' if provider is None else 'ambiguous' if result is None else result.state,
                'operational_change':False,'payment_executed':False,'customer_order_updated':False}
        if result is None or result.state!='ok':return output
        try:
            event=result.payload
            if type(event) is not dict or set(event)!={'order_id','event_key','revision','state','source_verified'}:
                raise ValueError('exact event snapshot required')
            if event['order_id']!=order_id or event['event_key']!=event_key or event['source_verified'] is not True:
                raise ValueError('verified source event required')
            revision=_integer(event['revision'],1);state=event['state']
            if state not in SHOP_STATES:raise ValueError('known source state required')
            fingerprint=digest(event)
        except (ValueError,TypeError):return {**output,'state':'ambiguous'}
        with self.store._transaction() as c:
            current=self.registry._get(c,token,project)
            if current['fingerprint']!=config['fingerprint']:raise Conflict('project changed before event recording')
            old=c.execute('SELECT * FROM manager_business_events WHERE project=? AND order_id=? AND event_key=?',(project,order_id,event_key)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise Conflict('event reference reused with changed snapshot')
                return {**output,'state':'duplicate','revision':revision,'projection_changed':False}
            last=c.execute('SELECT * FROM manager_business_order_review WHERE project=? AND order_id=?',(project,order_id)).fetchone()
            result_state='recorded'
            if last and revision<=last['revision']:result_state='stale' if revision<last['revision'] else 'needs_reconciliation'
            elif last and last['state'] in {'cancelled','partial_cancel'} and state in {'paid','waiting','requesting'}:
                result_state='needs_reconciliation'
            c.execute('INSERT INTO manager_business_events VALUES (?,?,?,?,?,?,?,?)',(project,event_key,order_id,revision,fingerprint,state,result.reference,result.observed_at))
            if result_state=='recorded':
                c.execute('INSERT OR REPLACE INTO manager_business_order_review VALUES (?,?,?,?,?,?)',(project,order_id,revision,state,result.reference,result.observed_at))
            return {**output,'state':result_state,'revision':revision,'projection_changed':result_state=='recorded','reference':result.reference}
