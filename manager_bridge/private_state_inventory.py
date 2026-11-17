"""Metadata-only inventory of explicitly selected migration assets.

Does not recurse, read file contents, copy credentials, back up SQLite or rebind
restored authority. Available metadata is never a transfer/activation grant.
"""
import json
import os
from pathlib import Path
import stat
import time
from .host_preflight import _root

POLICY={
 'application_checkout':(False,'recreate_from_verified_pinned_source'),
 'authority_database':(True,'consistent_online_backup_then_quarantined_restore'),
 'control_credentials':(True,'owner_authorized_encrypted_handoff'),
 'canonical_subscription':(True,'encrypted_handoff_and_target_profile_reconciliation'),
 'worker_subscription':(True,'owner_reenrollment_with_new_filesystem_bindings'),
 'host_configuration':(True,'rewrite_paths_and_requalify_host'),
 'host_access_key':(True,'explicit_recipient_encrypted_handoff'),
 'trust_anchors':(False,'preserve_and_compare_with_trusted_provider_identity'),
 'recovery_keys':(True,'independent_key_custody_outside_backup_bundle'),
 'evidence_catalog':(True,'select_required_history_before_encrypted_handoff'),
}


def inspect_private_state(selection):
    if type(selection) is not dict or set(selection)-set(POLICY):raise ValueError('fixed migration asset categories required')
    if any(type(paths) not in (list,tuple) or len(paths)>200 for paths in selection.values()) or sum(len(p) for p in selection.values())>1000:
        raise ValueError('bounded explicit migration paths required')
    categories=[]
    for category,(private,action) in POLICY.items():
        paths=selection.get(category,());items=[];seen=set()
        for value in paths:
            if not isinstance(value,(str,Path)):raise ValueError('literal host paths required')
            path=Path(value)
            item={'path':str(path),'state':'unavailable'}
            try:
                if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)):raise ValueError()
                info=path.lstat();kind='file' if stat.S_ISREG(info.st_mode) else 'directory' if stat.S_ISDIR(info.st_mode) else None
                if kind is None or info.st_uid!=os.getuid() or (kind=='file' and info.st_nlink!=1):raise ValueError()
                mode=stat.S_IMODE(info.st_mode)
                if private and mode!=(0o600 if kind=='file' else 0o700):raise ValueError()
                identity=(info.st_dev,info.st_ino)
                if identity in seen:raise ValueError()
                seen.add(identity)
                item.update(state='metadata_available',kind=kind,mode=oct(mode),bytes=info.st_size if kind=='file' else None,
                            device=info.st_dev,inode=info.st_ino,modified_ns=info.st_mtime_ns)
            except (OSError,ValueError):pass
            items.append(item)
        categories.append({'category':category,'selected_count':len(paths),'items':items,
            'state':'unconfigured' if not paths else 'attention_required' if any(i['state']!='metadata_available' for i in items) else 'metadata_available',
            'required_action':action,'private':private,'contents_inspected':False})
    return {'scope':'explicit_selected_path_metadata_only','observed_at':time.time(),'categories':categories,
        'category_count':len(categories),'selected_path_count':sum(c['selected_count'] for c in categories),
        'unconfigured_categories':[c['category'] for c in categories if c['state']=='unconfigured'],
        'whole_workspace_inventory_verified':False,'complete_backup_verified':False,'credentials_copied':False,
        'target_filesystem_bindings_reconciled':False,'target_host_qualified':False,
        'environment_replacement_authorized':False,'execution_authorized':False}


def public_summary(report):
    return {'scope':report['scope'],'category_count':report['category_count'],
        'selected_path_count':report['selected_path_count'],
        'categories':[{'category':c['category'],'state':c['state'],'selected_count':c['selected_count'],'required_action':c['required_action']} for c in report['categories']],
        'complete_backup_verified':False,'environment_replacement_authorized':False}


def write_private_inventory(report,destination):
    path=Path(destination)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('physical absolute private report path required')
    parent=_root(path.parent)
    encoded=json.dumps(report,ensure_ascii=False,allow_nan=False).encode()
    if len(encoded)>2*1024*1024:raise ValueError('bounded metadata report required')
    directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        fd=os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'wb') as out:os.fchmod(out.fileno(),0o600);out.write(encoded);out.flush();os.fsync(out.fileno())
        os.fsync(directory)
    finally:os.close(directory)
