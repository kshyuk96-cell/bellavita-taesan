"""Current native metadata from both roles, never OAuth expiry or spend proof."""
import math
from .operating_budget import BudgetAuthObservation
from .subscription_observations import NativeSubscriptionObserver,SubscriptionObservation
from .store import Conflict
from .three_tier import ThreeTier


class DualRoleSubscriptionAdmission:
    def __init__(self,core,observers,token_loader):
        if not isinstance(core,ThreeTier) or type(observers) is not dict or not 1<=len(observers)<=100 or not callable(token_loader):
            raise ValueError('bounded registered native role observers required')
        for project,roles in observers.items():
            if (type(roles) is not dict or set(roles)!={'developer','reviewer'}
                    or any(type(o) is not NativeSubscriptionObserver or o.core is not core for o in roles.values())):
                raise ValueError('same-core exact developer and reviewer observers required')
        self.core,self.store,self.observers,self.token_loader=core,core.store,{p:dict(v) for p,v in observers.items()},token_loader

    def __call__(self,project):
        if project not in self.observers:raise Conflict('project outside registered authentication observers')
        token=self.token_loader()
        with self.store._transaction() as c:
            config=self.core._config(c,token,project,('manager',))
            self.core._actor(c,token,project,'taesan',config)
            fingerprint=config['fingerprint']
            members={role:self.core._member(c,project,o.member_id,role,config) for role,o in self.observers[project].items()}
            bindings={role:(m['credential_hash'],m['subject']) for role,m in members.items()}
        observations=[]
        for role in ('developer','reviewer'):
            observations.append(self.observers[project][role].read(token,project))
        with self.store._transaction() as c:
            if self.token_loader()!=token:raise Conflict('manager authentication changed during observation')
            config=self.core._config(c,token,project,('manager',))
            self.core._actor(c,token,project,'taesan',config)
            if config['fingerprint']!=fingerprint:raise Conflict('authentication project binding changed')
            for role,o in self.observers[project].items():
                member=self.core._member(c,project,o.member_id,role,config)
                if (member['credential_hash'],member['subject'])!=bindings[role]:raise Conflict('native role binding changed')
            now=self.store._now()
            if type(now) not in (int,float) or not math.isfinite(now) or now<=0:raise Conflict('finite authentication clock required')
        good=all(type(o) is SubscriptionObservation and o.project==project
                 and o.config_fingerprint==fingerprint and o.state=='observed'
                 and o.ordinary_usage_allowed is True and 0<=now-o.observed_at
                 and now<o.valid_until for o in observations)
        if not good:return BudgetAuthObservation(project,fingerprint,'unknown',now,None,'managed-subscription/dual-role-unverified')
        return BudgetAuthObservation(project,fingerprint,'ready',min(o.observed_at for o in observations),
                                     min(o.valid_until for o in observations),'managed-subscription/dual-role')
