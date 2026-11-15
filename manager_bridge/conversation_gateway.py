"""Fresh-authenticated read conversation integration over the reused business core.

No operation/mutation tool is registered. Text/media are transient planner input;
only digests and bounded structured replies are retained in private state. Model
and media providers are host configuration, never supplied by an HTTP request.
"""
from dataclasses import asdict
import json
import hashlib
import re
import base64
from .agent_directory import _reject_known_tokens
from .maintenance_registry import name
from .store import Conflict, PermissionDenied, NotFound, canonical_json, digest, _reject_credentials
from taesan_core import Core, CoreError, Attachment, Reply, Plan, Store as CoreStore
from taesan_core.adapters import Media
from .conversation_tools import build_registry, principal_for_token
from .conversation_planner import Planner
from .conversation_media import prepare_media, media_gate
from .conversation_reply import render_reply
from .conversation_results import ResultHistory
from .conversation_followups import FollowupScope, FollowupMemory, image_candidates, voice_number_clarification, voice_query
from .conversation_batch import propose_batch, summarize_batch

_EXPORT = re.compile(r'^(?:이거|이 결과|조회 결과|현재 결과)?\s*(?:를|을)?\s*(?:엑셀|xlsx)(?:로)?\s*(?:줘|주세요|내보내줘|다운로드|만들어줘)?[.!?\s]*$',re.I)
_COMPARE = re.compile(r'^(?:아까|이전|직전)(?:랑|와|과)?\s*(?:뭐가|무엇이|어떻게)?\s*(?:달라졌어|달라졌나요|바뀌었어|변경됐어|비교해줘)[.!?\s]*$')
_PHOTO_FOLLOWUP = re.compile(r'^(?:(?:이|그|사진의|사진 속)\s*(?:주문|번호|사진)(?:을|를|는)?\s*)?(?:조회|확인|보여)(?:해줘|해주세요|줘|주세요)?[.!?\s]*$')

class CustomerAuthenticationUnavailable(RuntimeError):
    pass


CHANNELS=frozenset({'erp','shop','kakao','manager'})


class ConversationGateway:
    def __init__(self,store,registry,adapters,core_store,*,planner=None,media=None,authorize=None,fixture_mode=False):
        if registry.store is not store or adapters.store is not store or not isinstance(core_store,CoreStore):
            raise ValueError('shared registered manager and private core state required')
        if planner is not None and not isinstance(planner,Planner):raise ValueError('registered planner required')
        if media is not None and not isinstance(media,Media):raise ValueError('registered media required')
        if authorize is not None and not callable(authorize):raise ValueError('registered current customer authorization required')
        if type(fixture_mode) is not bool:raise ValueError('explicit fixture mode required')
        self.authorize,self.fixture_mode=authorize,fixture_mode
        self.store,self.registry,self.adapters,self.core_store=store,registry,adapters,core_store
        self.planner=planner if planner is not None else Planner(clock=store.clock)
        self.media=media
        self.history=ResultHistory(store)
        self.followups=FollowupMemory(store)
        with store._transaction() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS manager_conversation_requests (
                actor TEXT,project TEXT,channel TEXT,request_key TEXT,conversation TEXT,
                fingerprint TEXT,config_fingerprint TEXT,state TEXT,response TEXT,
                created REAL,expires REAL,PRIMARY KEY(actor,project,channel,request_key));
                CREATE TABLE IF NOT EXISTS manager_conversation_locks (
                actor TEXT,project TEXT,channel TEXT,conversation TEXT,request_key TEXT,
                PRIMARY KEY(actor,project,channel,conversation));''')

    def _auth(self,token,project,expected=None,*,channel,conversation):
        config=self.registry.get(token,project)
        principal=principal_for_token(self.store,self.registry,token,project)
        if expected is not None and config['fingerprint']!=expected:
            raise Conflict('conversation project configuration changed')
        if self.authorize is not None:
            try:allowed=self.authorize(token,project,channel,conversation)
            except PermissionError:raise PermissionDenied('customer access denied') from None
            except Exception:raise CustomerAuthenticationUnavailable('customer authentication unavailable') from None
            if allowed is not True:raise PermissionDenied('current customer conversation access required')
            current=self.registry.get(token,project)
            fresh=principal_for_token(self.store,self.registry,token,project)
            if current['fingerprint']!=config['fingerprint'] or fresh!=principal:
                raise Conflict('conversation authority changed during authentication')
        return config,principal

    def _context(self,principal,channel,conversation):
        scope=Core.scope(principal,channel,conversation)
        with self.core_store.db() as c:
            row=c.execute('SELECT state,expires FROM turns WHERE tenant=? AND subject=? AND channel=? AND conversation=?',scope).fetchone()
            if row is None or row['expires']<=self.core_store.now():return None
            state=json.loads(row['state'])
            if type(state) is not dict or not {'tool','arguments'}<=state.keys():return None
            return {'tool':state['tool'],'arguments':state['arguments']}

    def _clear_context(self,principal,channel,conversation):
        scope=Core.scope(principal,channel,conversation)
        with self.core_store.db() as c:
            c.execute('DELETE FROM turns WHERE tenant=? AND subject=? AND channel=? AND conversation=?',scope)

    def _reply(self,reply,project,*,source=None,context_used=False):
        return {'status':reply.status,'message':render_reply(reply,project),'data':reply.data,
                'planner_source':source,'context_used':context_used,'operational_change':False,
                'customer_write_enabled':False,'raw_input_retained':False,
                'development_fixture':self.fixture_mode,'customer_identity_verified':self.authorize is not None}

    def _authentication_unavailable(self,project):
        result=self._reply(Reply('unavailable','직원 인증 상태를 확인하지 못했습니다.'),project)
        result['customer_identity_verified']=False
        return result

    def _abort(self,key,scope):
        with self.store._transaction() as c:
            c.execute("UPDATE manager_conversation_requests SET state='uncertain',response=NULL WHERE actor=? AND project=? AND channel=? AND request_key=?",key)
            c.execute('DELETE FROM manager_conversation_locks WHERE actor=? AND project=? AND channel=? AND conversation=? AND request_key=?',(*scope,key[-1]))

    def _result_scope(self,principal,project,channel,conversation,config):
        return (principal.tenant,principal.subject,project,channel,conversation,config['fingerprint'])

    def _followup_scope(self,principal,project,channel,conversation,config):
        return FollowupScope(principal.subject,project,channel,conversation,config['fingerprint'])

    def export(self,token,project,channel,conversation,snapshot_id):
        if channel not in CHANNELS:raise ValueError('registered channel required')
        name(project);name(conversation)
        config,principal=self._auth(token,project,channel=channel,conversation=conversation)
        if self.authorize is None and not self.fixture_mode:raise PermissionDenied('customer authentication required')
        scope=self._result_scope(principal,project,channel,conversation,config)
        def authorize():
            self._auth(token,project,config['fingerprint'],channel=channel,conversation=conversation)
            return True
        content=self.history.xlsx(scope,snapshot_id,authorize)
        return {'filename':'taesan-read-results.xlsx','content_type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'content_base64':base64.b64encode(content).decode('ascii'),'snapshot_id':snapshot_id,
                'operational_change':False,'customer_write_enabled':False}

    def select(self,token,project,channel,conversation,request_id,selection_id,generation):
        # A selector is a read action, never an approval. Refetch the exact source
        # window before resolving it, then execute an ordinary authenticated read.
        if channel not in CHANNELS:raise ValueError('registered channel required')
        for value in (project,conversation,request_id):name(value)
        config,principal=self._auth(token,project,channel=channel,conversation=conversation)
        if self.authorize is None and not self.fixture_mode:raise PermissionDenied('customer authentication required')
        scope=self._result_scope(principal,project,channel,conversation,config)
        snapshot=self.history.current(scope)
        if snapshot['id']!=generation:raise Conflict('selection generation changed')
        follow=self._followup_scope(principal,project,channel,conversation,config)
        report=self.adapters.read(token,project,snapshot['action'],snapshot['query'])
        self._auth(token,project,config['fingerprint'],channel=channel,conversation=conversation)
        if report.get('state')!='ok':raise Conflict('current selection source unavailable')
        plan=self.followups.choose(follow,selection_id,generation,report['items'])
        # Current query's filters remain explicit; no old state is silently inherited.
        args=dict(snapshot['query']);args['id']=plan['arguments']['id']
        selected=Plan(plan['tool'],args,False)
        return self.handle(token,project,channel,conversation,request_id,'선택한 자료 조회',_selected_plan=selected,
                           _selection_generation=generation,_selected_identity=digest(next(i for i in report['items'] if i['id']==args['id'])))

    def _run_read(self,token,project,channel,conversation,core_conversation,core_request,text,media_context,config,principal,selected_plan=None):
        context=self._context(principal,channel,core_conversation)
        if re.search(r'(?:취소|등록|삭제|발송|입고|환불|결제|수정|변경)(?:하고|한\s*뒤|\s*후)',text):
            self._clear_context(principal,channel,core_conversation)
            return Reply('unsupported','이 대화 연결에서는 조회만 가능합니다. 변경은 별도 검토가 필요합니다.'),'rules',False
        from .conversation_planner import PlannerResult
        proposed=(PlannerResult('planned',selected_plan,'조회 계획을 확인했습니다.','rules') if selected_plan is not None
                  else self.planner.propose(project,text,media=media_context,context=context))
        self._auth(token,project,config['fingerprint'],channel=channel,conversation=conversation)
        if proposed.state!='planned':
            if proposed.draft is not None:
                tools=build_registry(self.adapters,token,project,config['fingerprint'])
                tool=tools.get(proposed.draft.tool)
                if tool is None or tool.kind!='read':raise PermissionDenied('registered read draft required')
                reply=Core(self.core_store,tools).handle(principal,channel,core_conversation,core_request,plan=proposed.draft)
            else:
                self._clear_context(principal,channel,core_conversation)
                reply=Reply('clarify' if proposed.state=='clarify' else 'unavailable' if proposed.state in ('unavailable','failed') else 'unsupported',proposed.message)
            return reply,proposed.source,False
        tools=build_registry(self.adapters,token,project,config['fingerprint'])
        tool=tools.get(proposed.plan.tool)
        if tool is None or tool.kind!='read':raise PermissionDenied('conversation read tool required')
        used=context is not None and context['tool']==proposed.plan.tool and (proposed.plan.follow_up or '다시' in text or '빼' in text or '전체 상태' in text)
        reply=Core(self.core_store,tools).handle(principal,channel,core_conversation,core_request,plan=proposed.plan)
        return reply,proposed.source,used

    def handle(self,token,project,channel,conversation,request_id,text,*,attachments=(),_selected_plan=None,_selection_generation=None,_selected_identity=None):
        name(project);name(channel);name(conversation);name(request_id)
        if _selected_plan is not None and not isinstance(_selected_plan,Plan):raise ValueError('typed host read selection required')
        if channel not in CHANNELS:raise ValueError('registered conversation channel required')
        if type(text) is not str or len(text)>12000 or (not text.strip() and not attachments):
            raise ValueError('bounded question or consented attachment required')
        if type(attachments) is not tuple or len(attachments)>5 or any(not isinstance(a,Attachment) for a in attachments):
            raise ValueError('validated consented attachments required')
        _reject_credentials({'project':project,'channel':channel,'conversation':conversation,'request_id':request_id,'text':text},token)
        try:config,principal=self._auth(token,project,channel=channel,conversation=conversation)
        except CustomerAuthenticationUnavailable:
            return self._authentication_unavailable(project)
        if self.authorize is None and not self.fixture_mode:
            return self._reply(Reply('unavailable','직원 인증 연결이 없습니다.',{'result':{'provider_state':'not_configured'}}),project)
        self.core_store.purge()
        fingerprint=digest({'project':project,'channel':channel,'conversation':conversation,'text_digest':digest(text),
            'attachments':[{'kind':a.kind,'digest':hashlib.sha256(a.content).hexdigest()} for a in attachments],
            'selected_plan':asdict(_selected_plan) if _selected_plan else None,'selection_generation':_selection_generation,'selected_identity':_selected_identity,'config':config['fingerprint']})
        core_conversation=digest({'project':project,'channel':channel,'conversation':conversation,'config':config['fingerprint']})
        core_request=digest({'project':project,'channel':channel,'conversation':conversation,'request':request_id,'config':config['fingerprint']})
        key=(principal.subject,project,channel,request_id);scope=(principal.subject,project,channel,conversation)
        with self.store._transaction() as c:
            current=self.registry._get(c,token,project)
            if current['fingerprint']!=config['fingerprint']:raise Conflict('conversation configuration changed')
            _reject_known_tokens(c,{'conversation':conversation,'request':request_id,'text':text})
            now=self.store._now()
            c.execute('UPDATE manager_conversation_requests SET response=NULL WHERE expires<=?',(now,))
            old=c.execute('SELECT * FROM manager_conversation_requests WHERE actor=? AND project=? AND channel=? AND request_key=?',key).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise Conflict('conversation request key changed')
                if old['state']=='done' and old['expires']<=now:
                    return self._reply(Reply('expired','이전 결과 보관 기간이 지났습니다. 새 요청으로 조회해 주십시오.'),project)
                if old['state']=='done' and old['response'] is not None:
                    cached=json.loads(old['response'])
                    exported=cached.get('data',{}).get('export')
                    if type(exported) is dict:
                        # Never replay retained XLSX bytes beyond snapshot expiry.
                        snapshot=c.execute('SELECT expires FROM manager_conversation_snapshots WHERE id=?',(exported.get('snapshot_id'),)).fetchone()
                        if snapshot is None or snapshot['expires']<=now:return self._reply(Reply('expired',''),project)
                    return cached
                return self._reply(Reply('uncertain','이전 요청의 결과를 대조해야 합니다. 자동 재실행하지 않습니다.'),project)
            if c.execute('SELECT 1 FROM manager_conversation_locks WHERE actor=? AND project=? AND channel=? AND conversation=?',scope).fetchone():
                return self._reply(Reply('busy','앞선 요청을 확인 중입니다.'),project)
            c.execute('INSERT INTO manager_conversation_requests VALUES (?,?,?,?,?,?,?,\'running\',NULL,?,?)',
                (*key,conversation,fingerprint,config['fingerprint'],now,now+self.core_store.ttl))
            c.execute('INSERT INTO manager_conversation_locks VALUES (?,?,?,?,?)',(*scope,request_id))
        try:
            if _selection_generation is not None and self.history.current(self._result_scope(principal,project,channel,conversation,config))['id']!=_selection_generation:
                raise Conflict('selection generation changed during request')
            media_context=prepare_media(principal,attachments,text,media=self.media)
            self._auth(token,project,config['fingerprint'],channel=channel,conversation=conversation)
            blocked=media_gate(media_context)
            follow=self._followup_scope(principal,project,channel,conversation,config)
            result_scope=self._result_scope(principal,project,channel,conversation,config)
            source=None;used=False;response=None;comparison=False
            if blocked is not None:
                self._clear_context(principal,channel,core_conversation)
                self.followups.clear(follow)
                reply=blocked
            elif _EXPORT.fullmatch(text.strip()) and not attachments:
                try:
                    snapshot=self.history.current(result_scope)
                    export=self.export(token,project,channel,conversation,snapshot['id'])
                    response=self._reply(Reply('ok','',{}),project)
                    response.update(message='현재 조회 결과를 엑셀로 만들었습니다. 전체 원장 내보내기는 아닙니다.',data={'export':export})
                except NotFound:
                    response=self._reply(Reply('clarify',''),project)
                    response['message']='엑셀로 만들 최근 조회 결과가 없습니다. 먼저 조회해 주십시오.'
            else:
                voice=voice_number_clarification(media_context)
                if attachments:
                    self.followups.clear(follow)
                    self.history.clear(result_scope)
                candidates=image_candidates(media_context)
                if candidates:
                    receipts=[o.extraction.evidence.receipt for o in media_context.observations if o.kind=='image' and o.status=='ok']
                    self.followups.remember_photo(follow,candidates,digest(receipts))
                if voice is not None:
                    voice_plan=voice_query(media_context,project,self.planner,self._context(principal,channel,core_conversation))
                    if voice_plan is not None:self.followups.remember_voice(follow,voice_plan.tool,voice_plan.arguments)
                    self._clear_context(principal,channel,core_conversation)
                    reply=voice
                    response=self._reply(reply,project)
                    response['message']='주문번호만 다시 알려주십시오.'
                else:
                    effective=text
                    voice_pending=self.followups.voice(follow) if not attachments else None
                    number=re.fullmatch(r'(?:주문\s*(?:번호)?\s*)?(\d{1,9})(?:번)?[.!?\s]*',text.strip())
                    selected_plan=_selected_plan
                    if voice_pending and number:
                        selected_plan=Plan(voice_pending['tool'],dict(voice_pending['arguments'],id=number.group(1)),False)
                    pending=self.followups.photo(follow) if not attachments else None
                    if pending and _PHOTO_FOLLOWUP.fullmatch(text.strip()):
                        if len(pending['ids'])!=1:
                            response=self._reply(Reply('clarify',''),project)
                            response['message']='조회할 주문번호만 알려주십시오.'
                        elif project in {'bellavita-erp','bellavita-shop'}:
                            effective='주문번호 '+pending['ids'][0]+' 조회'
                            used=True
                    elif not attachments:
                        self.followups.clear(follow)
                    batch=propose_batch(effective) if effective.strip() and not attachments else None
                    if media_context is not None:
                        audio=[o.extraction.text for o in media_context.observations if o.kind=='audio' and o.status=='ok']
                        if len(audio)==1 and re.search(r'(?:취소|등록|삭제|발송|입고|환불|결제|수정|변경)(?:하고|한\s*뒤|\s*후)',audio[0]):
                            response=self._reply(Reply('unsupported','이 대화 연결에서는 조회만 가능합니다. 변경은 별도 검토가 필요합니다.'),project)
                        elif len(audio)==1 and propose_batch(audio[0]).state!='single':
                            response=self._reply(Reply('clarify',''),project)
                            response['message']='음성 업무를 하나씩 알려주십시오. 여러 업무는 글로 나누어 요청할 수 있습니다.'
                    if response is None and batch is not None and batch.state=='clarify':
                        self._clear_context(principal,channel,core_conversation)
                        response=self._reply(Reply('clarify',''),project);response['message']=batch.message
                    elif response is None and batch is not None and batch.state=='batch':
                        children=[]
                        self.followups.clear(follow)
                        self.history.clear(result_scope)
                        for index,task in enumerate(batch.tasks):
                            # Independent clauses never inherit earlier task filters.
                            self._clear_context(principal,channel,core_conversation)
                            self._auth(token,project,config['fingerprint'],channel=channel,conversation=conversation)
                            if task.kind=='unsupported':
                                child=self._reply(Reply('unsupported','이 대화 연결에서는 조회만 가능합니다. 변경은 별도 검토가 필요합니다.'),project)
                            else:
                                value,child_source,child_used=self._run_read(token,project,channel,conversation,core_conversation,digest({'request':core_request,'task':index}),task.text,None,config,principal)
                                child=self._reply(value,project,source=child_source,context_used=child_used)
                            children.append(child)
                        self._clear_context(principal,channel,core_conversation)
                        response=summarize_batch(children)
                        response.update(planner_source='rules',context_used=False,development_fixture=self.fixture_mode,customer_identity_verified=self.authorize is not None)
                    elif response is None:
                        comparison=bool(_COMPARE.fullmatch(effective.strip())) and not attachments
                        if comparison:
                            effective='아까 조건으로 다시 조회해줘'
                        reply,source,read_used=self._run_read(token,project,channel,conversation,core_conversation,core_request,effective,media_context,config,principal,selected_plan)
                        used=used or read_used
                        if _selected_identity is not None and reply.status in {'ok','empty'}:
                            selected_items=reply.data.get('result',{}).get('items',[])
                            if len(selected_items)!=1 or digest(selected_items[0])!=_selected_identity:
                                self._clear_context(principal,channel,core_conversation)
                                raise Conflict('selected source changed during read')
            self._auth(token,project,config['fingerprint'],channel=channel,conversation=conversation)
            if response is None:
                response=self._reply(reply,project,source=source,context_used=used)
                if reply.status not in {'ok','empty'} and not image_candidates(media_context) and voice_number_clarification(media_context) is None:
                    self.history.clear(result_scope)
                result=reply.data.get('result') if type(reply.data) is dict else None
                if reply.status in {'ok','empty'} and type(result) is dict and result.get('action')=='access.status':
                    self.history.clear(result_scope)
                    self.followups.clear(follow)
                if reply.status in {'ok','empty'} and type(result) is dict and result.get('action')!='access.status':
                    try:snapshot_id=self.history.remember(result_scope,result)
                    except ValueError:
                        snapshot_id=None
                        self.history.clear(result_scope)
                        self.followups.clear(follow)
                    if snapshot_id is not None:response['snapshot_id']=snapshot_id
                    items=result.get('items',[])
                    tools_by_action={'orders.list':'erp.orders' if project=='bellavita-erp' else 'shop.orders','products.list':'hope.products','inquiries.list':'hope.inquiries'}
                    if snapshot_id is not None and 2<=len(items)<=20 and result.get('action') in tools_by_action and all(type(i.get('id')) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',i['id']) for i in items):
                        try:response['choices']=self.followups.offer(follow,tools_by_action[result['action']],items,snapshot_id)
                        except ValueError:pass
                    if comparison and snapshot_id is not None:
                        try:
                            diff=self.history.compare(result_scope)
                            response['data']['comparison']=diff
                            response['message']=f"이전·현재 표시 결과 비교: 새로 표시 {len(diff['appeared'])}건, 현재 표시에서 빠짐 {len(diff['absent_from_current_display'])}건, 내용 변경 {len(diff['changed'])}건입니다. 원장 삭제를 뜻하지 않습니다."
                        except (NotFound,Conflict):
                            response['status']='clarify';response['message']='같은 조건의 이전 조회가 없어 비교할 수 없습니다. 현재 결과를 확인해 주십시오.'
            _reject_credentials(response,token)
            with self.store._transaction() as c:
                current=self.registry._get(c,token,project)
                if current['fingerprint']!=config['fingerprint']:raise Conflict('project changed before reply')
                _reject_known_tokens(c,response)
                c.execute("UPDATE manager_conversation_requests SET state='done',response=? WHERE actor=? AND project=? AND channel=? AND request_key=?",(canonical_json(response),*key))
                c.execute('DELETE FROM manager_conversation_locks WHERE actor=? AND project=? AND channel=? AND conversation=? AND request_key=?',(*scope,request_id))
            return response
        except CustomerAuthenticationUnavailable:
            self._abort(key,scope)
            return self._authentication_unavailable(project)
        except (PermissionDenied,NotFound,Conflict):
            self._abort(key,scope)
            raise
        except Exception:
            self._abort(key,scope)
            return self._reply(Reply('uncertain','처리 결과 확인이 필요합니다. 자동 재실행하지 않습니다.'),project)
