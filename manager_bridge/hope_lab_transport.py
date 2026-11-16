"""Loopback-only fixture HTTP receiver/client; a nonproduction adapter contract."""
import json
import re
import secrets
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler
from .control import NoRedirect
from .hope_lab_model import LabBlocked
from .store import Conflict

PROTOCOL='hope-test-lab/v1'


def _pairs(pairs):
    out={}
    for key,value in pairs:
        if key in out:raise ValueError('duplicate key')
        out[key]=value
    return out


class LabHandler(BaseHTTPRequestHandler):
    def log_message(self,*_):pass

    def setup(self):
        super().setup();self.connection.settimeout(5)

    def reply(self,status,value):
        payload=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store')
        self.end_headers();self.wfile.write(payload)

    def do_GET(self):self.dispatch()
    def do_POST(self):self.dispatch()

    def dispatch(self):
        try:
            if self.path=='/test-lab/v1/capabilities' and self.command=='GET':
                return self.reply(200,dict(protocol=PROTOCOL,environment='local_reference_fixture',
                    source_revision=self.server.source_revision,operationalWrites=False,realCharges=False,
                    realNotifications=False,cardProvider='simulator',serverPersistence=True,dataSetup=True))
            match=re.fullmatch('/test-lab/v1/workspaces/([a-f0-9]{32})/(state|events|seed|command)',self.path)
            if not match:return self.reply(404,{'ok':False,'reason':'not_found'})
            workspace,action=match.groups();auth=self.headers.get('Authorization','')
            if not auth.startswith('Bearer ') or len(auth)>1024:raise PermissionError()
            token=auth[7:];store=self.server.lab
            if self.command=='GET' and action in ('state','events'):
                value=store.inspect(workspace,token) if action=='state' else store.evidence(workspace,token)
            elif self.command=='POST' and action in ('seed','command'):
                length=int(self.headers.get('Content-Length','0'))
                if self.headers.get_content_type()!='application/json' or not 0<length<=65536:raise ValueError()
                data=json.loads(self.rfile.read(length),object_pairs_hook=_pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if type(data) is not dict:raise ValueError()
                expected={'expected_generation','request_key','contract_digest'} if action=='seed' else {'generation','request_key','action','payload'}
                if set(data)!=expected:raise ValueError()
                if store.inspect(workspace,token)['source_revision']!=self.server.source_revision:raise Conflict('fixture source changed')
                value=store.seed(workspace,token,**data) if action=='seed' else store.command(workspace,token,**data)
            else:return self.reply(405,{'ok':False,'reason':'method_not_allowed'})
            return self.reply(200,{'ok':True,'value':value})
        except PermissionError:return self.reply(403,{'ok':False,'reason':'fixture_scope_or_role_denied'})
        except LabBlocked as error:return self.reply(409,{'ok':False,'reason':str(error)})
        except Conflict:return self.reply(409,{'ok':False,'reason':'fixture_conflict'})
        except (ValueError,KeyError,TypeError):return self.reply(400,{'ok':False,'reason':'invalid_fixture_request'})
        except Exception:return self.reply(503,{'ok':False,'reason':'fixture_unavailable'})


def make_lab_server(store,source_revision,*,host='127.0.0.1',port=0):
    if host!='127.0.0.1' or not re.fullmatch('[a-f0-9]{40}',source_revision):
        raise ValueError('loopback and pinned fixture source required')
    server=ThreadingHTTPServer((host,port),LabHandler);server.lab=store;server.source_revision=source_revision
    return server


class LabTransportUncertain(Exception):pass


class LabClient:
    def __init__(self,origin,session,role,*,source_revision):
        match=re.fullmatch(r'http://127\.0\.0\.1:([0-9]{1,5})',origin)
        if not match or not 1<=int(match[1])<=65535:raise ValueError('registered loopback fixture origin only')
        if source_revision!=session.source_revision:raise ValueError('session/source mismatch')
        if role not in session.tokens:raise ValueError('registered synthetic identity required')
        self.origin=origin;self.session=session;self._token=session.tokens[role];self.source_revision=source_revision
        self._opener=build_opener(ProxyHandler({}),NoRedirect());self.generation=0;self.events=[]

    def call(self,method,action,data=None):
        if method not in ('GET','POST') or action not in ('state','events','seed','command','capabilities'):
            raise ValueError('fixed fixture request required')
        path='/test-lab/v1/capabilities' if action=='capabilities' else '/test-lab/v1/workspaces/'+self.session.workspace+'/'+action
        body=json.dumps(data,allow_nan=False).encode() if data is not None else None
        request=Request(self.origin+path,data=body,method=method,
            headers={'Authorization':'Bearer '+self._token,'Content-Type':'application/json'})
        try:
            response=self._opener.open(request,timeout=5)
        except HTTPError as error:response=error
        except OSError:
            self.events.append(dict(action=action,state='uncertain',retry_performed=False))
            raise LabTransportUncertain('fixture_result_uncertain') from None
        try:
            with response:
                raw=response.read(4*1024*1024+1);status=response.code
            if len(raw)>4*1024*1024:raise LabTransportUncertain('fixture_response_limit')
            value=json.loads(raw)
        except (OSError,ValueError):raise LabTransportUncertain('fixture_response_unverified') from None
        self.events.append(dict(action=action,status=status,request_key=data.get('request_key') if type(data) is dict else None))
        return status,value

    def preflight(self):
        status,caps=self.call('GET','capabilities')
        if (status!=200 or caps.get('protocol')!=PROTOCOL or caps.get('environment')!='local_reference_fixture' or
            caps.get('source_revision')!=self.source_revision or caps.get('cardProvider')!='simulator' or
            caps.get('dataSetup') is not True or caps.get('serverPersistence') is not True or
            any(caps.get(k) is not False for k in ('operationalWrites','realCharges','realNotifications'))):
            raise ValueError('fixture environment/source not approved')
        return caps

    def state(self):
        status,value=self.call('GET','state')
        if status!=200:raise PermissionError('fixture state not available')
        return value['value']

    def seed(self,*,key=None):
        self.preflight()
        status,value=self.call('POST','seed',dict(expected_generation=self.generation,
            request_key=key or secrets.token_hex(16),contract_digest=self.session.contract_digest))
        if status!=200:raise Conflict('fixture seed/reset not confirmed')
        self.generation=value['value']['generation'];return value['value']

    def command(self,action,payload=None,*,key=None):
        self.preflight()
        return self.call('POST','command',dict(generation=self.generation,request_key=key or secrets.token_hex(16),
                                             action=action,payload=payload or {}))


def remote_fixture_readiness(capabilities):
    """No guessed setup endpoint and no remote mutation if protocol is absent."""
    safe=(type(capabilities) is dict and capabilities.get('mode')=='isolated_server_review' and
          capabilities.get('cardProvider')=='simulator' and all(capabilities.get(k) is False for k in
          ('operationalWrites','realCharges','realNotifications')))
    return dict(connected=False,setup_allowed=False,
        reason='adapter_binding_not_approved' if safe and capabilities.get('testDataProtocol')==PROTOCOL
        else 'test_data_protocol_not_advertised' if safe else 'nonproduction_guards_not_verified',
        operational_authorization=False)
