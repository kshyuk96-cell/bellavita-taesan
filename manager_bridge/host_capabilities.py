"""Local host measurements, never remote claims or deployment authorization."""
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from .project_runtime import BubblewrapTests


def inspect_host(*,expected_service_id=None,state_root=None,engine=None):
    if expected_service_id is not None and (type(expected_service_id) is not str or not re.fullmatch(r'srv-[A-Za-z0-9_-]{1,100}',expected_service_id)):
        raise ValueError('exact expected Render service required')
    observed=os.environ.get('RENDER_SERVICE_ID')
    if type(observed) is not str or not re.fullmatch(r'srv-[A-Za-z0-9_-]{1,100}',observed):observed=None
    # A requested target ID cannot attest the host on which this process runs.
    exact_target=bool(observed and expected_service_id==observed)
    sandbox=engine if engine is not None else BubblewrapTests()
    try:
        raw=sandbox.probe()
        isolated=type(raw) is dict and raw.get('isolation_verified') is True
    except Exception:isolated=False
    storage={'configured':False,'private_writable_fsync_verified':False,'persistent_across_restart_verified':False}
    if state_root is not None:
        root=Path(state_root)
        if not root.is_absolute() or '..' in root.parts or any(p.is_symlink() for p in (root,*root.parents)):
            raise ValueError('physical absolute state directory required')
        info=root.stat()
        if not root.is_dir() or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError('owned private state directory required')
        with tempfile.TemporaryDirectory(prefix='capability-',dir=root) as d:
            p=Path(d)/'fsync-probe'
            fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'wb') as f:
                f.write(b'taesan-host-probe\n');f.flush();os.fsync(f.fileno())
            if p.read_bytes()!=b'taesan-host-probe\n':raise ValueError('state write/read mismatch')
        storage.update(configured=True,private_writable_fsync_verified=True,
                       available_bytes=shutil.disk_usage(root).free)
    return {'scope':'local_host_measurement','expected_service_id':expected_service_id,
            'observed_service_id':observed,'exact_target_host_verified':exact_target,
            'namespace_isolation_verified':isolated,'storage':storage,
            'deployment_authorized':False,'always_on_verified':False,
            'remote_execution_performed':False,'observed_at':time.time()}
