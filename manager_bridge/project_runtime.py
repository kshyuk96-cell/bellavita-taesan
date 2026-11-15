"""Fixed, opt-in project test receivers with a fail-closed Linux sandbox.

No deployment, provider calls, shell snippets from a task, automatic replay, or
account bootstrap. A registered host supplies the exact source and dependency
roots. Sandbox execution is a worker claim, never independent verification.
"""
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import signal
import resource
import stat
import subprocess
import tarfile
import tempfile
import time

from .agent_directory import AgentObservation, Connector
from .representative_worker import RepresentativeWorker
from .source_rehearsal import observe_checkout
from .store import Conflict, PermissionDenied, _project, _sha, canonical_json, token_hash


# Arguments originate here, never from a model, inbox text, or an HTTP request.
# Sources match the package scripts inspected in the four pinned projects.
CHECK_COMMANDS = {
    'bellavita-shop': {
        'python_tests': ('/runtime/bin/python', '-m', 'unittest', 'discover', '-s', 'tests', '-q'),
    },
    'bellavita-erp': {
        'build': ('/runtime/bin/npm', 'run', 'build'),
        'node_tests': ('/runtime/bin/npm', 'test'),
    },
    'hope-sunrise': {
        'build': ('/runtime/bin/npm', 'run', 'build'),
        'node_tests': ('/runtime/bin/npm', 'test'),
    },
    'funeral-association': {
        'node_tests': ('/runtime/bin/npm', 'test'),
        'typecheck': ('/runtime/bin/npm', 'run', 'typecheck'),
        'build': ('/runtime/bin/npm', 'run', 'build'),
    },
}


def _directory(value):
    path = Path(value).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)) or not path.is_dir():
        raise ValueError('exact non-symlink registered directory required')
    if path == Path('/') or len(path.parts) < 3:
        raise ValueError('dedicated runtime/source directory required')
    return path


CONTROL_COMMANDS = {
    'authorization_regression': ('/control-runtime/bin/python', '-P', '-m', 'unittest',
        'manager_bridge.tests.test_agent_directory', 'manager_bridge.tests.test_representative_worker', '-q'),
    'approval_regression': ('/control-runtime/bin/python', '-P', '-m', 'unittest',
        'manager_bridge.tests.test_delegation', 'manager_bridge.tests.test_delegation_batch', '-q'),
}


@dataclass(frozen=True)
class ControlChecks:
    source: Path
    base_sha: str
    runtime: Path

    def __post_init__(self):
        _sha(self.base_sha)
        source, runtime = _directory(self.source), _directory(self.runtime)
        if source == runtime or source in runtime.parents or runtime in source.parents:
            raise ValueError('separate control source and runtime required')
        object.__setattr__(self, 'source', source); object.__setattr__(self, 'runtime', runtime)

    def source_check(self):
        value = observe_checkout(self.source)
        if value['sha'] != self.base_sha or not value['tracked_and_visible_untracked_clean']:
            raise Conflict('registered control source changed')
        required = ('test_agent_directory.py','test_representative_worker.py','test_delegation.py','test_delegation_batch.py')
        if not all((self.source/'manager_bridge'/'tests'/filename).is_file() for filename in required):
            raise Conflict('missing_required_control_checks')
        return value


@dataclass(frozen=True)
class ProjectTestProfile:
    project: str
    repository: str
    base_sha: str
    source: Path
    runtime: Path
    dependencies: Path | None = None
    timeout: int = 300
    control: ControlChecks | None = None

    def __post_init__(self):
        _project(self.project); _sha(self.base_sha)
        if self.project not in CHECK_COMMANDS or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repository):
            raise ValueError('registered project test profile required')
        if type(self.timeout) is not int or not 1 <= self.timeout <= 1800:
            raise ValueError('bounded test timeout required')
        source, runtime = _directory(self.source), _directory(self.runtime)
        if source == runtime or source in runtime.parents or runtime in source.parents:
            raise ValueError('source and dedicated runtime must be separate')
        object.__setattr__(self, 'source', source); object.__setattr__(self, 'runtime', runtime)
        if self.control is not None and not isinstance(self.control, ControlChecks):
            raise ValueError('host-owned control test profile required')
        if self.dependencies is not None:
            object.__setattr__(self, 'dependencies', _directory(self.dependencies))

    def source_check(self):
        result = observe_checkout(self.source)
        if result['sha'] != self.base_sha or not result['tracked_and_visible_untracked_clean']:
            raise Conflict('registered source changed')
        return result

    def checks(self, requested):
        available = dict(CHECK_COMMANDS[self.project])
        if self.control is not None: available.update(CONTROL_COMMANDS)
        if type(requested) is not list or not requested or len(requested) != len(set(requested)):
            raise ValueError('exact fixed check list required')
        if self.control is None and any(key in CONTROL_COMMANDS for key in requested):
            raise Conflict('missing_required_control_checks')
        if any(type(key) is not str or key not in available for key in requested):
            raise PermissionDenied('check not registered for this project')
        # ERP's PWA tests import build output from dist/server/index.js.
        ordered = list(requested)
        if self.project == 'bellavita-erp' and 'node_tests' in ordered:
            ordered = ['build'] + [key for key in ordered if key != 'build']
        return [(key, available[key]) for key in ordered]

    def assignment(self, assignment):
        manifest = assignment.get('manifest', {})
        if (assignment.get('project'), manifest.get('project'), manifest.get('repository'), manifest.get('base_sha'), manifest.get('kind')) != (
                self.project, self.project, self.repository, self.base_sha, 'test'):
            raise PermissionDenied('test receiver project/source/kind mismatch')
        return self.checks(manifest.get('checks'))


def _limits():
    # Bound each command even when repository test code is defective.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2)


def _archive_source(profile, destination):
    """Copy only the pinned tracked tree, excluding host .git/config and ignored data."""
    profile.source_check()
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_LAZY_FETCH': '1',
           'GIT_OPTIONAL_LOCKS': '0', 'GIT_ALLOW_PROTOCOL': ''}
    result = subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                             '-C', str(profile.source), 'archive', '--format=tar', profile.base_sha],
                            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=30)
    if result.returncode or len(result.stdout) > 256 * 1024 * 1024:
        raise Conflict('registered source archive unavailable')
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode='r:') as archive:
        members = archive.getmembers()
        if len(members) > 100000 or sum(m.size for m in members) > 256 * 1024 * 1024:
            raise ValueError('bounded source archive required')
        for item in members:
            path = Path(item.name)
            if path.is_absolute() or '..' in path.parts or not (item.isdir() or item.isfile()):
                raise ValueError('source links and special files are not accepted')
            if path.parts and path.parts[0] in ('.git', 'node_modules'):
                raise ValueError('source metadata/dependencies require separate registration')
        archive.extractall(destination, members=members, filter='data')
    profile.source_check()


class BubblewrapTests:
    """Host-owned execution engine. Absence or failed isolation never falls back."""
    sandbox_kind = 'bubblewrap_network_and_pid_namespace'

    def __init__(self):
        self.binary = Path('/usr/bin/bwrap')

    def _base(self):
        # No host /home, /workspace, /etc, credentials, sockets, or agent state.
        args = [str(self.binary), '--unshare-all', '--die-with-parent', '--new-session',
                '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
                '--symlink', 'usr/lib', '/lib']
        if Path('/usr/lib64').exists():
            args += ['--symlink', 'usr/lib64', '/lib64']
        return args + ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--clearenv',
                       '--setenv', 'PATH', '/runtime/bin:/usr/bin:/bin', '--setenv', 'HOME', '/tmp',
                       '--setenv', 'LANG', 'C.UTF-8']

    def probe(self):
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            return {'available': False, 'state': 'sandbox_unavailable', 'isolation_verified': False}
        try:
            result = subprocess.run(self._base() + ['/usr/bin/true'], env={'PATH': '/usr/bin:/bin'},
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=5)
            verified = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            verified = False
        return {'available': verified, 'state': 'ready' if verified else 'sandbox_unavailable',
                'isolation_verified': verified}

    def execute(self, profile, checks, heartbeat):
        if not self.probe()['available']:
            raise Conflict('secure test sandbox unavailable')
        with tempfile.TemporaryDirectory(prefix='taesan-isolated-test-') as directory:
            work = Path(directory) / 'work'; work.mkdir(mode=0o700)
            _archive_source(profile, work)
            if profile.control is not None:
                control = Path(directory) / 'control'; control.mkdir(mode=0o700)
                _archive_source(profile.control, control)
            commands = self._base() + ['--ro-bind', str(profile.runtime), '/runtime',
                                      '--bind', str(work), '/work', '--chdir', '/work',
                                      '--setenv', 'TAESAN_DATA', '/tmp/taesan-data',
                                      '--setenv', 'TAESAN_WEBHOOK_SECRET', 'fixture-runtime-only',
                                      '--setenv', 'TAESAN_ADMIN_IDS', 'fixture-admin',
                                      '--setenv', 'TAESAN_STAFF_IDS', 'fixture-employee',
                                      '--setenv', 'NEXT_TELEMETRY_DISABLED', '1',
                                      '--setenv', 'DATABASE_URL', 'postgres://fixture:fixture@127.0.0.1:1/fixture']
            if profile.control is not None:
                commands += ['--ro-bind', str(control), '/control',
                             '--ro-bind', str(profile.control.runtime), '/control-runtime',
                             '--setenv', 'PYTHONPATH', '/control']
            if profile.dependencies is not None:
                (work / 'node_modules').mkdir(exist_ok=True)
                commands += ['--ro-bind', str(profile.dependencies), '/work/node_modules']
            results = []
            for name, argv in checks:
                heartbeat()
                process = subprocess.Popen(commands + list(argv), env={'PATH': '/usr/bin:/bin'},
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=_limits)
                started = time.monotonic()
                try:
                    while process.poll() is None:
                        heartbeat()
                        if time.monotonic() - started > profile.timeout:
                            raise TimeoutError('fixed check exceeded time limit')
                        time.sleep(0.1)
                    heartbeat()
                    results.append({'name': name, 'passed': process.returncode == 0})
                finally:
                    if process.poll() is None:
                        try: os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError: pass
                        try: process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            try: os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError: pass
                            process.wait(timeout=2)
                if not results[-1]['passed']:
                    break
            return results


class ProjectReceiver:
    """One existing worker inbox; no new account, registration, or readiness grant."""
    def __init__(self, store, directory, profile, *, engine=None):
        if directory.store is not store or not isinstance(profile, ProjectTestProfile):
            raise ValueError('shared registered receiver required')
        self.store, self.directory, self.profile = store, directory, profile
        self.engine = engine if engine is not None else BubblewrapTests()

    def _scope(self, token, agent_id, *, require_enabled=True):
        with self.store._transaction() as c:
            actor, agent = self.directory._agent(c, token, self.profile.project, agent_id, ('worker',))
            if require_enabled and not agent['enabled']:
                raise PermissionDenied('receiver registration disabled')
            row = c.execute('SELECT config FROM maintenance_projects WHERE project=?', (self.profile.project,)).fetchone()
            if require_enabled and (row is None or not json.loads(row['config'])['maintenance_enabled']):
                raise PermissionDenied('project execution disabled')
            expected = self.store._project_access(c, actor, self.profile.project)
            if expected != self.profile.repository:
                raise PermissionDenied('receiver repository changed')
            return dict(agent)

    def check(self, worker_token, agent_id):
        self._scope(worker_token, agent_id)
        self.profile.source_check()
        if self.profile.control is not None: self.profile.control.source_check()
        result = self.engine.probe()
        return {'project': self.profile.project, 'agent_id': agent_id, **result,
                'execution_started': False, 'existing_chat_control_verified': False,
                'accounts_generated': False, 'operational_change': False}

    def connector(self, worker_hash, session_id):
        """Register this callback at the host; directory still requires manager probe."""
        def observe(project, agent_id, session):
            if (project, session) != (self.profile.project, session_id):
                raise PermissionDenied('receiver identity mismatch')
            self.profile.source_check()
            if self.profile.control is not None: self.profile.control.source_check()
            with self.store._transaction() as c:
                registered = c.execute('SELECT worker_hash,session_id,enabled FROM manager_agents WHERE project=? AND agent_id=?',
                                       (project,agent_id)).fetchone()
                config = c.execute('SELECT config FROM maintenance_projects WHERE project=?',(project,)).fetchone()
                enabled = bool(registered and config and registered['enabled'] and
                    (registered['worker_hash'],registered['session_id']) == (worker_hash,session_id) and
                    json.loads(config['config'])['maintenance_enabled'])
            now = self.store._now()
            return AgentObservation(project, agent_id, session_id, worker_hash,
                bool(enabled and self.engine.probe()['available']), 'project-receiver/' + project, now, now + 30)
        return Connector(observe)

    def _execute(self, token, agent_id, assignment, heartbeat):
        # Nothing supplied by the task becomes argv, an env var, a path, or a URL.
        self._scope(token, agent_id)
        checks = self.profile.assignment(assignment)
        self.profile.source_check()
        if self.profile.control is not None: self.profile.control.source_check()
        def fenced():
            self._scope(token, agent_id)
            return heartbeat()
        results = self.engine.execute(self.profile, checks, fenced)
        self._scope(token, agent_id); self.profile.source_check()
        if self.profile.control is not None: self.profile.control.source_check()
        if type(results) is not list or not results or any(type(item) is not dict or set(item) != {'name', 'passed'}
            or type(item['passed']) is not bool for item in results):
            raise ValueError('typed actual check outcomes required')
        names = [name for name, _ in checks]
        if [item['name'] for item in results] != names[:len(results)]:
            raise ValueError('unregistered check results rejected')
        if len(results) < len(checks) and results[-1]['passed']:
            raise ValueError('missing check outcomes rejected')
        return {'head_sha': self.profile.base_sha, 'summary': 'Registered isolated fixture tests; independent manager verification required.',
                'checks': results}

    def run_once(self, worker_token, agent_id):
        status = self.check(worker_token, agent_id)
        if not status['available']:
            return {**status, 'automatically_replayed': False}
        runner = RepresentativeWorker(self.store, self.directory,
            execute=lambda assignment, heartbeat: self._execute(worker_token, agent_id, assignment, heartbeat))
        return runner.run_once(worker_token, self.profile.project, agent_id)

    def recovery(self, worker_token, agent_id):
        """Read reconciliation status after stop/restart, including disabled receivers."""
        self._scope(worker_token, agent_id, require_enabled=False)
        with self.store._transaction() as c:
            actor, _ = self.directory._agent(c, worker_token, self.profile.project, agent_id, ('worker',))
            rows = c.execute("""SELECT t.id,t.state,t.lease_expires,i.state AS delivery_state,
                EXISTS(SELECT 1 FROM bridge_stop_confirmations s WHERE s.task=t.id) AS stop_confirmed
                FROM manager_agent_inbox i JOIN bridge_tasks t ON t.id=i.task_id
                WHERE i.project=? AND i.agent_id=? AND t.worker_token_hash=?
                AND t.state IN ('running','needs_verification','cancelled','reported')
                ORDER BY t.created,t.id""",
                (self.profile.project, agent_id, actor['token_hash'])).fetchall()
            tasks = [{'task_id': row['id'], 'task_state': row['state'],
                      'delivery_state': row['delivery_state'],
                      'lease_expired': row['lease_expires'] is not None and row['lease_expires'] <= self.store._now(),
                      'stop_confirmed': bool(row['stop_confirmed']),
                      'independent_verification_required': True, 'automatic_resume': False}
                     for row in rows if row['state'] != 'cancelled' or not row['stop_confirmed']]
            return {'project': self.profile.project, 'agent_id': agent_id, 'tasks': tasks,
                    'automatically_replayed': False, 'operational_change': False}
