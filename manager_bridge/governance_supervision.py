"""Opt-in local supervision, including explicitly registered subscription rotation.

Default schedules do not refresh credentials or send external messages.
"""
from dataclasses import dataclass
import json
import math
import secrets
from .governance_durability import GovernanceDurability,_directory
from .governance_routing import GovernanceRouting
from .store import Conflict,canonical_json,digest


@dataclass(frozen=True)
class SupervisionSettings:
    auth_interval: int = 60
    health_interval: int = 30
    recovery_interval: int = 30
    subscription_interval: int | None = None
    backup_interval: int | None = None
    backup_root: str | None = None
    retention: int = 3

    def __post_init__(self):
        for key,minimum,maximum in (('auth_interval',10,3600),('health_interval',5,300),('recovery_interval',5,300),('retention',1,100)):
            value=getattr(self,key)
            if type(value) is not int or not minimum<=value<=maximum:raise ValueError('bounded supervision settings required')
        if self.subscription_interval is not None and (type(self.subscription_interval) is not int or not 300<=self.subscription_interval<=86400):
            raise ValueError('explicit bounded subscription interval required')
        if self.backup_interval is not None:
            if type(self.backup_interval) is not int or not 300<=self.backup_interval<=86400 or type(self.backup_root) is not str:
                raise ValueError('explicit bounded private backup settings required')
            _directory(self.backup_root)
        elif self.backup_root is not None:raise ValueError('backup interval required')


class GovernanceSupervision:
    def __init__(self,host,controller,projects,settings,*,health,owner_token_loader=None,expiration_provider=None,subscription_broker=None):
        if not isinstance(settings,SupervisionSettings) or controller.core is not host.core or not callable(health):
            raise ValueError('trusted supervision composition required')
        if settings.backup_interval is not None and not callable(owner_token_loader):
            raise ValueError('registered owner loader required for backups')
        if owner_token_loader is not None and not callable(owner_token_loader):raise ValueError('trusted owner loader required')
        self.validate_registration(host.core,projects,settings,owner_token_loader,subscription_broker)
        self.subscription_broker=subscription_broker
        self.host,self.controller,self.store,self.registry=host,controller,host.store,host.registry
        self.projects=tuple(projects);self.settings=settings;self.health=health;self.owner_token_loader=owner_token_loader
        self.durability=GovernanceDurability(host.core,expiration_provider=expiration_provider)
        self.routing=GovernanceRouting(host.core,controller)
        self.scope=digest(sorted(self.projects))
        self.binding=digest({'projects':sorted(self.projects),'settings':settings.__dict__})
        with self.store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS governance_supervision_jobs (scope TEXT,job TEXT,binding TEXT,attempt TEXT,state TEXT,last_attempt REAL,next_due REAL,snapshot TEXT,PRIMARY KEY(scope,job))')
            c.execute('CREATE TABLE IF NOT EXISTS governance_supervision_backups (attempt TEXT PRIMARY KEY,scope TEXT,path TEXT,state TEXT,recorded REAL)')

    @staticmethod
    def validate_registration(core,projects,settings,owner_token_loader,subscription_broker):
        from .subscription_broker import SubscriptionBroker
        if not isinstance(settings,SupervisionSettings):raise ValueError('typed supervision settings required')
        if settings.subscription_interval is None:
            if subscription_broker is not None:raise ValueError('explicit subscription schedule required')
            return
        if not isinstance(subscription_broker,SubscriptionBroker) or subscription_broker.core is not core or subscription_broker.store is not core.store or not callable(owner_token_loader):
            raise ValueError('registered same-core subscription broker and owner loader required')
        if {target.project for target in subscription_broker.targets}!=set(projects):raise ValueError('exact subscription project scope required')
        if not callable(subscription_broker.refresh_callback):raise ValueError('registered native subscription refresher required')
        owner=owner_token_loader()
        with core.store._transaction() as c:subscription_broker._auth(c,owner,owner=True)

    def _subscription(self,attempt):
        owner=None;entered=False
        try:
            owner=self.owner_token_loader()
            status=self.subscription_broker.status(owner)
            if status['state'] not in ('ready','unverified') or status.get('recovery_quarantined'):
                return 'uncertain',{'state':'uncertain','automatic_replay':False}
            with self.store._transaction() as c:self.subscription_broker._auth(c,owner,owner=True,idle=True)
            entered=True
            receipt=self.subscription_broker.refresh_and_distribute(owner,status['generation'],'supervision-'+attempt)
            return 'observed',{'state':'ready','generation':receipt['generation'],
                               'profiles_updated':receipt['profiles_updated'],'refresh_requested':receipt['refresh_requested'],
                               'observed_at':receipt['observed_at'],'api_key_fallback':False,'models_started':0}
        except Exception:
            try:
                status=self.subscription_broker.status(owner)
                uncertain=status['state'] not in ('ready','unverified') or status.get('recovery_quarantined',False)
            # The broker preserves ready/unverified only for a failure before its
            # external callback (including a busy shared profile guard).
            except Exception:uncertain=entered
            state='uncertain' if uncertain else 'unavailable'
            return state,{'state':state,'automatic_replay':False,'api_key_fallback':False}

    def _auth(self,c,token,projects):
        if type(projects) not in (list,tuple) or not projects or len(projects)>100 or len(set(projects))!=len(projects) or any(p not in self.projects for p in projects):raise ValueError('configured supervision scope required')
        return {p:self.registry._get(c,token,p)['fingerprint'] for p in projects}

    def _claim(self,token,job,interval):
        with self.store._transaction() as c:
            configs=self._auth(c,token,self.projects);now=self.store._now()
            row=c.execute('SELECT * FROM governance_supervision_jobs WHERE scope=? AND job=?',(self.scope,job)).fetchone()
            if row and (row['state'] in ('running','uncertain') or row['next_due']>now):return None
            attempt=secrets.token_hex(16)
            c.execute("INSERT OR REPLACE INTO governance_supervision_jobs VALUES (?,?,?,?,'running',?,?,NULL)",
                      (self.scope,job,self.binding,attempt,now,now+interval))
            return attempt,configs

    def _finish(self,token,job,claim,state,snapshot):
        attempt,configs=claim
        with self.store._transaction() as c:
            if self._auth(c,token,self.projects)!=configs:raise Conflict('supervision configuration changed')
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,snapshot)
            updated=c.execute("UPDATE governance_supervision_jobs SET state=?,snapshot=? WHERE scope=? AND job=? AND attempt=? AND state='running'",
                      (state,canonical_json({**snapshot,'_configs':configs}),self.scope,job,attempt))
            if updated.rowcount!=1:raise Conflict('supervision attempt ownership changed')

    def _backup(self,attempt):
        # Unknown prior attempts are retained and count against the cap. No cleanup
        # or overwrite is inferred from a missing completion receipt.
        owner=self.owner_token_loader()
        self.durability._global_owner(owner)
        root=_directory(self.settings.backup_root);destination=root/('snapshot-'+attempt)
        with self.store._transaction() as c:
            count=c.execute('SELECT COUNT(*) FROM governance_supervision_backups WHERE scope=?',(self.scope,)).fetchone()[0]
            if count>=self.settings.retention:return 'retention_required',{'state':'retention_required','automatic_deletion':False}
            c.execute("INSERT INTO governance_supervision_backups VALUES (?,?,?,'running',?)",(attempt,self.scope,str(destination),self.store._now()))
        receipt=self.durability.backup(owner,destination)
        with self.store._transaction() as c:
            changed=c.execute("UPDATE governance_supervision_backups SET state='complete' WHERE attempt=? AND scope=? AND state='running'",(attempt,self.scope))
            if changed.rowcount!=1:raise Conflict('backup reservation ownership changed')
        return 'observed',{'state':receipt['state'],'automatic_replay':False,'retained_locally':True}

    def poll(self,token,*,stop_event=None):
        intervals={'auth':self.settings.auth_interval,'health':self.settings.health_interval,'recovery':self.settings.recovery_interval}
        if self.settings.backup_interval is not None:intervals['backup']=self.settings.backup_interval
        if self.settings.subscription_interval is not None:intervals['subscription']=self.settings.subscription_interval
        for job,interval in intervals.items():
            if stop_event is not None and stop_event.is_set():break
            claim=self._claim(token,job,interval)
            if claim is None:continue
            try:
                if job=='auth':
                    value=self.durability.auth_watch(token,self.projects)
                    snapshot={'items':[{k:i[k] for k in ('project','member_id','role','state','profile_state')} for i in value['items']],
                              'refresh_performed':False,'messages_sent':False}
                    state='observed'
                elif job=='health':
                    value=self.health(token,self.projects)
                    if type(value) is not dict or value.get('state') not in ('running','stopping','unavailable') or value.get('http_state') not in ('listening','stopped','unknown') or value.get('manager_state') not in ('running','idle','stopped','unknown') or type(value.get('observed_at')) not in (int,float) or not math.isfinite(value['observed_at']) or not 0<=self.store._now()-value['observed_at']<=60:
                        raise ValueError('measured health required')
                    snapshot={k:value[k] for k in ('state','http_state','manager_state','observed_at')};state='observed'
                elif job=='recovery':
                    value=self.routing.plan(token,self.projects)['recovery']
                    snapshot={k:value[k] for k in ('state','leader_state','reconciliation_required','action_count','truncated') if k in value}
                    snapshot['automatic_replay']=False;state='observed'
                elif job=='subscription':state,snapshot=self._subscription(claim[0])
                else:state,snapshot=self._backup(claim[0])
            except Exception:
                # No provider messages, credential values or paths enter reports.
                state='unavailable'
                if job=='backup':
                    with self.store._transaction() as c:
                        reserved=c.execute('SELECT state FROM governance_supervision_backups WHERE attempt=?',(claim[0],)).fetchone()
                        if reserved is not None:
                            state='uncertain'
                            c.execute("UPDATE governance_supervision_backups SET state='uncertain' WHERE attempt=? AND state='running'",(claim[0],))
                snapshot={'state':state,'automatic_replay':False}
            self._finish(token,job,claim,state,snapshot)
        return self.report(token,self.projects)

    def report(self,token,projects):
        with self.store._transaction() as c:
            # Shared recovery/backup/subscription snapshots cover the whole host
            # scope; subset filtering cannot authorize those aggregate facts.
            self._auth(c,token,self.projects)
            configs=self._auth(c,token,projects)
            rows=c.execute('SELECT job,state,last_attempt,next_due,snapshot FROM governance_supervision_jobs WHERE scope=? ORDER BY job',(self.scope,)).fetchall()
            jobs=[]
            for row in rows:
                snapshot=json.loads(row['snapshot']) if row['snapshot'] else {'state':'unresolved_attempt','automatic_replay':False}
                recorded_configs=snapshot.pop('_configs',{})
                stale=self.store._now()>=row['next_due'] or any(recorded_configs.get(p)!=configs[p] for p in projects)
                if row['job']=='auth' and 'items' in snapshot:snapshot['items']=[item for item in snapshot['items'] if item['project'] in projects]
                jobs.append({'job':row['job'],'state':row['state'],'last_attempt':row['last_attempt'],'next_due':row['next_due'],'stale':stale,'reconciliation_required':row['state'] in ('running','uncertain'),'snapshot':snapshot})
            subscription=next((job for job in jobs if job['job']=='subscription'),None)
            observed=bool(subscription and subscription['state']=='observed' and subscription['snapshot'].get('refresh_requested') is True)
            unknown=bool(subscription and subscription['state'] in ('running','uncertain'))
            refresh_fact={key:subscription['snapshot'][key] for key in ('generation','profiles_updated','observed_at')} if observed else None
            return {'projects':list(projects),'jobs':jobs,'backup_enabled':self.settings.backup_interval is not None,'subscription_enabled':self.settings.subscription_interval is not None,
                    'credential_refresh_performed':True if observed else (None if unknown else False),
                    'credential_refresh_scope':'last_retained_subscription_attempt',
                    'observed_subscription_refresh':refresh_fact,'messages_sent':False,'always_on_verified':False}
