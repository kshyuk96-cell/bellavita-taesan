"""Read-only migration inventory; source freshness is never execution authority.

Inputs are fixed host-side project bindings and typed provider observations.
No provider calls, source synchronization, worker launch or approval occurs here.
"""
from dataclasses import asdict
import math
from pathlib import Path
import subprocess
import time
from .connection_readiness import SourcePin
from .continuous_host import _guard_git_filters
from .maintenance_registry import configuration
from .source_rehearsal import observe_checkout
from .source_watch import SourceHead
from .store import Conflict


def inspect_handoff(config, source, baseline, upstream=None, *, clock=time.time, max_age=300):
    config=configuration(config)
    if not isinstance(baseline,SourcePin):raise ValueError('typed source baseline required')
    binding=tuple(config[k] for k in ('source_kind','source_ref','branch'))
    if binding!=(baseline.source_kind,baseline.source_ref,baseline.branch):
        raise Conflict('handoff baseline belongs to another registered source')
    if type(max_age) is not int or not 1<=max_age<=3600:raise ValueError('bounded observation age required')
    now=clock()
    if type(now) not in (int,float) or not math.isfinite(now) or now<=0:raise ValueError('actual observation clock required')
    if upstream is not None:
        if not isinstance(upstream,SourceHead):raise ValueError('typed upstream metadata required')
        if (upstream.project,upstream.source_kind,upstream.source_ref,upstream.branch)!=(baseline.project,*binding):
            raise Conflict('upstream observation belongs to another source')
    local=None;state='source_unavailable'
    try:
        path=Path(source).absolute()
        if '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)) or not path.is_dir():
            raise ValueError('physical source directory required')
        _guard_git_filters(source)
        local=observe_checkout(source)
    except (OSError,ValueError,Conflict,subprocess.SubprocessError):pass
    if local:
        if not local['tracked_and_visible_untracked_clean']:state='uncommitted_changes_preserved'
        elif local['sha']!=baseline.sha:state='local_baseline_changed'
        elif upstream is None:state='upstream_unmeasured'
        elif not 0<=now-upstream.observed_at<=max_age:state='upstream_stale'
        elif upstream.sha!=local['sha']:state='upstream_changed'
        elif upstream.revision!=baseline.revision:state='revision_changed'
        else:state='source_aligned'
    actions={
        'source_unavailable':'locate_registered_source',
        'uncommitted_changes_preserved':'capture_pending_changes_without_reset',
        'local_baseline_changed':'reconcile_local_baseline',
        'upstream_unmeasured':'observe_remote_head',
        'upstream_stale':'refresh_remote_observation',
        'upstream_changed':'import_new_source_into_separate_worktree',
        'revision_changed':'reconcile_saved_version',
        'source_aligned':'verify_requirements_runtime_worker_and_qa_contract',
    }
    return {'project':baseline.project,'name':config['name'],'state':state,
        'baseline':asdict(baseline),'local_sha':local['sha'] if local else None,
        'local_clean':local['tracked_and_visible_untracked_clean'] if local else None,
        'upstream_sha':upstream.sha if upstream else None,
        'upstream_revision':upstream.revision if upstream else None,
        'upstream_observed_at':upstream.observed_at if upstream else None,
        'source_ready_for_handoff':state=='source_aligned','next_action':actions[state],
        'qa_contract':'not_verified','requirements_handoff':'not_verified',
        'worker_connection':'not_verified','persistent_cloud_host':'not_verified',
        'execution_ready':False,'approval_granted':False,'source_modified':False,
        'tests_executed':0,'operational_change':False,'checked_at':now}
