"""Trusted owner-only add-only enrollment into one managed subscription broker."""
from dataclasses import dataclass
from pathlib import Path
import json,os,stat
from .subscription_broker import SubscriptionBroker,SubscriptionTarget,_physical,_home_digest,_read,_atomic
from .store import Conflict,canonical_json,digest

@dataclass(frozen=True)
class EnrollmentPlan:
    broker_ref:str
    old_binding:str
    new_binding:str
    expected_generation:int
    authority_digest:str
    additions:tuple

def _view(broker,targets):
    view=object.__new__(SubscriptionBroker);view.__dict__=dict(broker.__dict__);view.targets=tuple(targets)
    return view

def _empty(home):
    home=_physical(home);info=home.stat()
    if info.st_mode&0o777!=0o700 or info.st_uid!=os.getuid() or any(home.iterdir()):raise Conflict('new private empty profile required')

class SubscriptionEnrollment:
    def __init__(self,broker):
        if type(broker) is not SubscriptionBroker:raise ValueError('registered subscription broker required')
        self.broker=broker;self.store=broker.store
    def _validate(self,c,owner,additions,generation):
        b=self.broker
        if type(generation) is not int or generation<0 or type(additions) is not tuple or not 1<=len(additions)<=100 or any(type(t) is not SubscriptionTarget for t in additions):raise ValueError('bounded typed enrollment required')
        targets=(*b.targets,*additions)
        if len(targets)>200 or len({(t.project,t.member_id) for t in targets})!=len(targets):raise Conflict('new distinct members required')
        homes=[b.home,*[t.home for t in targets]]
        if len({_home_digest(h) for h in homes})!=len(homes) or any(a==z or a in z.parents or z in a.parents for i,a in enumerate(homes) for z in homes[i+1:]):raise Conflict('distinct non-overlapping profiles required')
        for t in additions:
            _empty(t.home)
            if t.home==self.store.path or t.home in self.store.path.parents:raise Conflict('database outside profiles required')
            if c.execute('SELECT 1 FROM subscription_profile_owners WHERE home_digest=?',(_home_digest(t.home),)).fetchone():raise Conflict('profile ownership already registered')
        from .agent_directory import _reject_known_tokens
        from .store import _reject_credentials
        from .subscription_broker import _credential_strings
        metadata=[(t.project,t.member_id,str(t.home)) for t in additions]
        _reject_known_tokens(c,metadata)
        _reject_credentials(metadata,*_credential_strings(json.loads(_read(b.home)[0])['tokens']))
        actor,bindings=_view(b,targets)._auth(c,owner,owner=True,idle=True)
        row=b._state(c);auth=_read(b.home)
        if row['state']!='ready' or row['generation']!=generation:raise Conflict('ready exact generation required')
        if auth[1]!=row['canonical_digest'] or b._targets(auth[2])!=json.loads(row['target_digests']):raise Conflict('subscription profile drift requires reconciliation')
        binding=digest({'canonical':str(b.home),'targets':[(t.project,t.member_id,t.role,str(t.home)) for t in targets]})
        return actor,bindings,row,auth,binding
    def plan(self,owner,additions,expected_generation):
        additions=tuple(additions) if type(additions) in (list,tuple) else additions
        with self.broker._lock(True):
            with self.store._transaction() as c:
                _,bindings,row,auth,binding=self._validate(c,owner,additions,expected_generation)
                return EnrollmentPlan(self.broker.ref,self.broker.binding,binding,expected_generation,digest(bindings),additions)
    def apply(self,owner,plan):
        if type(plan) is not EnrollmentPlan:raise ValueError('typed exact enrollment plan required')
        b=self.broker;started=False
        with b._lock(True):
            try:
                with self.store._transaction() as c:
                    _,bindings,row,auth,binding=self._validate(c,owner,plan.additions,plan.expected_generation)
                    if (plan.broker_ref,plan.old_binding,plan.new_binding,plan.authority_digest)!=(b.ref,b.binding,binding,digest(bindings)):raise Conflict('enrollment plan authority changed')
                    c.execute('CREATE TABLE IF NOT EXISTS subscription_enrollments (broker_ref TEXT,generation INTEGER,plan_digest TEXT,state TEXT,updated REAL,PRIMARY KEY(broker_ref,generation))')
                    if c.execute('SELECT 1 FROM subscription_enrollments WHERE broker_ref=? AND generation=?',(b.ref,plan.expected_generation)).fetchone():raise Conflict('enrollment replay requires explicit reconciliation')
                    for t in plan.additions:c.execute('INSERT INTO subscription_profile_owners VALUES (?,?,?)',(_home_digest(t.home),b.ref,t.project+':'+t.member_id))
                    c.execute("INSERT INTO subscription_enrollments VALUES (?,?,?,'enrolling',?)",(b.ref,plan.expected_generation,digest({'old':plan.old_binding,'new':binding,'authority':plan.authority_digest}),self.store._now()))
                    c.execute("UPDATE subscription_broker_state SET state='enrolling',updated=? WHERE ref=?",(self.store._now(),b.ref))
                started=True;view=_view(b,(*b.targets,*plan.additions))
                def fence(c):
                    _,current=view._auth(c,owner,owner=True,idle=True)
                    row=c.execute('SELECT * FROM subscription_broker_state WHERE ref=?',(b.ref,)).fetchone()
                    if digest(current)!=plan.authority_digest or row['binding']!=plan.old_binding or row['generation']!=plan.expected_generation or row['state']!='enrolling':raise Conflict('enrollment authority changed')
                    expected={(_home_digest(b.home),'canonical'),*[(_home_digest(t.home),t.project+':'+t.member_id) for t in view.targets]}
                    actual={(r['home_digest'],r['profile_key']) for r in c.execute('SELECT home_digest,profile_key FROM subscription_profile_owners WHERE broker_ref=?',(b.ref,))}
                    if expected!=actual or _read(b.home)[1]!=auth[1] or b._targets(auth[2])!=json.loads(row['target_digests']):raise Conflict('enrollment profile drift')
                for target in plan.additions:
                    with self.store._transaction() as c:
                        fence(c);_empty(target.home);_atomic(target.home,auth[0],None)
                with self.store._transaction() as c:
                    fence(c);snapshots=view._targets(auth[2])
                    if any(snapshots[t.project+':'+t.member_id]!=auth[1] for t in plan.additions):raise Conflict('enrollment copies unverified')
                    c.execute("UPDATE subscription_broker_state SET binding=?,generation=?,state='ready',target_digests=?,updated=? WHERE ref=?",(binding,plan.expected_generation+1,canonical_json(snapshots),self.store._now(),b.ref))
                    c.execute("UPDATE subscription_enrollments SET state='ready',updated=? WHERE broker_ref=? AND generation=?",(self.store._now(),b.ref,plan.expected_generation))
            except BaseException:
                if started:
                    with self.store._transaction() as c:
                        c.execute("UPDATE subscription_broker_state SET state='uncertain',updated=? WHERE ref=?",(self.store._now(),b.ref))
                        c.execute("UPDATE subscription_enrollments SET state='uncertain',updated=? WHERE broker_ref=? AND generation=?",(self.store._now(),b.ref,plan.expected_generation))
                raise
        return SubscriptionBroker(b.core,b.ref,b.home,view.targets,refresh_callback=b.refresh_callback)
