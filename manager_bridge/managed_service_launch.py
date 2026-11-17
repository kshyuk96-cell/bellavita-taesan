"""Fixed native subscription bootstrap from private local configuration.

No dynamic imports, callback names, tokens, endpoints or enrollment inputs are
accepted. Creating the refresher does not invoke it or start a model.
"""
from pathlib import Path
from .continuous_host import _private_json, _private_file
from .fleet_codex import CodexHost, ProjectBinding
from .governance_service import load_service
from .managed_subscription_runtime import ManagedSubscriptionRuntime
from .subscription_broker import SubscriptionTarget, CodexSubscriptionRefresher, _physical


def load_managed_service(service_file, subscription_file):
    service_config=_private_json(service_file)
    subscription=_private_json(subscription_file)
    if type(service_config.get('governance')) is not dict or service_config['governance']:
        raise ValueError('managed launch requires registered governance admission')
    fields={'version','broker_ref','canonical_home','owner_token_file'}
    if (set(subscription)!=fields or type(subscription['version']) is not int
            or subscription['version']!=1):
        raise ValueError('exact private managed launch schema required')
    if (type(service_config.get('supervision')) is not dict
            or service_config['supervision'].get('subscription_interval') is None):
        raise ValueError('explicit scheduled native subscription refresh required')
    host_file=_private_file(service_config['host_config_file'])
    config=_private_json(host_file)
    entries=config.get('projects')
    if type(entries) is not list or not 1<=len(entries)<=100:
        raise ValueError('explicit managed host project scope required')
    canonical=_physical(subscription['canonical_home'])
    owner=_private_file(subscription['owner_token_file'])
    # Sources and candidate directories are made available to coding workers;
    # the independent owner credential and canonical profile must stay outside.
    for entry in entries:
        for field in ('source','candidates','runtime'):
            workspace=_physical(entry[field])
            if (canonical==workspace or workspace in canonical.parents or canonical in workspace.parents
                    or owner==workspace or workspace in owner.parents):
                raise ValueError('canonical credentials must be separate from worker workspaces')
    application=_physical(config['application_root'])
    if (canonical==application or application in canonical.parents or canonical in application.parents
            or application in owner.parents):
        raise ValueError('canonical credentials must be separate from application source')
    targets=[]
    for entry in entries:
        for role in ('developer','reviewer'):
            targets.append(SubscriptionTarget(entry['project'],entry[role+'_id'],role,
                                              Path(entry[role]['codex_home'])))
    first=entries[0]
    native=CodexHost(first['developer']['binary'],canonical,{
        first['project']:ProjectBinding(first['project'],first['repository'],Path(first['source']),
                                       require_origin=first['developer'].get('require_origin',True))})
    runtime=ManagedSubscriptionRuntime(broker_ref=subscription['broker_ref'],canonical_home=canonical,
        targets=targets,owner_token_file=owner,
        refresh_callback=CodexSubscriptionRefresher(native,first['project']))
    service=load_service(service_file,subscription_runtime=runtime)
    try:
        if (_private_json(service_file)!=service_config or _private_json(host_file)!=config
                or _private_json(subscription_file)!=subscription):
            raise ValueError('managed launch configuration changed while loading')
    except BaseException:
        service.close()
        raise
    return service
