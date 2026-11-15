"""Bounded read-only followups. Hosts must freshly authorize before every call.

Opaque selections are never approvals. Only parsed order identifiers are kept
from a photo, never bytes, URLs, OCR text, or accompanying questions.
"""
from dataclasses import dataclass
import json
import re
import secrets
from .store import digest, canonical_json, Conflict
from taesan_core import Reply, Plan
from taesan_core.adapters import MediaContext

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_ORDER = re.compile(r'(?:주문\s*(?:번호)?\s*[:：]?\s*|(?<!\d))(\d{1,9})\s*번')


@dataclass(frozen=True)
class FollowupScope:
    actor: str
    project: str
    channel: str
    conversation: str
    config: str

    def key(self):
        if any(type(v) is not str or not v or len(v)>256 for v in self.__dict__.values()):
            raise ValueError('bounded authenticated scope required')
        return digest(self.__dict__)


def _ids(values):
    if type(values) not in (tuple,list) or not 1<=len(values)<=20:
        raise ValueError('bounded candidate identifiers required')
    if any(type(v) is not str or not _ID.fullmatch(v) for v in values):
        raise ValueError('validated identifiers required')
    return list(dict.fromkeys(values))


def image_candidates(context):
    """Extract only explicit numeric order references; no implied instructions."""
    if not isinstance(context,MediaContext):return ()
    values=[]
    for observation in context.observations:
        if observation.kind=='image' and observation.status=='ok':
            values.extend(_ORDER.findall(observation.extraction.text))
    return tuple(dict.fromkeys(values))[:20]


def voice_number_clarification(context):
    """Ask only for the number when a transcript explicitly leaves it uncertain.

    This is a conservative text check, not a confidence score or STT evaluation.
    """
    if not isinstance(context,MediaContext):return None
    for observation in context.observations:
        if observation.kind!='audio' or observation.status!='ok':continue
        text=observation.extraction.text
        if not re.search(r'주문|번호|\d+\s*번',text):continue
        numbers=set(_ORDER.findall(text))
        ambiguous=(len(numbers)>1 or bool(re.search(r'인가|인지|아니면|또는|불명확|잘 안|확실하지|[?？]',text)))
        if ambiguous:
            return Reply('clarify','주문번호만 다시 알려주십시오.',
                         {'missing':['id'],'execution_started':False,'confirmation_granted':False})
    return None


def _identity(item):
    if type(item) is not dict or type(item.get('id')) is not str or not _ID.fullmatch(item['id']):
        raise ValueError('registered result identifier required')
    # Full result digest also catches changed content when a row number is reused.
    return digest(item)


class FollowupMemory:
    def __init__(self,store,*,ttl=300):
        if type(ttl) is not int or not 1<=ttl<=600:raise ValueError('bounded followup lifetime required')
        self.store,self.ttl=store,ttl
        with store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS manager_read_followups (scope TEXT,kind TEXT,value TEXT,expires REAL,PRIMARY KEY(scope,kind))')

    def _put(self,scope,kind,value):
        with self.store._transaction() as c:
            c.execute('DELETE FROM manager_read_followups WHERE expires<=?',(self.store.clock(),))
            c.execute('INSERT OR REPLACE INTO manager_read_followups VALUES (?,?,?,?)',
                      (scope.key(),kind,canonical_json(value),self.store.clock()+self.ttl))

    def _get(self,scope,kind):
        with self.store._transaction() as c:
            c.execute('DELETE FROM manager_read_followups WHERE expires<=?',(self.store.clock(),))
            row=c.execute('SELECT value FROM manager_read_followups WHERE scope=? AND kind=?',(scope.key(),kind)).fetchone()
            return json.loads(row['value']) if row else None

    def clear(self,scope):
        with self.store._transaction() as c:c.execute('DELETE FROM manager_read_followups WHERE scope=?',(scope.key(),))

    def remember_photo(self,scope,ids,receipt):
        candidates=_ids(ids)
        if type(receipt) is not str or not receipt or len(receipt)>512:raise ValueError('media receipt required')
        self._put(scope,'photo',{'ids':candidates,'receipt_digest':digest(receipt)})

    def remember_voice(self,scope,tool,args):
        """Retain only validated query conditions while asking for an order ID."""
        from .conversation_planner import _args
        if type(args) is not dict or 'id' in args or not args.keys()<={'date','status'}:
            raise ValueError('only date and status may await a voice number')
        validated=_args(scope.project,tool,args)
        self._put(scope,'voice',{'tool':tool,'arguments':validated})

    def voice(self,scope):
        return self._get(scope,'voice')

    def photo(self,scope):
        return self._get(scope,'photo')

    def offer(self,scope,tool,items,generation):
        if tool not in {'erp.orders','shop.orders','hope.products','hope.inquiries','association.obituary'}:raise ValueError('registered read tool required')
        if type(generation) is not str or not generation or len(generation)>128:raise ValueError('result generation required')
        if type(items) not in (tuple,list) or not 1<=len(items)<=20:raise ValueError('bounded selection required')
        ids=_ids([i.get('id') if type(i) is dict else None for i in items])
        if len(ids)!=len(items):raise ValueError('ambiguous repeated identifiers')
        records=[{'token':secrets.token_hex(24),'id':item['id'],'identity':_identity(item)} for item in items]
        self._put(scope,'selection',{'tool':tool,'generation':generation,'records':records})
        label={'hope.products':'상품','hope.inquiries':'문의','association.obituary':'부고'}.get(tool,'주문')
        return [{'label':f"{label} {r['id']}",'selection_id':r['token']} for r in records]

    def choose(self,scope,token,generation,current_items):
        """Return a read query only after caller re-fetches the current result.

        A changed generation or content invalidates every old button. This does
        not perform customer authorization; the gateway must do that first.
        """
        if type(token) is not str or not re.fullmatch(r'[a-f0-9]{48}',token):raise Conflict('selection unavailable')
        saved=self._get(scope,'selection')
        if not saved or saved['generation']!=generation:raise Conflict('selection expired or changed')
        if type(current_items) not in (list,tuple) or len(current_items)>100:raise Conflict('current result required')
        selected=next((r for r in saved['records'] if secrets.compare_digest(r['token'],token)),None)
        if selected is None:raise Conflict('selection unavailable')
        matches=[item for item in current_items if type(item) is dict and item.get('id')==selected['id']]
        if len(matches)!=1 or _identity(matches[0])!=selected['identity']:raise Conflict('selected order changed; query again')
        return {'tool':saved['tool'],'arguments':{'id':selected['id']},'follow_up':False}


def voice_query(context,project,planner,contextquery=None):
    """Recover explicit read conditions independently of an uncertain number.

    Uses the existing deterministic planner rather than a model's confidence or
    inferred conditions. No transcript or untrusted instructions are persisted.
    The host retains the resulting date/status only until a fresh number arrives.
    """
    from .conversation_planner import Planner, _args, _MUTATION, _ACK
    if not isinstance(context,MediaContext) or not isinstance(planner,Planner):return None
    audio=[o.extraction.text for o in context.observations if o.kind=='audio' and o.status=='ok']
    if len(audio)!=1:return None
    text=audio[0]
    from .conversation_batch import contains_mutation
    if contains_mutation(text) or _MUTATION.search(text) or _ACK.fullmatch(text.strip()) or re.search(r'승인|허락|confirm|approve',text,re.I):return None
    # Remove order references and uncertainty markers without changing dates or states.
    text=re.sub(r'(?:주문\s*(?:번호)?\s*[:：]?\s*)?\d{1,12}\s*번(?:인가|인지|일까)?', ' ', text)
    text=re.sub(r'주문\s*번호\s*[:：]?\s*[가-힣\d ]+?(?=불명확|잘 안|확실하지|조회|보여|찾아|$)', '주문 ', text)
    text=re.sub(r'아니면|또는|인가|인지|불명확|확실하지\s*않[은다]?|잘\s*안\s*들[린려]\w*|[?？]', ' ',text)
    text=' '.join(text.split())
    previous=None
    if contextquery is not None:
        try:
            if type(contextquery) is not dict or set(contextquery)!={'tool','arguments'}:return None
            args=_args(project,contextquery['tool'],contextquery['arguments'])
            previous={'tool':contextquery['tool'],'arguments':{k:v for k,v in args.items() if k in {'date','status'}}}
        except (ValueError,TypeError,KeyError):return None
    try:
        result=Planner(clock=planner.clock).propose(project,text,context=previous)
        if result.state!='planned':return None
        args={}
        if result.plan.follow_up and previous and previous['tool']==result.plan.tool:args.update(previous['arguments'])
        args.update({k:v for k,v in result.plan.arguments.items() if k in {'date','status'}})
        return Plan(result.plan.tool,_args(project,result.plan.tool,args),False)
    except (ValueError,TypeError,KeyError):return None
