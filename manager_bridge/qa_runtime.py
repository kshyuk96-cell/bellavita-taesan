"""Host-registered QA profiles and a fail-closed, bounded isolated executor."""
from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from .project_runtime import BubblewrapTests, _archive_source, _directory, _limits
from .source_rehearsal import observe_checkout
from .store import Conflict, digest, _sha, _project

SUITES = {'order-ledger-v1', 'universal-v1'}
MAX_REPORT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class QAProfile:
    project: str
    release: str
    revision: str
    source: Path
    runtime: Path
    suite: str
    timeout: int = 300
    seed: int = 20261116

    def __post_init__(self):
        _project(self.project)
        _sha(self.revision)
        if (self.suite not in SUITES or type(self.release) is not str or not self.release or
                len(self.release) > 100 or type(self.timeout) is not int or not 1 <= self.timeout <= 3600 or
                type(self.seed) is not int or not 0 <= self.seed <= 2**32-1):
            raise ValueError('bounded registered QA profile required')
        object.__setattr__(self, 'source', _directory(self.source))
        object.__setattr__(self, 'runtime', _directory(self.runtime))
        if self.source == self.runtime or self.source in self.runtime.parents or self.runtime in self.source.parents:
            raise ValueError('separate runtime and source required')
        if not (self.runtime/'bin/python').is_file():
            raise ValueError('registered Python runtime required')

    @property
    def base_sha(self):
        return self.revision

    def source_check(self):
        state = observe_checkout(self.source)
        if state['sha'] != self.revision or not state['tracked_and_visible_untracked_clean']:
            raise Conflict('registered QA source changed')
        return state

    def manifest(self):
        value = asdict(self)
        value.update(source=str(self.source), runtime=str(self.runtime),
            python_sha256=hashlib.sha256((self.runtime/'bin/python').read_bytes()).hexdigest(),
            schema='qa-execution-profile/v1', network='none', source_mount='read_only')
        return value

    @property
    def fingerprint(self):
        return digest(self.manifest())


class QARegistry:
    """Construct only from trusted host config, never from a task/report."""
    def __init__(self, profiles):
        self.profiles = {}
        for profile in profiles:
            if not isinstance(profile, QAProfile):
                raise ValueError('typed registered profile required')
            key = (profile.project, profile.release)
            if key in self.profiles:
                raise ValueError('duplicate adapter release')
            self.profiles[key] = profile

    def resolve(self, manifest):
        profile = self.profiles.get((manifest['project'], manifest['adapter_release']))
        if (profile is None or manifest.get('execution_profile_digest') != profile.fingerprint or
                profile.revision != manifest['source_revision'] or
                profile.timeout > manifest['max_seconds']):
            raise Conflict('unapproved execution profile')
        profile.source_check()
        return profile


class QAInterrupted(Exception):
    pass


class QASandbox:
    """Reuse the project receiver's network/PID namespace isolation; no fallback."""
    def __init__(self):
        self.base = BubblewrapTests()

    def probe(self):
        return self.base.probe()

    def execute(self, profile, heartbeat):
        if not self.probe()['isolation_verified']:
            raise Conflict('QA sandbox unavailable')
        before = profile.fingerprint
        with tempfile.TemporaryDirectory(prefix='taesan-qa-') as directory:
            source = Path(directory)/'source'
            source.mkdir(mode=0o700)
            _archive_source(profile, source)
            argv = self.base._base() + [
                '--ro-bind', str(profile.runtime), '/runtime',
                '--ro-bind', str(source), '/source', '--chdir', '/source',
                '--setenv', 'PYTHONHOME', '/runtime',
                '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
                '/runtime/bin/python', '-I', '/source/scripts/qa_isolated_child.py',
                '--suite', profile.suite, '--revision', profile.revision, '--seed', str(profile.seed)]
            # File-backed stdout avoids pipe deadlocks and bounds host memory.
            with tempfile.TemporaryFile() as output:
                heartbeat()
                process = subprocess.Popen(argv, env={'PATH':'/usr/bin:/bin'},
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                    start_new_session=True, preexec_fn=_limits)
                started = time.monotonic()
                monitoring_done = threading.Event()
                fencing_errors = []
                def monitor():
                    while not monitoring_done.is_set():
                        try:
                            heartbeat()
                        except Exception:
                            fencing_errors.append(True)
                            return
                        monitoring_done.wait(0.1)
                monitor_thread = threading.Thread(target=monitor, daemon=True)
                monitor_thread.start()
                try:
                    while process.poll() is None:
                        if fencing_errors:
                            raise QAInterrupted('manager_fence_lost')
                        if time.monotonic()-started >= profile.timeout:
                            raise QAInterrupted('execution_timeout')
                        if os.fstat(output.fileno()).st_size > MAX_REPORT_BYTES:
                            raise QAInterrupted('output_limit')
                        time.sleep(0.05)
                    heartbeat()
                    if process.returncode != 0:
                        raise QAInterrupted('child_failed')
                    output.seek(0)
                    raw = output.read(MAX_REPORT_BYTES+1)
                    if len(raw) > MAX_REPORT_BYTES:
                        raise QAInterrupted('output_limit')
                    report = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if type(report) is not dict:
                        raise QAInterrupted('malformed_child_report')
                    profile.source_check()
                    if profile.fingerprint != before:
                        raise QAInterrupted('runtime_changed')
                    return report, dict(schema='qa-execution-receipt/v1',
                        profile_digest=before, source_revision=profile.revision,
                        isolation_verified=True, network='none', process_exited=True,
                        report_observation_digest=digest(report))
                finally:
                    monitoring_done.set()
                    # Kill descendants even if the parent already exited.
                    try: os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                    try: process.wait(timeout=1)
                    except subprocess.TimeoutExpired: pass
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait(timeout=2)
