"""Bounded local host evidence. No remote attestation, signals, or deployment."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from types import SimpleNamespace
from .host_capabilities import inspect_host
from .fleet_codex import CodexHost

MAX_CHALLENGE_BYTES=16384
CHALLENGE_FILE='process-restart-challenge.json'


def local_host_identity():
    """Observe kernel host/boot values, independently of any requested identity."""
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    host=Path('/proc/sys/kernel/hostname').read_text().strip()
    if not re.fullmatch('[a-f0-9-]{36}',boot) or not host or len(host)>255:
        raise ValueError('bounded local kernel host identity unavailable')
    return hashlib.sha256(json.dumps([boot,host],separators=(',',':')).encode()).hexdigest()


def _expected(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):
        raise ValueError('explicit expected local host fingerprint required')
    return value


def _root(value):
    root=Path(value)
    if not root.is_absolute() or '..' in root.parts or any(p.is_symlink() for p in (root,*root.parents)):
        raise ValueError('physical absolute private state root required')
    info=root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError('owned mode0700 state root required')
    return root


def _identity():
    return CodexHost._process_identity(SimpleNamespace(pid=os.getpid()))


def inspect_preflight(*,expected_host_identity,state_root,expected_service_id=None,engine=None):
    expected=_expected(expected_host_identity);root=_root(state_root)
    result=inspect_host(expected_service_id=expected_service_id,state_root=root,engine=engine)
    observed=local_host_identity()
    result.update(expected_local_host_identity=expected,observed_local_host_identity=observed,
                  exact_expected_local_host_verified=observed==expected,
                  infrastructure_persistence_verified=False,process_restart_persistence_verified=False)
    # The generic capability probe's optional service environment is not remote attestation.
    result['remote_host_attestation_verified']=False
    return result


def _write(root,value):
    path=root/CHALLENGE_FILE
    if path.exists() or path.is_symlink():raise ValueError('existing restart challenge must be preserved')
    content=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    if len(content)>MAX_CHALLENGE_BYTES:raise ValueError('restart challenge exceeds bound')
    fd,temp=tempfile.mkstemp(prefix='.restart-',dir=root)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        # Hard-link publishes without overwriting an existing challenge.
        os.link(temp,path,follow_symlinks=False)
        Path(temp).unlink()
        directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)
    return path


def prepare_restart_challenge(*,expected_host_identity,state_root):
    expected=_expected(expected_host_identity);root=_root(state_root)
    if local_host_identity()!=expected:raise ValueError('expected local host differs from observed host')
    body={'version':1,'host_identity':expected,'writer':_identity(),'nonce':secrets.token_hex(32),'prepared_at':time.time()}
    encoded=json.dumps(body,sort_keys=True,separators=(',',':')).encode()
    fingerprint=hashlib.sha256(encoded).hexdigest()
    _write(root,dict(body,challenge_digest=fingerprint))
    return {'scope':'local_process_restart_challenge','challenge_digest':fingerprint,
            'prepared':True,'private_file_fsync_verified':True,'infrastructure_persistence_verified':False}


def _read(root):
    path=root/CHALLENGE_FILE
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or info.st_size>MAX_CHALLENGE_BYTES:
            raise ValueError('owned bounded mode0600 challenge required')
        value=json.loads(stream.read(MAX_CHALLENGE_BYTES+1))
    if type(value) is not dict or set(value)!={'version','host_identity','writer','nonce','prepared_at','challenge_digest'} or value['version']!=1:
        raise ValueError('exact restart challenge schema required')
    unsigned={k:v for k,v in value.items() if k!='challenge_digest'}
    if hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=value['challenge_digest']:
        raise ValueError('restart challenge integrity mismatch')
    return value


def verify_restart_challenge(*,expected_host_identity,state_root):
    expected=_expected(expected_host_identity);root=_root(state_root);value=_read(root)
    if local_host_identity()!=expected or value['host_identity']!=expected:
        raise ValueError('restart challenge host binding differs')
    current=_identity();writer=value['writer']
    if type(writer) is not dict or set(writer)!=set(current):raise ValueError('exact writer identity required')
    separate=any(writer[k]!=current[k] for k in ('boot_id','pid','start_ticks'))
    same_boot=writer['boot_id']==current['boot_id']
    if not separate:raise ValueError('verification requires a separate process')
    return {'scope':'local_process_restart_challenge','challenge_digest':value['challenge_digest'],
            'separate_process_read_verified':True,'same_kernel_boot_verified':same_boot,
            'process_restart_persistence_verified':False,
            'infrastructure_persistence_verified':False,'writer_stop_verified':False,
            'note':'Separate process read only; CLI restart-proof also waits for both child exits.'}


class RegisteredWorkerObservations:
    """Trusted host handles only; no PID selector, signal, or recovery authority."""
    def __init__(self):self._workers={}

    def register(self,worker_id,process):
        from .maintenance_registry import name
        import subprocess
        name(worker_id)
        if not isinstance(process,subprocess.Popen):raise ValueError('trusted spawned process handle required')
        if worker_id in self._workers:raise ValueError('worker identity already registered')
        if process.poll() is not None:raise ValueError('live process required at registration')
        self._workers[worker_id]=(process,CodexHost._process_identity(process))

    def observe(self,worker_id):
        if worker_id not in self._workers:raise ValueError('registered worker required')
        process,identity=self._workers[worker_id]
        try:
            local_host_identity()  # Kernel identity must remain measurable.
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            raw=CodexHost._stop_observation(identity) if boot==identity['boot_id'] else None
            if raw is not None and not raw['exact_process_gone'] and process.poll() is None and CodexHost._process_identity(process)!=identity:raw=None
        except Exception:raw=None
        exited=process.poll()
        state=('unknown' if raw is None else 'pid_reused' if raw['pid_reused'] else
               'exit_observed' if exited is not None else 'unobserved' if raw['exact_process_gone'] else 'running')
        return {'worker_id':worker_id,'state':state,'process_exit_observed':exited is not None,
                'exit_code':exited,'stopped_verified':state=='exit_observed',
                'descendants_verified':False,'safe_for_recovery':False,'signals_sent':False}
