"""Read-only, exact local source snapshots for preparing connection reviews.

This is NOT a sandbox executor and never executes project commands, fetches Git,
loads credential files, or treats source-copy tests as operational acceptance.
"""
from pathlib import Path
import re
import subprocess
from .connection_readiness import ConnectionFact,HostProfile,SourcePin
from .store import Conflict


def observe_checkout(path):
    root=Path(path).absolute()
    if '..' in root.parts or any(p.is_symlink() for p in (root,*root.parents)) or not root.is_dir():
        raise ValueError('registered non-symlink source directory required')
    env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1',
         'GIT_TERMINAL_PROMPT':'0','GIT_NO_LAZY_FETCH':'1','GIT_OPTIONAL_LOCKS':'0','GIT_ALLOW_PROTOCOL':''}
    def git(args):
        result=subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false','-c','core.untrackedCache=false','-C',str(root),*args],
            env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15)
        if result.returncode or len(result.stdout)>1000000:raise ValueError('source observation failed')
        return result.stdout.decode().strip()
    if git(['rev-parse','--show-toplevel'])!=str(root):raise Conflict('exact repository root required')
    sha=git(['rev-parse','HEAD'])
    if not re.fullmatch('[a-f0-9]{40}',sha):raise ValueError('exact source head required')
    clean=not git(['status','--porcelain','--untracked-files=all'])
    return {'sha':sha,'tracked_and_visible_untracked_clean':clean,'ignored_dependencies_audited':False,
            'remote_rechecked':False,'security_sandbox_verified':False}


def snapshot_profile(record,checkout):
    """Trusted local review artifact plus fresh local HEAD; not a live provider adapter."""
    pin=SourcePin(**record['pin'])
    root=Path(checkout)
    initial=observe_checkout(root)
    if initial['sha']!=pin.sha or not initial['tracked_and_visible_untracked_clean']:
        raise Conflict('review artifact and checkout differ')
    facts=record['fixture_tests']
    if type(facts) is not dict or set(facts)!={'passed','failed','skipped'}:
        raise ValueError('explicit fixture results required')
    observed=record['observed_at']
    def observe(project,capability):
        current=observe_checkout(root)
        if project!=pin.project or current['sha']!=pin.sha or not current['tracked_and_visible_untracked_clean']:
            raise Conflict('source snapshot changed')
        ref='source-snapshot/'+project+'/'+pin.sha[:12]
        if capability=='source_snapshot':return ConnectionFact(project,capability,pin.sha,'verified',ref,observed)
        if capability=='fixture_tests':
            state='partial' if facts['failed'] or facts['skipped'] else 'verified'
            return ConnectionFact(project,capability,pin.sha,state,ref+'/tests',observed,**facts)
        if capability=='build':return ConnectionFact(project,capability,pin.sha,record['build'],ref+'/build',observed)
        # No artifact, source path, or test result can attest these real connections.
        return None
    return HostProfile(pin,observe)
