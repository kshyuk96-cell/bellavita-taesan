"""Pull approved bridge tasks into isolated local worktrees.

Bridge bearer/lease tokens never enter a Codex prompt, argv, child environment,
or console output. Claim/heartbeat/report each make ONE HTTP request: transport
uncertainty stops work rather than retrying an uncertain state change.

Codex workspace-write is a write/network restriction, not a complete host data
confidentiality boundary. Run this worker on a dedicated credential-free host;
its dedicated CODEX_HOME contains only the separately authorized coding login.
No existing Codex window/session is controlled by this worker.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import threading
import tomllib
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_JSON = 1_000_000
_SHA = re.compile(r'[a-f0-9]{40}')
_ID = re.compile(r'[a-f0-9]{32}')


class WorkerError(RuntimeError):
    """Safe operator-facing error; never wrap a raw provider error string."""


class LeaseStopped(WorkerError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def _post(url, *, headers, body, timeout):
    request = Request(url, data=body, headers=headers, method='POST')
    try:
        with build_opener(_NoRedirect(), ProxyHandler({})).open(request, timeout=timeout) as response:
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise WorkerError('브리지 응답 크기를 확인해 주세요.')
            return response.status, json.loads(raw)
    except HTTPError as error:
        return error.code, None


def _token(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600
                or details.st_uid != os.getuid() or not 32 <= details.st_size <= 4096):
            raise WorkerError('워커 토큰 파일의 소유자와 0600 권한을 확인해 주세요.')
        value = os.read(descriptor, 4097).decode('ascii').strip()
        if not 32 <= len(value) <= 1024 or any(character.isspace() for character in value):
            raise WorkerError('워커 토큰 파일 형식을 확인해 주세요.')
        return value
    except (OSError, UnicodeError):
        raise WorkerError('워커 토큰 파일을 읽지 못했어요.') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _manifest_digest(manifest):
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_task(task, project):
    if not isinstance(task, dict) or not isinstance(task.get('manifest'), dict):
        raise WorkerError('승인된 작업 형식을 확인하지 못했어요.')
    manifest = task['manifest']
    approval = task.get('approval')
    try:
        digest = _manifest_digest(manifest)
        valid = (set(manifest) == {'project', 'repository', 'base_sha', 'description', 'checks', 'kind'}
                 and manifest['project'] == project and manifest['kind'] in ('development', 'test')
                 and isinstance(manifest['repository'], str)
                 and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', manifest['repository'])
                 and isinstance(manifest['base_sha'], str) and _SHA.fullmatch(manifest['base_sha'])
                 and isinstance(manifest['description'], str) and 0 < len(manifest['description']) <= 16000
                 and isinstance(manifest['checks'], list) and len(manifest['checks']) <= 100
                 and all(isinstance(item, str) and 0 < len(item) <= 500 for item in manifest['checks'])
                 and isinstance(task.get('id'), str) and _ID.fullmatch(task['id'])
                 and type(task.get('version')) is int and task['version'] >= 1
                 and task.get('state') == 'running'
                 and task.get('manifest_digest') == digest and isinstance(approval, dict)
                 and type(approval.get('version')) is int and approval.get('version') == task['version']
                 and approval.get('manifest_digest') == digest
                 and isinstance(task.get('lease_token'), str) and re.fullmatch(r'[a-f0-9]{64}', task['lease_token'])
                 and type(task.get('lease_expires_at')) in (int, float)
                 and math.isfinite(task['lease_expires_at']) and task['lease_expires_at'] > 0)
    except (TypeError, ValueError, KeyError):
        valid = False
    if valid and approval.get('policy_digest') is not None:
        policy = task.get('execution_policy')
        valid = isinstance(policy, dict) and _manifest_digest(policy) == approval['policy_digest']
    if not valid:
        raise WorkerError('작업 내용·개발 승인·임대 정보가 일치하지 않아요.')
    return manifest


class WorkerClient:
    def __init__(self, base_url, token_file, project, *, transport=None, clock=time.time, lease_seconds=300):
        parsed = urlsplit(base_url)
        local = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
        if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and local)) or (
                not parsed.netloc or parsed.username or parsed.password or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment):
            raise WorkerError('고정된 HTTPS 브리지 주소 또는 로컬 시험 주소가 필요해요.')
        if (not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', project)
                or type(lease_seconds) is not int or not 10 <= lease_seconds <= 3600):
            raise WorkerError('워커 프로젝트와 임대 설정을 확인해 주세요.')
        self.base_url, self.token_file, self.project = base_url.rstrip('/'), Path(token_file), project
        self.transport, self.clock, self.lease_seconds = transport or _post, clock, lease_seconds

    def _request(self, endpoint, payload):
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            status, response = self.transport(self.base_url + '/v1/worker/' + endpoint,
                headers={'Authorization': 'Bearer ' + _token(self.token_file), 'Content-Type': 'application/json'},
                body=body, timeout=10)
        except WorkerError:
            raise
        except Exception:
            raise WorkerError('브리지 응답이 불확실해 작업을 멈췄어요. 자동 재전송하지 않았어요.') from None
        if status in (403, 404, 409):
            raise LeaseStopped('워커 권한 또는 현재 작업 임대를 확인하지 못해 멈췄어요.')
        if status != 200 or not isinstance(response, dict) or response.get('ok') is not True:
            raise WorkerError('브리지 요청 결과를 확인하지 못했어요. 자동 재전송하지 않았어요.')
        return response

    def claim(self):
        response = self._request('claim', {'project': self.project, 'lease_seconds': self.lease_seconds})
        if 'task' not in response:
            raise WorkerError('작업 배정 응답을 확인하지 못했어요.')
        task = response['task']
        if task is not None:
            validate_task(task, self.project)
        return task

    def heartbeat(self, task):
        response = self._request('heartbeat', {'id': task['id'], 'lease_token': task['lease_token'],
                                               'lease_seconds': self.lease_seconds})
        heartbeat = response.get('heartbeat')
        if (not isinstance(heartbeat, dict) or heartbeat.get('id') != task['id']
                or heartbeat.get('state') != 'running' or heartbeat.get('cancel_requested') is not False
                or type(heartbeat.get('lease_expires_at')) not in (int, float)
                or not math.isfinite(heartbeat['lease_expires_at'])
                or heartbeat['lease_expires_at'] <= self.clock()):
            raise LeaseStopped('작업이 취소됐거나 임대가 만료되어 멈췄어요.')
        task['lease_expires_at'] = heartbeat['lease_expires_at']
        return heartbeat

    def report(self, task, report):
        response = self._request('report', {'id': task['id'], 'lease_token': task['lease_token'], 'report': report})
        saved = response.get('task')
        if not isinstance(saved, dict) or saved.get('id') != task['id'] or saved.get('state') != 'reported':
            raise WorkerError('검토 보고 접수 결과를 확인하지 못했어요.')
        return saved

    def run_once(self, runner):
        try:
            if isinstance(runner, CodexRunner):
                readiness = runner.check_environment()
                if readiness.get('ready') is not True:
                    return {'status': 'environment_blocked', 'reason': readiness['reason']}
            task = self.claim()
            if task is None:
                return {'status': 'idle'}
            self.heartbeat(task)
            current = [True]
            def pulse():
                try:
                    self.heartbeat(task)
                    return True
                except WorkerError:
                    current[0] = False
                    return False
            report = runner.run(task, pulse=pulse)
            if not current[0] or not pulse():
                return {'status': 'stopped'}
            self.report(task, report)
            return {'status': 'reported'}
        except LeaseStopped:
            return {'status': 'stopped'}
        except WorkerError:
            return {'status': 'uncertain'}
        except Exception:
            return {'status': 'uncertain'}


def _private_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise WorkerError('절대 경로의 전용 워커 폴더가 필요해요.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise WorkerError('전용 워커 폴더의 소유자와 0700 권한을 확인해 주세요.')
    return path.resolve()


class CodexRunner:
    """Real CLI adapter; capability/auth failures are reported as unsupported.

    Only locally configured project/repository mappings are executable. Requested
    checks are model instructions, never shell strings executed by this worker.
    A zero CLI exit or worker report is NOT independent verification or deployment.
    """
    def __init__(self, projects, *, codex_home, work_root, codex_bin='/opt/codex/bin/codex',
                 process_factory=subprocess.Popen, command=subprocess.run, clock=time.time,
                 monotonic=time.monotonic, heartbeat_seconds=15, max_runtime_seconds=1800,
                 stop_event=None, network_environment=None):
        self.projects = projects
        self.codex_home, self.work_root, self.codex_bin = Path(codex_home), Path(work_root), Path(codex_bin)
        self.process_factory, self.command = process_factory, command
        self.clock, self.monotonic = clock, monotonic
        self.heartbeat_seconds, self.max_runtime = heartbeat_seconds, max_runtime_seconds
        self.stop_event = stop_event or threading.Event()
        self.network_environment = dict(network_environment or {})
        allowed_network = {'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy',
                           'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CODEX_PROXY_CERT'}
        if (set(self.network_environment) - allowed_network or any(
                not isinstance(value, str) or not value or len(value) > 8192
                or any(ord(char) < 32 for char in value) for value in self.network_environment.values())):
            raise WorkerError('운영자가 명시한 프록시·인증서 설정만 사용할 수 있어요.')
        if not 0.01 <= heartbeat_seconds <= 60 or not 1 <= max_runtime_seconds <= 3600:
            raise WorkerError('워커 실행 시간 범위를 확인해 주세요.')

    def _env(self, home):
        return {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'HOME': str(home),
                'CODEX_HOME': str(self.codex_home), 'GIT_CONFIG_NOSYSTEM': '1',
                'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'PYTHONDONTWRITEBYTECODE': '1', **self.network_environment}

    def _git(self, repo, *arguments):
        result = self.command(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                               '-c', 'credential.helper=', '-C', str(repo), *arguments],
                              env=self._env(self.work_root), capture_output=True, text=True, timeout=20, check=False)
        if result.returncode != 0:
            raise WorkerError('승인된 로컬 Git 작업을 확인하지 못했어요.')
        return result.stdout.strip()

    def _profile(self):
        home = _private_directory(self.codex_home)
        work = _private_directory(self.work_root)
        if home == work or not self.codex_bin.is_absolute() or not self.codex_bin.is_file():
            raise WorkerError('전용 Codex 실행 연결이 준비되지 않았어요.')
        auth = home / 'auth.json'
        details = auth.lstat()
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) != 0o600):
            raise WorkerError('전용 Codex 로그인 파일을 확인해 주세요.')
        for entry in home.iterdir():
            if entry.is_symlink() or entry.name in ('mcp.json', 'hooks.json', 'hooks', 'managed_config.toml'):
                raise WorkerError('전용 Codex 환경에 외부 도구나 훅 설정이 있어 실행하지 않았어요.')
        configuration = home / 'config.toml'
        if configuration.exists():
            if not configuration.is_file() or configuration.stat().st_size > 65536:
                raise WorkerError('전용 Codex 설정 형식을 확인해 주세요.')
            try:
                settings = tomllib.loads(configuration.read_text())
            except (OSError, ValueError):
                raise WorkerError('전용 Codex 설정 형식을 확인하지 못했어요.') from None
            if any(settings.get(section) for section in ('plugins', 'mcp_servers', 'hooks')):
                raise WorkerError('전용 프로필의 외부 도구 설정을 확인해 주세요.')
        plugins = home / 'plugins'
        if plugins.exists():
            if not plugins.is_dir():
                raise WorkerError('Codex 캐시 폴더 형식을 확인해 주세요.')
            for entry in plugins.iterdir():
                if entry.name not in ('cache', '.remote-plugin-install-staging') or entry.is_symlink() or not entry.is_dir():
                    raise WorkerError('공식 CLI 캐시 이외의 플러그인 설정은 사용할 수 없어요.')
                if entry.name == '.remote-plugin-install-staging' and any(entry.iterdir()):
                    raise WorkerError('플러그인 설치 중에는 워커를 시작하지 않아요.')
            for index, entry in enumerate(plugins.rglob('*')):
                if index >= 10000 or entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
                    raise WorkerError('Codex 캐시 범위 또는 파일 형식을 확인해 주세요.')
        # User config is explicitly ignored. Project configs are checked separately.
        return home, work

    def preflight(self):
        """Check local prerequisites before claiming; this does not verify AI auth."""
        try:
            self._profile()
        except Exception:
            raise WorkerError('전용 실행 환경을 먼저 준비해 주세요.') from None

    def check_environment(self):
        """Exercise the actual sandbox without an AI call, before taking a lease."""
        try:
            self.preflight()
            with tempfile.TemporaryDirectory(prefix='probe-', dir=self.work_root) as folder:
                probe = Path(folder)
                (probe / 'input.txt').write_text('taesan-probe')
                program = ("from pathlib import Path; "
                           "assert Path('input.txt').read_text() == 'taesan-probe'; "
                           "Path('output.txt').write_text('taesan-probe'); "
                           "print('TAESAN_SANDBOX_READY')")
                result = self.command([str(self.codex_bin), 'sandbox', '--cd', folder,
                    '-c', 'sandbox_mode="workspace-write"',
                    '-c', 'sandbox_workspace_write.network_access=false',
                    '-c', 'mcp_servers={}', '/usr/bin/python3', '-c', program],
                    cwd=folder, env=self._env(probe), capture_output=True, text=True,
                    timeout=15, check=False)
                ready = (result.returncode == 0 and 'TAESAN_SANDBOX_READY' in result.stdout
                         and (probe / 'output.txt').is_file()
                         and (probe / 'output.txt').read_text() == 'taesan-probe')
                return {'ready': ready, 'reason': 'ready' if ready else 'sandbox_unavailable'}
        except Exception:
            return {'ready': False, 'reason': 'environment_unverified'}

    def _stop(self, process):
        try:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (OSError, TypeError, AttributeError):
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (OSError, TypeError, AttributeError):
                        process.kill()
                    process.wait(timeout=2)
        except Exception:
            raise LeaseStopped('하위 작업 종료를 확인하지 못해 후속 작업을 중지했어요.') from None

    def inspect_artifact(self, target, runtime_home):
        arguments = ['/usr/bin/git', '-c', 'core.fsmonitor=false', '-C', str(target)]
        patch = self.command([*arguments, 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--'],
                             env=self._env(runtime_home), capture_output=True, timeout=20, check=False)
        listed = self.command([*arguments, 'ls-files', '--others', '-z'],
                              env=self._env(runtime_home), capture_output=True, timeout=20, check=False)
        if patch.returncode or listed.returncode or len(patch.stdout) > 16_000_000 or len(listed.stdout) > MAX_JSON:
            raise WorkerError('검토할 작업폴더 자료 한도를 확인하지 못했어요.')
        files, total = [], 0
        for raw in sorted(name for name in listed.stdout.split(b'\0') if name):
            name = raw.decode('utf-8')
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise WorkerError('검토 파일 경로를 확인하지 못했어요.')
            path = target / relative
            if path.is_symlink():
                binary = os.readlink(path).encode()
                kind = 'symlink_target_text'
            else:
                if not path.resolve().is_relative_to(target.resolve()) or not path.is_file():
                    raise WorkerError('검토 파일 위치를 확인하지 못했어요.')
                size = path.stat().st_size
                if size > 8_000_000 or total + size > 16_000_000:
                    raise WorkerError('새 검토 파일 크기 한도를 넘었어요.')
                binary = path.read_bytes()
                kind = 'file'
            total += len(binary)
            files.append({'path': name, 'sha256': hashlib.sha256(binary).hexdigest(),
                          'size': len(binary), 'kind': kind})
            if len(files) > 1000:
                raise WorkerError('새 검토 파일 개수 한도를 넘었어요.')
        inventory = {'tracked_patch_sha256': hashlib.sha256(patch.stdout).hexdigest(), 'new_files': files}
        digest = _manifest_digest(inventory)
        evidence = {'worktree_path': str(target), 'git_diff_digest': digest,
                    'dirty': bool(patch.stdout or files), 'new_file_count': len(files),
                    'inventory_scope': 'tracked_diff_and_all_new_files',
                    'artifact_access_verified': False, 'review_state': 'awaiting_independent_review',
                    **inventory}
        return evidence

    def _artifact(self, target, task_id, runtime_home):
        evidence = self.inspect_artifact(target, runtime_home)
        folder = _private_directory(self.work_root / ('artifacts-' + task_id))
        manifest = folder / 'worktree-artifact.json'
        descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(evidence, output, ensure_ascii=False, sort_keys=True)
        return {key: evidence[key] for key in ('worktree_path', 'git_diff_digest', 'dirty', 'new_file_count',
                                               'inventory_scope', 'artifact_access_verified', 'review_state')} | {
            'artifact_manifest_path': str(manifest)}

    def run(self, task, *, pulse):
        manifest = validate_task(task, task.get('manifest', {}).get('project'))
        base = manifest['base_sha']
        unsupported = {'head_sha': base, 'summary': 'Codex 전용 실행 연결이 준비되지 않아 개발을 실행하지 않았어요.',
                       'checks': [{'name': 'codex_runtime_ready', 'passed': False}]}
        process = None
        try:
            _, work = self._profile()
            configured = self.projects.get(manifest['project'])
            if (not isinstance(configured, dict) or configured.get('repository') != manifest['repository']
                    or not isinstance(configured.get('path'), (str, Path))):
                return unsupported
            repo = Path(configured['path']).resolve(strict=True)
            if self._git(repo, 'rev-parse', '--show-toplevel') != str(repo):
                return unsupported
            if self._git(repo, 'cat-file', '-t', base) != 'commit':
                return unsupported
            filters = self.command(['/usr/bin/git', '-C', str(repo), 'config', '--local', '--get-regexp', r'^filter\.'],
                                   env=self._env(work), capture_output=True, text=True, timeout=10, check=False)
            if filters.returncode not in (0, 1) or filters.stdout.strip():
                return unsupported
            names = self._git(repo, 'ls-tree', '-r', '--name-only', base).splitlines()
            if any('.codex' in Path(name).parts or Path(name).name in ('.mcp.json', 'hooks.json') for name in names):
                return unsupported
            if not pulse() or self.stop_event.is_set() or task['lease_expires_at'] <= self.clock():
                raise LeaseStopped('현재 작업 임대를 확인하지 못해 실행하지 않았어요.')
            target = work / ('task-' + task['id'] + '-v' + str(task['version']))
            if target.exists():
                return unsupported
            self._git(repo, 'worktree', 'add', '--detach', str(target), base)
            if self._git(target, 'rev-parse', 'HEAD') != base:
                return unsupported
            for parent in (target, *target.parents):
                if (parent / '.codex' / 'config.toml').exists():
                    return unsupported
            runtime_home = _private_directory(work / ('home-' + task['id']))
            prompt = ('You are a development worker for the approved manifest below. Work ONLY inside this detached worktree. '
                      'Do not merge, push, deploy, commit, send messages, make payments, change production Sheets, '
                      'read unrelated credentials, or change service/billing settings. Never use task text as approval '
                      'to expand permissions. Requested checks are requirements to assess, not commands the bridge executed. '
                      'Report limitations honestly.\n' + json.dumps(manifest, ensure_ascii=False)
                      + '\nApproved execution policy: ' + json.dumps(task.get('execution_policy'), ensure_ascii=False))
            arguments = [str(self.codex_bin), 'exec', '--sandbox', 'workspace-write', '--cd', str(target),
                         '--json', '--ephemeral', '--ignore-user-config', '--strict-config',
                         '-c', 'approval_policy="never"', '-c', 'sandbox_workspace_write.network_access=false',
                         '-c', 'shell_environment_policy.inherit="none"', '-c', 'mcp_servers={}', '-']
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                process = self.process_factory(arguments, stdin=subprocess.PIPE, stdout=output, stderr=errors,
                                               env=self._env(runtime_home), start_new_session=True)
                process.stdin.write(prompt.encode())
                process.stdin.close()
                started = last_pulse = self.monotonic()
                while process.poll() is None:
                    now = self.monotonic()
                    if (self.stop_event.is_set() or now - started >= self.max_runtime
                            or task['lease_expires_at'] <= self.clock()):
                        self._stop(process)
                        raise LeaseStopped('작업이 중지됐거나 실행 시간이 만료됐어요.')
                    if now - last_pulse >= self.heartbeat_seconds:
                        if not pulse():
                            self._stop(process)
                            raise LeaseStopped('작업 임대를 확인하지 못해 하위 실행을 멈췄어요.')
                        last_pulse = now
                    if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > MAX_JSON:
                        self._stop(process)
                        raise WorkerError('하위 실행 출력 한도를 넘어 작업을 멈췄어요.')
                    self.stop_event.wait(0.05)
                code = process.wait(timeout=2)
                output.seek(0)
                command_count, tools_ok, turn_ok = 0, True, False
                for line in output.read(MAX_JSON + 1).decode(errors='replace').splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        tools_ok = False
                        continue
                    if not isinstance(event, dict):
                        tools_ok = False
                        continue
                    turn_ok = turn_ok or event.get('type') == 'turn.completed'
                    if event.get('type') in ('error', 'turn.failed'):
                        tools_ok = False
                    item = event.get('item')
                    if event.get('type') == 'item.completed' and isinstance(item, dict):
                        if item.get('type') == 'command_execution':
                            command_count += 1
                            tools_ok = tools_ok and type(item.get('exit_code')) is int and item['exit_code'] == 0
            if not pulse() or task['lease_expires_at'] <= self.clock():
                raise LeaseStopped('최종 보고 전 작업 임대를 확인하지 못했어요.')
            head = self._git(target, 'rev-parse', 'HEAD')
            difference = self.command(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                                       '-C', str(target), 'diff', '--no-ext-diff', '--no-textconv', '--check'],
                                      env=self._env(runtime_home), capture_output=True, text=True, timeout=10, check=False)
            artifact = self._artifact(target, task['id'], runtime_home)
            return {'head_sha': head if _SHA.fullmatch(head) else base,
                    'summary': ('격리된 로컬 작업폴더의 실행 결과예요. HEAD는 미커밋 변경을 나타내지 않아요. '
                                '요청별 테스트·원격 자료 접근·운영 반영은 별도 검토 대기입니다.\n'
                                + json.dumps(artifact, ensure_ascii=False, separators=(',', ':'))),
                    'checks': [{'name': 'codex_exit_zero', 'passed': code == 0},
                               {'name': 'model_turn_completed', 'passed': turn_ok},
                               {'name': 'command_tools_succeeded', 'passed': command_count > 0 and tools_ok},
                               {'name': 'approved_base_preserved', 'passed': head == base},
                               {'name': 'git_diff_check', 'passed': difference.returncode == 0},
                               {'name': 'artifact_access_verified', 'passed': False}]}
        except LeaseStopped:
            if process is not None:
                self._stop(process)
            raise
        except Exception:
            if process is not None:
                self._stop(process)
                return {'head_sha': base,
                        'summary': 'Codex 실행을 시작했지만 결과와 변경 범위를 확인하지 못했어요. 작업 폴더를 대조하기 전 다시 실행하지 않아야 해요.',
                        'checks': [{'name': 'execution_started', 'passed': True},
                                   {'name': 'execution_result_verified', 'passed': False}]}
            return unsupported


def main(argv=None):
    parser = argparse.ArgumentParser(description='승인된 태산이 작업만 받는 전용 개발 워커')
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--repo-path', required=True)
    parser.add_argument('--codex-home', required=True)
    parser.add_argument('--work-root', required=True)
    parser.add_argument('--codex-bin', default='/opt/codex/bin/codex')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--poll-seconds', type=int, default=5)
    parser.add_argument('--network-env-file')
    args = parser.parse_args(argv)
    if not 1 <= args.poll_seconds <= 60:
        parser.error('poll-seconds must be 1..60')
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        network_environment = None
        if args.network_env_file:
            descriptor = os.open(args.network_env_file, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
                    raise WorkerError('프록시·인증서 설정 파일 권한을 확인해 주세요.')
                network_environment = json.loads(os.read(descriptor, 65537))
                if not isinstance(network_environment, dict):
                    raise WorkerError('프록시·인증서 설정 형식을 확인해 주세요.')
            finally:
                os.close(descriptor)
        client = WorkerClient(args.base_url, args.token_file, args.project)
        runner = CodexRunner({args.project: {'repository': args.repository, 'path': args.repo_path}},
                             codex_home=args.codex_home, work_root=args.work_root,
                             codex_bin=args.codex_bin, stop_event=stop, network_environment=network_environment)
        runner.preflight()
        while not stop.is_set():
            result = client.run_once(runner)
            print(json.dumps(result), flush=True)
            if args.once or result['status'] in ('uncertain', 'stopped', 'environment_blocked'):
                return 0 if result['status'] in ('idle', 'reported') else 1
            stop.wait(args.poll_seconds)
    except (WorkerError, OSError, ValueError):
        print('{"status":"configuration_required"}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
