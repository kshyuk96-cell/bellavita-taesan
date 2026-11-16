"""Only isolated Hope review fixtures; never operational APIs or real providers."""
import http.cookiejar
import json
import re
import time
import uuid
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor
from urllib.error import HTTPError, URLError
from .control import NoRedirect, secret
from .store import digest

ORIGIN = 'https://hope-tour-feedback.shrewdmaxim1.chatgpt.site'
ACTIONS = frozenset({'registerCard','signup','runCycle','runCurrentCycle','book','payInvoice',
    'requestRefund','reviewRefund','approveRefund','executeRefund','reconcilePayment'})


class ReviewUncertain(Exception):
    pass


class HopeReviewClient:
    def __init__(self, *, token_file=None, origin=ORIGIN):
        if origin != ORIGIN and not re.fullmatch(r'http://127\.0\.0\.1:\d{1,5}',origin):
            raise ValueError('fixed review origin or local fixture required')
        self.origin=origin
        self._auth=secret(token_file) if token_file is not None else None
        self._opener=build_opener(ProxyHandler() if origin==ORIGIN else ProxyHandler({}),NoRedirect(),HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.events=[]

    def _request(self, method, path, body=None):
        if not re.fullmatch(r'/review/(?:status\.json|api/(?:login|state|command)|features|guide|app|home|start|)',path):
            raise ValueError('review-only endpoint required')
        headers={'Content-Type':'application/json','User-Agent':'TaesanCustomerQA/1'}
        if self._auth:headers['OAI-Sites-Authorization']='Bearer '+self._auth
        data=json.dumps(body,allow_nan=False).encode() if body is not None else None
        started=time.monotonic()
        request=Request(self.origin+path,data=data,method=method,headers=headers)
        try:
            response=self._opener.open(request,timeout=15)
        except HTTPError as error:
            response=error
        except (OSError,URLError):
            self.events.append(dict(method=method,path=path,state='uncertain',retry_performed=False))
            raise ReviewUncertain('request_outcome_uncertain') from None
        with response:
            raw=response.read(2_000_001)
            if len(raw)>2_000_000:raise ReviewUncertain('bounded_response_required')
            status=response.code
        try:value=json.loads(raw)
        except (ValueError,UnicodeError):value=raw.decode(errors='replace')
        # No headers, cookies, login body, tokens or exception text in evidence.
        self.events.append(dict(method=method,path=path,status=status,response_digest=digest(value),
                                elapsed_ms=round((time.monotonic()-started)*1000)))
        return status,value

    def capabilities(self):
        status,caps=self._request('GET','/review/status.json')
        if (status!=200 or type(caps) is not dict or caps.get('mode')!='isolated_server_review' or
                caps.get('cardProvider')!='simulator' or caps.get('serverPersistence') is not True or
                any(caps.get(key) is not False for key in ('realCharges','operationalWrites','realNotifications'))):
            raise ReviewUncertain('isolated_simulator_guards_not_verified')
        return caps

    def get(self,path):return self._request('GET',path)

    def login(self,role):
        if role not in ('member','admin','finance'):raise ValueError('fixture role required')
        self.capabilities()
        status,value=self._request('POST','/review/api/login',dict(username=role+'@hope.test',password='HopeTest!2026'))
        if status!=200:raise ReviewUncertain('fixture_login_not_verified')
        return value

    def command(self,action,fields=None,*,request_id=None,expected_status=200):
        if action not in ACTIONS:raise ValueError('registered synthetic action required')
        fields=fields or {}
        allowed={'registerCard':{'scenario'},'signup':{'grade','consents'},'book':{'expectedTotal'},
            'payInvoice':{'invoiceId'},'requestRefund':{'bookingId','rooms'},
            'reviewRefund':{'refundId'},'approveRefund':{'refundId'},'executeRefund':{'refundId'}}.get(action,set())
        if type(fields) is not dict or not set(fields)<=allowed:
            raise ValueError('synthetic fields only; no PAN/CVC/workspace/provider inputs')
        if action=='registerCard' and fields.get('scenario') not in ('success','decline','unknown'):
            raise ValueError('simulator scenario required')
        if action=='signup':
            if fields.get('grade') not in ('hope','happy','premium'):
                raise ValueError('registered personal fixture grade required')
            consents=fields.get('consents')
            if type(consents) is not dict or set(consents)!={'terms','privacy','recurring','marketing'} or any(type(v) is not bool for v in consents.values()):
                raise ValueError('fixed boolean consent fixture required')
        if action=='book' and (type(fields.get('expectedTotal')) is not int or not 0<fields['expectedTotal']<=10**12):
            raise ValueError('bounded fixture price required')
        if action=='requestRefund' and (type(fields.get('rooms')) is not int or not 1<=fields['rooms']<=2):
            raise ValueError('fixed review room range required')
        for key in ('invoiceId','bookingId','refundId'):
            if key in fields and (type(fields[key]) is not str or not re.fullmatch('[a-f0-9-]{36}',fields[key])):
                raise ValueError('observed fixture UUID required')
        if request_id is not None and (type(request_id) is not str or not re.fullmatch('[a-f0-9-]{36}',request_id)):
            raise ValueError('bounded request UUID required')
        self.capabilities()
        status,value=self._request('POST','/review/api/command',dict(action=action,**fields,requestId=request_id or str(uuid.uuid4())))
        if status!=expected_status:raise ReviewUncertain('command_result_requires_reconciliation')
        return value

    def state(self):
        status,value=self._request('GET','/review/api/state')
        if status!=200 or type(value) is not dict or value.get('realCharges') is not False:
            raise ReviewUncertain('fixture_state_not_verified')
        return value
