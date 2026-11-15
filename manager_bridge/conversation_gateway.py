"""Fresh-authenticated read conversation integration over the reused business core.

No operation/mutation tool is registered. Text/media are transient planner input;
only digests and bounded structured replies are retained in private state. Model
and media providers are host configuration, never supplied by an HTTP request.
"""
from dataclasses import asdict
import json
import hashlib
from .agent_directory import _reject_known_tokens
from .maintenance_registry import name
from .store import Conflict, PermissionDenied, NotFound, canonical_json, digest, _reject_credentials
from taesan_core import Core, CoreError, Attachment, Reply, Store as CoreStore
from taesan_core.adapters import Media
from .conversation_tools import build_registry, principal_for_token
from .conversation_planner import Planner
from .conversation_media import prepare_media, media_gate
from .conversation_reply import render_reply

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

    def handle(self,token,project,channel,conversation,request_id,text,*,attachments=()):
        name(project);name(channel);name(conversation);name(request_id)
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
            'config':config['fingerprint']})
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
                    return json.loads(old['response'])
                return self._reply(Reply('uncertain','이전 요청의 결과를 대조해야 합니다. 자동 재실행하지 않습니다.'),project)
            if c.execute('SELECT 1 FROM manager_conversation_locks WHERE actor=? AND project=? AND channel=? AND conversation=?',scope).fetchone():
                return self._reply(Reply('busy','앞선 요청을 확인 중입니다.'),project)
            c.execute('INSERT INTO manager_conversation_requests VALUES (?,?,?,?,?,?,?,\'running\',NULL,?,?)',
                (*key,conversation,fingerprint,config['fingerprint'],now,now+self.core_store.ttl))
            c.execute('INSERT INTO manager_conversation_locks VALUES (?,?,?,?,?)',(*scope,request_id))
        try:
            media_context=prepare_media(principal,attachments,text,media=self.media)
            self._auth(token,project,config['fingerprint'],channel=channel,conversation=conversation)
            blocked=media_gate(media_context)
            source=None;used=False
            if blocked is not None:
                self._clear_context(principal,channel,core_conversation)
                reply=blocked
            else:
                context=self._context(principal,channel,core_conversation)
                proposed=self.planner.propose(project,text,media=media_context,context=context)
                source=proposed.source
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
                else:
                    tools=build_registry(self.adapters,token,project,config['fingerprint'])
                    tool=tools.get(proposed.plan.tool)
                    if tool is None or tool.kind!='read':
                        raise PermissionDenied('conversation read tool required')
                    used=proposed.plan.follow_up and context is not None and context['tool']==proposed.plan.tool
                    reply=Core(self.core_store,tools).handle(principal,channel,core_conversation,core_request,plan=proposed.plan)
            self._auth(token,project,config['fingerprint'],channel=channel,conversation=conversation)
            response=self._reply(reply,project,source=source,context_used=used)
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
