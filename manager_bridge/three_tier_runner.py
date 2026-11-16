"""Host-registered independent checks over a frozen tracked Git snapshot.

No commands, paths, credentials, callbacks or success flags come from HTTP task
text. The production engine requires real network/PID namespace isolation and
never falls back to a plain subprocess. Test counts are deliberately not parsed
from repository output. A successful command is evidence for that command only.
"""
from dataclasses import dataclass
import hashlib
import io
import math
import os
from pathlib import Path
import resource
import signal
import subprocess
import tarfile
import tempfile
import time

from .project_runtime import BubblewrapTests, _directory
from .source_rehearsal import observe_checkout
from .store import Conflict, PermissionDenied, _project, _sha, digest

MAX_ARCHIVE = 64 * 1024 * 1024
# Trusted host policy only; never a task/HTTP-selected archive allowance.
MAX_RUNTIME_BYTES = 256 * 1024 * 1024
MAX_FILES = 50000


class ReviewUnavailable(Conflict):
    """No independent pass may be recorded for an unavailable execution host."""


def _git_env():
    return {'PATH':'/usr/bin:/bin', 'HOME':'/nonexistent',
            'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1',
            'GIT_TERMINAL_PROMPT':'0','GIT_NO_LAZY_FETCH':'1',
            'GIT_OPTIONAL_LOCKS':'0','GIT_ALLOW_PROTOCOL':'','GIT_NO_REPLACE_OBJECTS':'1'}


def _archive_limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0,0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_ARCHIVE,MAX_ARCHIVE))


def _archive(profile):
    """Read only a committed, clean tracked tree; never copy host Git metadata."""
    before=observe_checkout(profile.source)
    if before['sha']!=profile.head_sha or not before['tracked_and_visible_untracked_clean']:
        raise Conflict('review source changed')
    ancestor=subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','-C',str(profile.source),
        'merge-base','--is-ancestor',profile.base_sha,profile.head_sha],env=_git_env(),stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
    if ancestor.returncode:raise Conflict('candidate does not extend registered base')
    with tempfile.TemporaryDirectory(prefix='taesan-review-archive-') as folder:
        target=Path(folder)/'source.tar'
        result=subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null',
            '-c','core.fsmonitor=false','-C',str(profile.source),'archive','--format=tar',
            '--output='+str(target),profile.head_sha],env=_git_env(),stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30,preexec_fn=_archive_limits)
        if result.returncode or not target.is_file() or target.stat().st_size>MAX_ARCHIVE:
            raise ReviewUnavailable('bounded frozen source unavailable')
        content=target.read_bytes()
    listing=subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','-C',str(profile.source),
        'ls-tree','-rz',profile.head_sha],env=_git_env(),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,timeout=10)
    if listing.returncode or len(listing.stdout)>8*1024*1024:raise ReviewUnavailable('bounded tracked input list required')
    tracked={}
    for raw in listing.stdout.split(b'\0'):
        if not raw:continue
        meta,path=raw.split(b'\t',1);mode,kind,oid=meta.split(b' ')
        if mode not in (b'100644',b'100755') or kind!=b'blob':raise ReviewUnavailable('tracked source links forbidden')
        decoded=path.decode('utf-8')
        if 'node_modules' in Path(decoded).parts:raise ReviewUnavailable('tracked candidate dependencies forbidden')
        tracked[decoded]=oid.decode('ascii')
    if len(tracked)>MAX_FILES:raise ReviewUnavailable('bounded tracked input count required')
    with tarfile.open(fileobj=io.BytesIO(content),mode='r:') as archive:
        files={m.name:m for m in archive.getmembers() if m.isfile()}
        if set(files)!=set(tracked):raise ReviewUnavailable('archive omits tracked candidate files')
        for path,item in files.items():
            data=archive.extractfile(item).read()
            actual=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            if actual!=tracked[path]:raise ReviewUnavailable('archive changes tracked candidate contents')
    after=observe_checkout(profile.source)
    if before!=after:raise Conflict('review source changed while archiving')
    return content


def _extract(content,destination):
    root=Path(destination)
    with tarfile.open(fileobj=io.BytesIO(content),mode='r:') as archive:
        members=archive.getmembers()
        if len(members)>MAX_FILES or sum(item.size for item in members)>MAX_ARCHIVE:
            raise ReviewUnavailable('review snapshot too large')
        for item in members:
            path=Path(item.name)
            if (path.is_absolute() or '..' in path.parts or '\\' in item.name or
                    not path.parts or path.parts[0]=='.git' or 'node_modules' in path.parts or
                    not (item.isdir() or item.isfile())):
                raise ReviewUnavailable('review snapshot links or metadata forbidden')
        archive.extractall(root,members=members,filter='data')


def _bounded_tree_digest(path, byte_limit):
    """Before/after fingerprint of the independent input copy, not build output."""
    root=Path(path);records=[];total=0
    if root.is_symlink() or not root.is_dir():raise Conflict('review input root changed')
    for item in sorted(root.rglob('*')):
        if item.is_symlink():raise Conflict('review input link detected')
        relative=item.relative_to(root).as_posix()
        if item.is_file():
            total+=item.stat().st_size
            if total>byte_limit:raise Conflict('review input too large')
            with item.open('rb') as stream:
                file_digest=hashlib.file_digest(stream,'sha256').hexdigest()
            records.append((relative,file_digest,item.stat().st_mode&0o777))
        elif not item.is_dir():raise Conflict('review input special file detected')
        if len(records)>MAX_FILES:raise Conflict('review input file count exceeded')
    return digest(records)


def _tree_digest(path):
    """Source snapshots retain the independent 64 MiB bound."""
    return _bounded_tree_digest(path, MAX_ARCHIVE)


def _runtime_digest(path):
    """A fixed larger allowance for host-owned Node binaries and dependencies."""
    return _bounded_tree_digest(path, MAX_RUNTIME_BYTES)


def _dependency_mount(profile, work):
    """Read-only trusted dependencies never originate in a tracked candidate."""
    modules=profile.runtime/'node_modules'
    if not modules.exists() and not modules.is_symlink():return []
    if modules.is_symlink() or not modules.is_dir():raise Conflict('trusted dependencies must be a real directory')
    target=Path(work)/'node_modules'
    if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise Conflict('candidate dependencies must not shadow trusted runtime')
    # _extract rejects every tracked node_modules entry, including empty dirs.
    # Prepare an empty mountpoint before the source becomes read-only in bwrap.
    target.mkdir(exist_ok=True)
    return ['--ro-bind',str(modules),'/work/node_modules']


@dataclass(frozen=True)
class ReviewProfile:
    project: str
    repository: str
    base_sha: str
    head_sha: str
    source: Path
    runtime: Path
    checks: dict
    timeout: int = 60

    def __post_init__(self):
        _project(self.project);_sha(self.base_sha);_sha(self.head_sha)
        from .store import _manifest
        _manifest({'project':self.project,'repository':self.repository,'base_sha':self.base_sha,
                   'description':'registered independent review','checks':['registered'],'kind':'test'})
        source,runtime=_directory(self.source),_directory(self.runtime)
        if source==runtime or source in runtime.parents or runtime in source.parents:
            raise ValueError('separate registered source and runtime required')
        if type(self.timeout) is not int or not 1<=self.timeout<=300:
            raise ValueError('bounded review command timeout required')
        if type(self.checks) is not dict or not 1<=len(self.checks)<=30:
            raise ValueError('fixed host check registry required')
        checks={}
        for name,argv in self.checks.items():
            if (type(name) is not str or not name or len(name)>200 or any(ord(c)<32 for c in name) or
                    type(argv) not in (tuple,list) or not 1<=len(argv)<=100 or
                    any(type(arg) is not str or not arg or len(arg)>4000 or '\x00' in arg for arg in argv)):
                raise ValueError('bounded host-owned check required')
            executable=Path(argv[0])
            if not executable.is_absolute() or '..' in executable.parts or not str(executable).startswith(('/runtime/','/usr/bin/','/bin/')):
                raise ValueError('registered sandbox executable required')
            checks[name]=tuple(argv)
        object.__setattr__(self,'source',source);object.__setattr__(self,'runtime',runtime)
        object.__setattr__(self,'checks',checks)

    def requested(self,task):
        manifest=task.get('manifest',{}) if type(task) is dict else {}
        if (manifest.get('project'),manifest.get('repository'),manifest.get('base_sha'))!=(self.project,self.repository,self.base_sha):
            raise PermissionDenied('review source task binding mismatch')
        names=manifest.get('checks')
        if type(names) is not list or not names or len(names)!=len(set(names)) or any(name not in self.checks for name in names):
            raise PermissionDenied('every requested check must be registered')
        return [(name,self.checks[name]) for name in names]


def _outer_limits():
    # NPROC must be applied inside the new user/PID namespaces. Applying a low
    # per-user process cap before bwrap creates them can reject namespace setup
    # merely because the shared outer host has many unrelated processes.
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_NOFILE,(256,256))
    resource.setrlimit(resource.RLIMIT_FSIZE,(256*1024*1024,)*2)
    resource.setrlimit(resource.RLIMIT_AS,(1536*1024*1024,)*2)


def _run_bounded(argv,*,cwd,timeout,heartbeat):
    """Low-level execution primitive; alone it does NOT establish isolation."""
    heartbeat()
    process=subprocess.Popen(list(argv),cwd=cwd,env={'PATH':'/usr/bin:/bin'},
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        start_new_session=True,preexec_fn=_outer_limits)
    started=time.monotonic()
    try:
        while process.poll() is None:
            heartbeat()
            if time.monotonic()-started>timeout:raise ReviewUnavailable('independent check timed out')
            time.sleep(0.02)
        heartbeat()
        return {'exit_code':process.returncode,'elapsed_seconds':round(max(0,time.monotonic()-started),6)}
    finally:
        if process.poll() is None:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=2)


class BubblewrapReviewEngine(BubblewrapTests):
    """Read-only source/runtime plus separate writable /outputs and /tmp."""
    def execute_snapshot(self,profile,work,outputs,checks,heartbeat):
        if self.probe().get('isolation_verified') is not True:
            raise ReviewUnavailable('independent review isolation unavailable')
        # Direct engine callers receive the same fixed runtime/path validation.
        runtime_before=_runtime_digest(profile.runtime)
        dependencies=_dependency_mount(profile,work)
        prefix=self._base()+['--ro-bind',str(profile.runtime),'/runtime',
            '--ro-bind',str(work),'/work']+dependencies+['--bind',str(outputs),'/outputs','--chdir','/work',
            '--setenv','PYTHONDONTWRITEBYTECODE','1','--setenv','TAESAN_DATA','/tmp/taesan-fixture-data',
            '--setenv','TAESAN_WEBHOOK_SECRET','fixture-review-only',
            '--setenv','TAESAN_ADMIN_IDS','fixture-admin','--setenv','TAESAN_STAFF_IDS','fixture-employee',
            '--setenv','NEXT_TELEMETRY_DISABLED','1']
        results=[]
        for name,argv in checks:
            guarded=['/usr/bin/prlimit','--core=0','--nofile=256','--fsize=268435456',
                     '--nproc=128','--as=1610612736','--']+list(argv)
            outcome=_run_bounded(prefix+guarded,cwd=None,timeout=profile.timeout,heartbeat=heartbeat)
            if _runtime_digest(profile.runtime)!=runtime_before:raise Conflict('trusted runtime changed during execution')
            results.append({'name':name,'argv_digest':digest(list(argv)),**outcome})
        return results


class FixedReviewRunner:
    """Register observe and this callable on ThreeTier's trusted host only.

    Custom engines are trusted host dependencies for isolated providers/tests,
    never request fields. The engine must prove its isolation before any command.
    Neither stdout, test prose nor arbitrary provider pass flags are accepted.
    The dedicated runtime tree is fingerprinted; /usr remains the trusted OS
    base and is NOT claimed as a fully fingerprinted container image.
    """
    def __init__(self,profile,*,engine=None,clock=time.time):
        if not isinstance(profile,ReviewProfile):raise ValueError('registered review profile required')
        if not callable(clock):raise ValueError('trusted clock required')
        self.profile=profile;self.engine=engine if engine is not None else BubblewrapReviewEngine()
        self.clock=clock
        if not callable(getattr(self.engine,'probe',None)) or not callable(getattr(self.engine,'execute_snapshot',None)):
            raise ValueError('trusted isolated review engine required')

    def _now(self):
        value=self.clock()
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
            raise ValueError('current observation time required')
        return value

    def _changed_paths(self):
        result=subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
            '-C',str(self.profile.source),'diff','--no-ext-diff','--no-textconv','--no-renames',
            '--name-only','-z',self.profile.base_sha,self.profile.head_sha,'--'],env=_git_env(),
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=10)
        if result.returncode or len(result.stdout)>1_000_000:raise ReviewUnavailable('bounded candidate paths required')
        paths=tuple(path.decode('utf-8') for path in result.stdout.split(b'\0') if path)
        from .coordination import validate_paths
        if paths:validate_paths(list(paths))
        return paths

    def observe(self,task):
        from .three_tier import SourceObservation
        checks=self.profile.requested(task);content=_archive(self.profile)
        return SourceObservation(project=self.profile.project,task_id=task['id'],
            base_sha=self.profile.base_sha,head_sha=self.profile.head_sha,
            artifact_digest=hashlib.sha256(content).hexdigest(),
            checks_digest=digest({name:digest(list(argv)) for name,argv in checks}),
            reference='independent-source/'+self.profile.project+'/'+self.profile.head_sha[:12],observed_at=self._now(),
            changed_paths=self._changed_paths(),runtime_digest=_runtime_digest(self.profile.runtime))

    def __call__(self,candidate,reviewer,*,heartbeat=None):
        from .three_tier import CheckOutcome,ReviewEvidence
        if not callable(heartbeat):raise ReviewUnavailable('per-request authorization heartbeat required')
        if type(candidate) is not dict or type(reviewer) is not dict:
            raise ValueError('registered candidate and reviewer required')
        task=candidate.get('task');saved=candidate.get('source')
        if type(task) is not dict or type(saved) is not dict or not isinstance(candidate.get('digest'),str):
            raise ValueError('frozen candidate bindings required')
        subject=reviewer.get('subject')
        if (type(subject) is not str or not subject or reviewer.get('project')!=self.profile.project or
                reviewer.get('role')!='reviewer' or subject==candidate.get('developer_subject')):
            raise PermissionDenied('independent project reviewer required')
        heartbeat();current=self.observe(task)
        for field in ('project','task_id','base_sha','head_sha','artifact_digest','checks_digest','changed_paths','runtime_digest'):
            supplied=tuple(saved.get(field,())) if field=='changed_paths' else saved.get(field)
            if supplied!=getattr(current,field):raise Conflict('frozen review candidate changed')
        probe=self.engine.probe()
        if type(probe) is not dict or probe.get('available') is not True or probe.get('isolation_verified') is not True:
            raise ReviewUnavailable('independent review isolation unavailable')
        checks=self.profile.requested(task);content=_archive(self.profile)
        if hashlib.sha256(content).hexdigest()!=current.artifact_digest:raise Conflict('review archive changed')
        with tempfile.TemporaryDirectory(prefix='taesan-independent-review-') as directory:
            root=Path(directory);work=root/'source';outputs=root/'outputs'
            work.mkdir(mode=0o700);outputs.mkdir(mode=0o700);_extract(content,work)
            _dependency_mount(self.profile,work)
            before=_tree_digest(work)
            def fence():
                heartbeat()
            outcomes=self.engine.execute_snapshot(self.profile,work,outputs,checks,fence)
            fence()
            observed=self.observe(task)
            if any(getattr(observed,k)!=getattr(current,k) for k in ('head_sha','artifact_digest','checks_digest','changed_paths','runtime_digest')):
                raise Conflict('candidate changed during independent verification')
            if before!=_tree_digest(work):raise Conflict('independent review inputs changed')
        if type(outcomes) is not list or len(outcomes)!=len(checks):raise ReviewUnavailable('complete execution outcomes required')
        expected=dict(checks);validated=[]
        if len({o.get('name') for o in outcomes if type(o) is dict})!=len(checks):raise ReviewUnavailable('unique execution outcomes required')
        for result in outcomes:
            if type(result) is not dict or set(result)!={'name','argv_digest','exit_code','elapsed_seconds'}:
                raise ReviewUnavailable('typed actual command outcome required')
            name=result['name'];elapsed=result['elapsed_seconds'];code=result['exit_code']
            if (name not in expected or result['argv_digest']!=digest(list(expected[name])) or
                    type(code) is not int or not -255<=code<=255 or type(elapsed) not in (int,float) or
                    not math.isfinite(elapsed) or not 0<=elapsed<=self.profile.timeout+10):
                raise ReviewUnavailable('execution evidence mismatch')
            validated.append(CheckOutcome(**result))
        if self.engine.probe().get('isolation_verified') is not True:
            raise ReviewUnavailable('independent isolation changed')
        heartbeat()
        return ReviewEvidence(candidate_digest=candidate['digest'],reviewer_subject=subject,
            checks=tuple(validated),artifact_digest=current.artifact_digest,
            reference='independent-checks/'+digest({'candidate':candidate['digest'],'checks':outcomes})[:32],
            observed_at=self._now(),isolation_verified=True,runtime_digest=current.runtime_digest)
