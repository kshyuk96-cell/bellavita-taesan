"""Registered native worker metadata links; metadata is not execution proof."""
from dataclasses import dataclass
import json
from pathlib import Path
from .connection_readiness import SourcePin
from .fleet_codex import CodexHost
from .subscription_observations import NativeSubscriptionObserver
from .three_tier import ThreeTier
from .store import Conflict,PermissionDenied,NotFound


@dataclass(frozen=True)
class WorkerLink:
    pin:SourcePin
    role:str
    member_id:str
    host:CodexHost
    def __post_init__(self):
        from .maintenance_registry import name
        if type(self.pin) is not SourcePin or self.role not in ('developer','reviewer'):
            raise ValueError('exact source pin and native role required')
        name(self.member_id)
        if not isinstance(self.host,CodexHost) or self.host.profile_guard is None:
            raise ValueError('registered guarded native transport required')
        binding=self.host._binding(self.pin.project)
        repository=self.pin.source_ref if self.pin.source_kind=='github' else 'sites/'+self.pin.source_ref
        if binding.repository!=repository:raise ValueError('worker repository differs from source pin')
        if self.pin.source_kind=='github' and not binding.require_origin:
            raise ValueError('GitHub worker requires real origin verification')


class NativeWorkerLinks:
    def __init__(self,core,links):
        if not isinstance(core,ThreeTier) or type(links) not in (list,tuple) or not 1<=len(links)<=40:
            raise ValueError('bounded registered native links required')
        if any(type(link) is not WorkerLink for link in links):raise ValueError('typed worker links required')
        if len({(link.pin.project,link.role) for link in links})!=len(links):raise ValueError('unique project role links required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.links=tuple(links)

    def _bindings(self,c,token,projects):
        if type(projects) not in (list,tuple) or not 1<=len(projects)<=20 or any(type(p) is not str for p in projects) or len(set(projects))!=len(projects):
            raise ValueError('explicit bounded project selection required')
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        result={}
        for project in projects:
            cfg=self.core._config(c,token,project,('owner','manager'))
            result[project]={'config':cfg['fingerprint'],'roles':{}}
            for link in self.links:
                if link.pin.project!=project:continue
                member=self.core._member(c,project,link.member_id,link.role,cfg)
                content=cfg['config']
                if (link.pin.source_kind,link.pin.source_ref,link.pin.branch)!=(content['source_kind'],content['source_ref'],content['branch']):
                    raise Conflict('registered worker source configuration changed')
                result[project]['roles'][link.role]=(member['credential_hash'],member['subject'])
        return result

    def _source(self,link):
        binding=link.host._binding(link.pin.project)
        repository=link.pin.source_ref if link.pin.source_kind=='github' else 'sites/'+link.pin.source_ref
        if binding.repository!=repository or link.host.profile_guard is None or (link.pin.source_kind=='github' and not binding.require_origin):
            raise Conflict('worker transport binding changed')
        observed=link.host._source(link.pin.project)
        if observed['sha']!=link.pin.sha or not observed['tracked_and_visible_untracked_clean']:
            raise Conflict('worker checkout differs from pinned clean source')
        if link.pin.source_kind=='sites':
            path=link.host._binding(link.pin.project).cwd/'.openai'/'hosting.json'
            if path.parent.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size>65536:
                raise Conflict('Sites source identity unavailable')
            try:manifest=json.loads(path.read_text())
            except (ValueError,UnicodeError):raise Conflict('Sites source identity malformed') from None
            if manifest.get('project_id')!=link.pin.source_ref:raise Conflict('Sites source project differs')
        return observed

    def inspect(self,token,projects):
        with self.store._transaction() as c:before=self._bindings(c,token,projects)
        items=[]
        for project in projects:
            for role in ('developer','reviewer'):
                link=next((link for link in self.links if (link.pin.project,link.role)==(project,role)),None)
                if link is None:
                    items.append({'project':project,'role':role,'state':'not_configured','metadata_connection_verified':False,'worker_execution_verified':False})
                    continue
                with self.store._transaction() as c:
                    if self._bindings(c,token,projects)!=before:raise Conflict('worker authority changed')
                source=None;subscription=None;state='unavailable'
                try:
                    source=self._source(link)
                    subscription=NativeSubscriptionObserver(self.core,link.host,link.member_id).read(token,project)
                    self._source(link)
                    state=('metadata_connected' if subscription.state=='observed' and subscription.ordinary_usage_allowed is True else
                           'usage_denied' if subscription.ordinary_usage_allowed is False else 'subscription_unverified')
                except (Conflict,PermissionDenied):
                    with self.store._transaction() as c:
                        if self._bindings(c,token,projects)!=before:raise Conflict('worker authority changed')
                    state='source_or_profile_unverified'
                    source=None
                except Exception:state='unavailable'
                item={'project':project,'role':role,'state':state,'source_revision':link.pin.revision,'source_sha':link.pin.sha,
                      'source_verified':source is not None,'metadata_connection_verified':state=='metadata_connected',
                      'worker_execution_verified':False,'execution_authorized':False}
                if subscription is not None:item['subscription']=subscription.public()
                items.append(item)
        # Later native reads may race an earlier source/profile observation.
        # Recheck all previously verified local bindings after the last RPC.
        for item in items:
            if not item.get('source_verified'):continue
            link=next(link for link in self.links if (link.pin.project,link.role)==(item['project'],item['role']))
            try:
                with link.host._profile_scope(link.pin.project):self._source(link)
            except Exception:
                item.update(state='source_or_profile_changed',source_verified=False,metadata_connection_verified=False)
        with self.store._transaction() as c:
            if self._bindings(c,token,projects)!=before:raise Conflict('worker authority changed')
            now=self.store._now()
            for item in items:
                if item.get('subscription') and item['subscription']['valid_until']<=now:
                    item['state']='observation_expired';item['metadata_connection_verified']=False
            from .agent_directory import _reject_known_tokens
            result={'items':items,'observed_at':now,'scope':'registered_native_metadata_only',
                    'account_wide_ui_control_verified':False,'models_started':0,'execution_authorized':False}
            _reject_known_tokens(c,result)
            return result
