"""Trusted host-loader bridge; tokens stay in private role files."""
from pathlib import Path
from dataclasses import replace
from .subscription_broker import SubscriptionBroker
from .continuous_host import _private_file
from .control import secret
from .store import PermissionDenied


def profile_guard_factory(broker):
    if not isinstance(broker,SubscriptionBroker):raise ValueError('registered subscription broker required')

    def bind(role,project,member_id,home,token_file):
        matches=[t for t in broker.targets if (t.project,t.member_id,t.role)==(project,member_id,role)]
        if len(matches)!=1 or Path(home).absolute()!=matches[0].home:
            raise PermissionDenied('exact registered subscription profile required')
        path=_private_file(token_file)

        def authenticate():
            token=secret(_private_file(path))
            with broker.store._transaction() as c:
                actor=broker.store._authenticate(c,token)
                config=broker.core._config(c,token,project)
                member=broker.core._member(c,project,member_id,role,config)
                if actor['role']!='worker' or actor['token_hash']!=member['credential_hash']:
                    raise PermissionDenied('exact private worker credential required')
            return token

        authenticate()
        def guard(selected_project):
            if selected_project!=project:raise PermissionDenied('subscription guard project changed')
            return broker.profile_guard(authenticate(),project,member_id)
        return guard

    return bind


def attach_profile_guards(host,broker):
    """Attach after load_host and before serving or dispatch; no refresh occurs."""
    from .fleet_codex import CodexHost
    if not isinstance(broker,SubscriptionBroker) or host.core is not broker.core:
        raise ValueError('host and subscription broker must share the exact core')
    projects=host.projects
    if set(projects)!={target.project for target in broker.targets}:
        raise ValueError('subscription target projects must match host projects')
    if getattr(host,'subscription_broker_ref',None) is not None:
        raise ValueError('host subscription guards already configured')
    factory=profile_guard_factory(broker);prepared=[]
    for project,entry in projects.items():
        if not isinstance(entry.developer_transport,CodexHost):
            raise ValueError('registered native developer transport required')
        developer=factory('developer',project,entry.developer_id,entry.developer_transport.home,entry.developer_token_file)
        matches=[t for t in broker.targets if (t.project,t.member_id,t.role)==(project,entry.reviewer_id,'reviewer')]
        if len(matches)!=1:raise ValueError('registered native reviewer target required')
        reviewer=factory('reviewer',project,entry.reviewer_id,matches[0].home,entry.reviewer_token_file)
        original=entry.reviewer_factory
        def wrapped(frozen,*,heartbeat,original=original,reviewer=reviewer,home=matches[0].home):
            transport=original(frozen,heartbeat=heartbeat)
            if not isinstance(transport,CodexHost) or transport.home!=home:
                raise PermissionDenied('review transport differs from subscription target')
            transport.profile_guard=reviewer
            return transport
        prepared.append((project,entry,developer,replace(entry,reviewer_factory=wrapped)))
    # Preflight all scopes, role credentials and homes before changing transports.
    for project,entry,developer,replacement in prepared:
        entry.developer_transport.profile_guard=developer
        projects[project]=replacement
    host.subscription_broker_ref=broker.ref
    return {'broker_ref':broker.ref,'target_profiles':len(prepared)*2,'refresh_performed':False,'models_started':0}
