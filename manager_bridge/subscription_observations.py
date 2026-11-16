"""Read-only native subscription metadata; account-wide, never project spend."""
from dataclasses import dataclass,asdict
import math
from .fleet_codex import CodexHost,_Rpc
from .three_tier import ThreeTier
from .store import Conflict,PermissionDenied

@dataclass(frozen=True)
class RateWindow:
    kind:str
    used_percent:int
    window_minutes:int|None
    resets_at:int|None
    def __post_init__(self):
        if self.kind not in ('primary','secondary'):raise ValueError('fixed window kind required')
        if type(self.used_percent) is not int or not 0<=self.used_percent<=100:raise ValueError('bounded percent required')
        for value,maximum in ((self.window_minutes,525600),(self.resets_at,32503680000)):
            if value is not None and (type(value) is not int or not 0<value<=maximum):raise ValueError('bounded window metadata required')

@dataclass(frozen=True)
class SubscriptionObservation:
    project:str
    config_fingerprint:str
    state:str
    observed_at:float
    valid_until:float
    ordinary_usage_allowed:bool|None
    windows:tuple
    def __post_init__(self):
        from .maintenance_registry import name
        name(self.project)
        if type(self.config_fingerprint) is not str or len(self.config_fingerprint)!=64 or any(c not in '0123456789abcdef' for c in self.config_fingerprint):raise ValueError('configuration digest required')
        if self.state not in ('observed','unavailable','malformed','account_unavailable'):raise ValueError('fixed observation state required')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (self.observed_at,self.valid_until)) or not 0<self.valid_until-self.observed_at<=60:raise ValueError('bounded freshness required')
        if self.ordinary_usage_allowed is not None and type(self.ordinary_usage_allowed) is not bool:raise ValueError('explicit permission required')
        if type(self.windows) is not tuple or len(self.windows)>2 or any(type(w) is not RateWindow for w in self.windows) or len({w.kind for w in self.windows})!=len(self.windows):raise ValueError('bounded typed windows required')
        if self.state!='observed' and (self.windows or self.ordinary_usage_allowed is not None):raise ValueError('unavailable observation carries no recovered limits')
    def public(self):
        return {**asdict(self),'scope':'subscription_account','project_spend_measured':False,'api_cost':None,'execution_authorized':False}

def sanitize(project,fingerprint,account,response,now,ttl=30):
    """Allowlist output. Unknown payload fields, identity, balances never escape."""
    if type(ttl) is not int or not 1<=ttl<=60:raise ValueError('bounded observation ttl required')
    def result(state,permission=None,windows=()):return SubscriptionObservation(project,fingerprint,state,now,now+ttl,permission,windows)
    if type(account) is not dict or type(account.get('account')) is not dict or account['account'].get('type')!='chatgpt':return result('account_unavailable')
    if type(response) is not dict or type(response.get('rateLimits')) is not dict:return result('malformed')
    permission=response.get('ordinaryUsageAllowed')
    if permission is not None and type(permission) is not bool:return result('malformed')
    windows=[]
    try:
        for kind in ('primary','secondary'):
            value=response['rateLimits'].get(kind)
            if value is None:continue
            if type(value) is not dict:raise ValueError()
            windows.append(RateWindow(kind,value['usedPercent'],value.get('windowDurationMins'),value.get('resetsAt')))
    except (ValueError,KeyError,TypeError):return result('malformed')
    # Empty windows stay unavailable, never zero-use. Backend permission is retained
    # only with an otherwise valid response, and cannot authorize execution.
    if not windows and permission is None:return result('unavailable')
    return result('observed',permission,tuple(windows))

class NativeSubscriptionObserver:
    def __init__(self,core,host,member_id,*,ttl=30,diagnostic_sink=None):
        if not isinstance(core,ThreeTier) or not isinstance(host,CodexHost) or host.profile_guard is None:raise ValueError('guarded registered native host required')
        from .maintenance_registry import name
        name(member_id)
        if type(ttl) is not int or not 1<=ttl<=60:raise ValueError('bounded ttl required')
        if diagnostic_sink is not None and not callable(diagnostic_sink):raise ValueError('trusted diagnostic callback required')
        self.diagnostic_sink=diagnostic_sink
        self.core,self.store,self.host,self.member_id,self.ttl=core,core.store,host,member_id,ttl
    def _authorize(self,c,token,project):
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager','worker'))
        config=self.core._config(c,token,project)
        row=c.execute('SELECT role FROM hierarchy_members WHERE project=? AND member_id=?',(project,self.member_id)).fetchone()
        if row is None or row['role'] not in ('developer','reviewer'):raise PermissionDenied('registered subscription member required')
        member=self.core._member(c,project,self.member_id,row['role'],config)
        if actor['role']=='worker' and actor['token_hash']!=member['credential_hash']:raise PermissionDenied('assigned subscription member required')
        return config['fingerprint'],member['credential_hash'],member['subject']
    def read(self,token,project):
        with self.store._transaction() as c:binding=self._authorize(c,token,project)
        now=self.store._now()
        stage='connection'
        try:
            with _Rpc(self.host,project) as rpc:
                stage='account'
                account=rpc.request('account/read',{'refreshToken':False})
                stage='rate_limits'
                response=rpc.request('account/rateLimits/read',{'excludeResetCreditDetails':True,'supportsLunaReserve':False})
            stage='sanitize'
            observation=sanitize(project,binding[0],account,response,now,self.ttl)
        except Exception as error:
            category=type(error).__name__
            if category not in ('Conflict','PermissionDenied','TransportUnavailable','ValueError','TimeoutError'):category='unavailable'
            if self.diagnostic_sink is not None:self.diagnostic_sink({'stage':stage,'category':category})
            observation=SubscriptionObservation(project,binding[0],'unavailable',now,now+self.ttl,None,())
        with self.store._transaction() as c:
            if self._authorize(c,token,project)!=binding:raise Conflict('subscription observation authority changed')
            if self.store._now()>=observation.valid_until:raise Conflict('subscription observation expired')
        return observation
