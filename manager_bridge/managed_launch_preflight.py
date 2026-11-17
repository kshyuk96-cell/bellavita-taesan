"""Ten read-only local configuration checks; never admission or auth proof."""
import math
import os
from pathlib import Path
import stat
import time
from .continuous_host import _private_json, _private_file, _physical, _git
from .governance_service import _listener
from .governance_supervision import SupervisionSettings
from .maintenance_registry import name
from .store import _sha
from .subscription_broker import _physical as private_directory

CHECKS=('private_configuration','schedule','project_scope','role_pairs',
        'profile_directories','credential_file_metadata','workspace_separation',
        'native_executables','application_pin','state_storage')


def inspect_managed_launch(service_file,subscription_file):
    results=[];service=host=sub=None;host_file=None
    def check(key,operation):
        try:
            operation()
            results.append({'check':key,'state':'passed'})
        except Exception:
            results.append({'check':key,'state':'blocked','reason':key+'_unavailable_or_invalid'})
    def configuration():
        nonlocal service,host,sub,host_file
        service=_private_json(service_file);sub=_private_json(subscription_file)
        allowed={'version','host_config_file','listen','governance','supervision','local_host_admission'}
        if (set(service)-allowed or type(service.get('version')) is not int or service['version']!=1
                or set(sub)!={'version','broker_ref','canonical_home','owner_token_file'}
                or type(sub['version']) is not int or sub['version']!=1):raise ValueError()
        if type(service.get('governance')) is not dict or service['governance']:raise ValueError()
        name(sub['broker_ref']);host_file=_private_file(service['host_config_file']);host=_private_json(host_file)
        required={'version','application_root','expected_source_commit','data_root','database','manager_token_file','instance_id','projects'}
        if (not required<=set(host) or set(host)-required-{'poll_seconds','reference_materials'}
                or type(host['version']) is not int or host['version']!=1):raise ValueError()
        name(host['instance_id'])
        for key in ('governance','local_host_admission'):
            if key in service and (type(service[key]) is not dict or service[key]):raise ValueError()
        if 'local_host_admission' in service and 'governance' not in service:raise ValueError()
    check(CHECKS[0],configuration)
    if results[0]['state']!='passed':
        results.extend({'check':key,'state':'blocked','reason':'configuration_dependency_unavailable'} for key in CHECKS[1:])
        return _report(results,False)
    entries=host['projects']
    def schedule():
        setting=SupervisionSettings(**service['supervision'])
        if setting.subscription_interval is None:raise ValueError()
        poll=host.get('poll_seconds',2)
        if type(poll) not in (int,float) or not math.isfinite(poll) or not 1<=poll<=60:raise ValueError()
        _listener(service.get('listen',{}))
    def scope():
        if type(entries) is not list or not 1<=len(entries)<=100:raise ValueError()
        required={'project','repository','source','candidates','runtime','developer_id','reviewer_id','developer_token_file','reviewer_token_file','checks','developer','reviewer'}
        for entry in entries:
            if type(entry) is not dict or not required<=set(entry) or set(entry)-required-{'session_id','fleet_template','fleet_host_ref','worker_mode'}:raise ValueError()
        projects=[name(e['project']) for e in entries]
        if len(set(projects))!=len(projects):raise ValueError()
    def roles():
        scope()
        for e in entries:
            if name(e['developer_id'])==name(e['reviewer_id']):raise ValueError()
            for role in ('developer','reviewer'):
                setting=e[role]
                if (type(setting) is not dict or not {'binary','codex_home'}<=set(setting)
                        or set(setting)-{'binary','codex_home','require_origin','execution_timeout'}):raise ValueError()
    def profiles():
        scope()
        homes=[private_directory(sub['canonical_home'])]
        homes.extend(private_directory(e[role]['codex_home']) for e in entries for role in ('developer','reviewer'))
        for index,a in enumerate(homes):
            for b in homes[index+1:]:
                if a==b or a in b.parents or b in a.parents:raise ValueError()
    def credentials():
        scope()
        paths=[sub['owner_token_file'],host['manager_token_file']]
        paths.extend(e[role+'_token_file'] for e in entries for role in ('developer','reviewer'))
        homes=[sub['canonical_home']]+[e[role]['codex_home'] for e in entries for role in ('developer','reviewer')]
        paths.extend(str(Path(h)/'auth.json') for h in homes)
        seen=set()
        for value in paths:
            p=_private_file(value);s=p.stat()
            if (stat.S_IMODE(s.st_mode)!=0o600 or s.st_nlink!=1 or not 0<s.st_size<=65536
                    or (s.st_dev,s.st_ino) in seen):raise ValueError()
            seen.add((s.st_dev,s.st_ino))
    def separation():
        scope()
        protected=[Path(sub['canonical_home']).absolute(),Path(sub['owner_token_file']).absolute(),
                   Path(host['manager_token_file']).absolute(),Path(host['database']).absolute(),
                   Path(service_file).absolute(),Path(subscription_file).absolute(),host_file]
        protected.extend(Path(e[role]['codex_home']).absolute() for e in entries for role in ('developer','reviewer'))
        protected.extend(Path(e[role+'_token_file']).absolute() for e in entries for role in ('developer','reviewer'))
        exposed=[_physical(e[k]) for e in entries for k in ('source','candidates','runtime')]
        exposed.append(_physical(host['application_root']))
        for a in exposed:
            for b in protected:
                if a==b or a in b.parents:raise ValueError()
        canonical=private_directory(sub['canonical_home'])
        if any(canonical in p.parents for p in exposed):raise ValueError()
    def executables():
        roles()
        for e in entries:
            for role in ('developer','reviewer'):
                p=Path(e[role]['binary'])
                if not p.is_absolute() or '..' in p.parts or not p.is_file() or not os.access(p,os.X_OK):raise ValueError()
    def application():
        root=_physical(host['application_root']);_sha(host['expected_source_commit'])
        if root!=Path(__file__).resolve().parents[1]:raise ValueError()
        if _git(root,'rev-parse','HEAD').decode().strip()!=host['expected_source_commit'] or _git(root,'--no-optional-locks','status','--porcelain'):raise ValueError()
    def storage():
        directory=private_directory(host['data_root']);database=_private_file(host['database']);s=database.stat()
        if (database.parent!=directory or s.st_nlink!=1 or stat.S_IMODE(s.st_mode)!=0o600 or s.st_size<=0):raise ValueError()
        protected=[sub['owner_token_file'],host['manager_token_file'],service_file,subscription_file,host_file]
        protected.extend(e[role+'_token_file'] for e in entries for role in ('developer','reviewer'))
        if any(database==Path(p).absolute() for p in protected):raise ValueError()
        homes=[private_directory(sub['canonical_home'])]
        homes.extend(private_directory(e[role]['codex_home']) for e in entries for role in ('developer','reviewer'))
        if any(h==database or h in database.parents for h in homes):raise ValueError()
    for key,operation in zip(CHECKS[1:],(schedule,scope,roles,profiles,credentials,separation,executables,application,storage)):
        check(key,operation)
    try:
        stable=(_private_json(service_file)==service and _private_json(host_file)==host
                and _private_json(subscription_file)==sub)
    except Exception:stable=False
    return _report(results,stable)


def _report(results,stable):
    return {'scope':'local_managed_configuration_only','observed_at':time.time(),
            'checks':results,'passed_checks':sum(r['state']=='passed' for r in results),
            'required_checks':len(CHECKS),'configuration_stable':stable,
            'configuration_preflight_passed':stable and all(r['state']=='passed' for r in results),
            'credential_file_contents_inspected':False,'authentication_verified':False,
            'database_contents_read':False,'database_durability_verified':False,
            'registry_membership_verified':False,'native_binary_identity_verified':False,
            'source_identity_verified':False,'namespace_isolation_verified':False,'scheduler_running_verified':False,
            'always_on_verified':False,'execution_authorized':False}
