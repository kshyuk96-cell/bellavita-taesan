"""Trusted local wiring of native guards, one broker and scheduled supervision.

No HTTP/config-provided callbacks, login, enrollment, profile copying or model
execution occurs here. Callbacks must be installed by the host bootstrap.
"""
from contextlib import contextmanager
from .continuous_host import _private_file, _token
from .store import Conflict
from .subscription_broker import SubscriptionBroker, SubscriptionTarget, _physical


class ManagedSubscriptionRuntime:
    def __init__(self, *, broker_ref, canonical_home, targets, owner_token_file,
                 refresh_callback):
        if (type(targets) not in (tuple,list) or not targets or len(targets)>200
                or any(type(t) is not SubscriptionTarget for t in targets)
                or not callable(refresh_callback)):
            raise ValueError('explicit registered native subscription runtime required')
        self.ref=broker_ref
        self.home=_physical(canonical_home)
        self.targets=tuple(targets)
        self.owner_file=_private_file(owner_token_file)
        self.refresh_callback=refresh_callback
        self._broker=None
        self._guards={}
        self._callbacks={}
        if len({(t.project,t.member_id) for t in targets})!=len(targets):
            raise ValueError('unique managed targets required')

    def guard_factory(self,role,project,member_id,home,token_file):
        if self._broker is not None:
            raise Conflict('managed runtime already bound')
        matches=[t for t in self.targets if (t.project,t.member_id,t.role)==(project,member_id,role)]
        if len(matches)!=1 or _physical(home)!=matches[0].home:
            raise ValueError('native host profile differs from managed target')
        key=(project,member_id)
        if key in self._guards:
            raise ValueError('duplicate native profile guard')
        token_path=_private_file(token_file)
        self._guards[key]=(role,token_path)
        @contextmanager
        def guard(requested_project):
            if requested_project!=project:
                raise ValueError('profile guard outside registered project')
            broker=self._broker
            if broker is None:
                raise Conflict('managed subscription runtime not bound')
            # Re-read and authenticate the current worker credential on every use.
            token=self._worker_token(broker.core,project,member_id,role,token_path)
            with broker.profile_guard(token,project,member_id) as value:
                yield value
        self._callbacks[key]=guard
        return guard

    def bind(self,host,settings,supervision):
        if self._broker is not None:
            raise Conflict('managed runtime cannot be rebound')
        if supervision is None or supervision.subscription_interval is None:
            raise ValueError('explicit subscription supervision schedule required')
        expected={(t.project,t.member_id) for t in self.targets}
        if (set(self._guards)!=expected or set(settings['projects'])!={t.project for t in self.targets}
                or len(settings['projects'])!=len(set(settings['projects']))):
            raise ValueError('exact host and broker target scope required')
        # Every configured project needs distinct developer and reviewer profiles.
        for project in settings['projects']:
            if sorted(t.role for t in self.targets if t.project==project)!=['developer','reviewer']:
                raise ValueError('exact developer and reviewer targets required')
        for (project,member_id),(role,path) in self._guards.items():
            self._worker_token(host.core,project,member_id,role,path)
        broker=SubscriptionBroker(host.core,self.ref,self.home,self.targets,
                                  refresh_callback=self.refresh_callback)
        with host.store._transaction() as c:
            broker._auth(c,self.owner_token(),owner=True)
        self._broker=broker
        return broker

    def make_auth_observer(self,host,host_config):
        from pathlib import Path
        from .dual_role_admission import DualRoleSubscriptionAdmission
        from .fleet_codex import CodexHost,ProjectBinding
        from .subscription_observations import NativeSubscriptionObserver
        if self._broker is None or self._broker.core is not host.core:
            raise Conflict('bound same-core subscription runtime required')
        observers={};seen=set()
        for entry in host_config['projects']:
            project=entry['project']
            if project in observers:raise ValueError('duplicate authentication project')
            roles={}
            for role in ('developer','reviewer'):
                key=(project,entry[role+'_id'])
                target=next((t for t in self.targets if (t.project,t.member_id)==key and t.role==role),None)
                if target is None or _physical(entry[role]['codex_home'])!=target.home:
                    raise ValueError('exact registered metadata profile required')
                setting=entry[role]
                native=CodexHost(setting['binary'],target.home,{
                    project:ProjectBinding(project,entry['repository'],Path(entry['source']),
                        require_origin=setting.get('require_origin',True))},profile_guard=self._callbacks[key])
                roles[role]=NativeSubscriptionObserver(host.core,native,target.member_id)
                seen.add(key)
            observers[project]=roles
        if seen!={(t.project,t.member_id) for t in self.targets}:raise ValueError('exact authentication observer scope required')
        return DualRoleSubscriptionAdmission(host.core,observers,host.manager_token)

    @staticmethod
    def _worker_token(core,project,member_id,role,path):
        token=_token(path)
        with core.store._transaction() as c:
            actor=core.store._authenticate(c,token)
            config=core._config(c,token,project)
            member=core._member(c,project,member_id,role,config)
            if actor['role']!='worker' or actor['token_hash']!=member['credential_hash']:
                raise ValueError('exact registered worker credential required')
        return token

    def owner_token(self):
        return _token(self.owner_file)
