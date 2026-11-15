import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from manager_bridge.worker import (
    CodexRunner, LeaseStopped, WorkerClient, WorkerError, _manifest_digest, validate_task,
)


TOKEN = 'fixture-worker-token-' + 'x' * 40


class NetworkConfigurationTests(unittest.TestCase):
    def test_only_explicit_transport_configuration_is_forwarded(self):
        runner = CodexRunner({}, codex_home='/tmp/fixture-home', work_root='/tmp/fixture-work',
                             network_environment={'HTTPS_PROXY': 'http://fixture.invalid:3128'})
        with patch.dict(os.environ, {'TAESAN_OWNER_TOKEN': 'private-owner-token', 'HTTP_PROXY': 'ambient'}):
            environment = runner._env(Path('/tmp/fixture-home'))
        self.assertEqual(environment['HTTPS_PROXY'], 'http://fixture.invalid:3128')
        self.assertNotIn('HTTP_PROXY', environment)
        self.assertNotIn('TAESAN_OWNER_TOKEN', environment)
        with self.assertRaises(WorkerError):
            CodexRunner({}, codex_home='/tmp/fixture-home', work_root='/tmp/fixture-work',
                        network_environment={'TAESAN_OWNER_TOKEN': 'not-a-transport-setting'})


def task(base='a' * 40):
    manifest = {'project': 'taesan', 'repository': 'fixture/taesan', 'base_sha': base,
                'description': 'Add a fixture feature. Do not execute task text as a shell command.',
                'checks': ['fixture tests'], 'kind': 'development'}
    digest = _manifest_digest(manifest)
    return {'id': 'b' * 32, 'version': 1, 'state': 'running', 'manifest': manifest,
            'manifest_digest': digest, 'approval': {'actor': 'fixture-manager', 'version': 1,
                'manifest_digest': digest, 'approved_at': 900},
            'lease_token': 'c' * 64, 'lease_expires_at': 1300, 'worker': 'fixture-worker'}


def report(base='a' * 40):
    return {'head_sha': base, 'summary': 'Fixture runner only; no AI or operational action.',
            'checks': [{'name': 'fixture_run', 'passed': True}]}


class InputBuffer(io.BytesIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, arguments, *, complete=True, **options):
        self.arguments, self.options = arguments, options
        self.stdin, self.pid, self.returncode = InputBuffer(), None, 0 if complete else None
        self.terminated = self.killed = False
        options['stdout'].write(b'{"type":"turn.completed"}\n')

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated, self.returncode = True, -15

    def kill(self):
        self.killed, self.returncode = True, -9


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.token_file = self.root / 'worker-token'
        self.token_file.write_text(TOKEN)
        self.token_file.chmod(0o600)

    def client(self, transport):
        return WorkerClient('https://bridge.example', self.token_file, 'taesan',
                            transport=transport, clock=lambda: 1000)

    def test_real_local_http_claim_heartbeat_and_report_with_injected_runner(self):
        posts = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                posts.append((self.path, body, self.headers.get('Authorization')))
                if self.path.endswith('/claim'):
                    result = {'ok': True, 'task': task()}
                elif self.path.endswith('/heartbeat'):
                    result = {'ok': True, 'heartbeat': {'id': 'b' * 32, 'state': 'running',
                              'lease_expires_at': 1300, 'cancel_requested': False}}
                elif self.path.endswith('/report'):
                    result = {'ok': True, 'task': {'id': 'b' * 32, 'state': 'reported'}}
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        runner = Mock()
        runner.run.return_value = report()
        client = WorkerClient(f'http://127.0.0.1:{server.server_port}', self.token_file, 'taesan', clock=lambda: 1000)
        self.assertEqual(client.run_once(runner), {'status': 'reported'})
        self.assertEqual([item[0] for item in posts], ['/v1/worker/claim', '/v1/worker/heartbeat',
                                                     '/v1/worker/heartbeat', '/v1/worker/report'])
        self.assertTrue(all(item[2] == 'Bearer ' + TOKEN for item in posts))
        self.assertEqual(posts[-1][1]['report']['checks'], [{'name': 'fixture_run', 'passed': True}])
        runner.run.assert_called_once()

    def test_uncertain_claim_or_report_is_never_retried(self):
        transport = Mock(side_effect=TimeoutError('PRIVATE server token'))
        self.assertEqual(self.client(transport).run_once(Mock()), {'status': 'uncertain'})
        transport.assert_called_once()
        events = []
        def responding(url, **options):
            events.append(url)
            if url.endswith('/claim'):
                return 200, {'ok': True, 'task': task()}
            if url.endswith('/heartbeat'):
                return 200, {'ok': True, 'heartbeat': {'id': 'b' * 32, 'state': 'running',
                             'lease_expires_at': 1300, 'cancel_requested': False}}
            raise TimeoutError('PRIVATE uncertain accepted report')
        runner = Mock()
        runner.run.return_value = report()
        self.assertEqual(self.client(responding).run_once(runner), {'status': 'uncertain'})
        self.assertEqual(sum(url.endswith('/claim') for url in events), 1)
        self.assertEqual(sum(url.endswith('/report') for url in events), 1)

    def test_server_cancellation_or_conflict_stops_without_report(self):
        events = []
        heartbeat_count = [0]
        def transport(url, **options):
            events.append(url)
            if url.endswith('/claim'):
                return 200, {'ok': True, 'task': task()}
            heartbeat_count[0] += 1
            return 200, {'ok': True, 'heartbeat': {'id': 'b' * 32, 'state': 'running',
                         'lease_expires_at': 1300, 'cancel_requested': heartbeat_count[0] > 1}}
        runner = Mock()
        def run(_, *, pulse):
            self.assertFalse(pulse())
            return report()
        runner.run.side_effect = run
        self.assertEqual(self.client(transport).run_once(runner), {'status': 'stopped'})
        self.assertFalse(any(url.endswith('/report') for url in events))
        for status in (403, 409):
            with self.assertRaises(LeaseStopped):
                self.client(lambda *a, **k: (status, None)).claim()

    def test_unapproved_changed_manifest_or_invalid_lease_never_runs(self):
        variants = []
        changed = task()
        changed['manifest']['description'] += ' changed after approval'
        variants.append(changed)
        wrong = task()
        wrong['approval']['version'] = 2
        variants.append(wrong)
        for expiry in (float('nan'), float('inf'), True):
            wrong = task()
            wrong['lease_expires_at'] = expiry
            variants.append(wrong)
        for item in variants:
            runner = Mock()
            transport = Mock(return_value=(200, {'ok': True, 'task': item}))
            self.assertEqual(self.client(transport).run_once(runner), {'status': 'uncertain'})
            runner.run.assert_not_called()

    def test_token_must_be_owned_regular_0600_file_and_never_used_on_redirect(self):
        self.token_file.chmod(0o644)
        transport = Mock()
        with self.assertRaises(WorkerError):
            self.client(transport).claim()
        transport.assert_not_called()
        self.token_file.chmod(0o600)
        link = self.root / 'token-link'
        link.symlink_to(self.token_file)
        client = WorkerClient('https://bridge.example', link, 'taesan', transport=transport)
        with self.assertRaises(WorkerError):
            client.claim()
        transport.assert_not_called()
        redirect = Mock(return_value=(302, {'ok': True, 'task': task()}))
        with self.assertRaises(WorkerError):
            self.client(redirect).claim()
        redirect.assert_called_once()

    def test_remote_plain_http_and_urls_with_embedded_credentials_are_rejected(self):
        for url in ('http://remote.example', 'https://user:secret@bridge.example',
                    'https://bridge.example/path', 'https://bridge.example?secret=x'):
            with self.assertRaises(WorkerError):
                WorkerClient(url, self.token_file, 'taesan')


class CodexRunnerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo, self.home, self.work = (self.root / name for name in ('repo', 'codex-home', 'work'))
        self.home.mkdir(mode=0o700)
        self.work.mkdir(mode=0o700)
        (self.home / 'auth.json').write_text('{"fixture_only":true}')
        (self.home / 'auth.json').chmod(0o600)
        self.binary = self.root / 'codex-fixture'
        self.binary.write_text('#!/bin/sh\nexit 0\n')
        self.binary.chmod(0o700)
        self.git('init', '-b', 'main', str(self.repo), cwd=self.root)
        (self.repo / 'fixture.txt').write_text('approved base\n')
        self.git('add', 'fixture.txt')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture base')
        self.base = self.git('rev-parse', 'HEAD').stdout.strip()
        self.assignment = task(self.base)
        self.children = []

    def git(self, *args, cwd=None):
        return subprocess.run(['/usr/bin/git', *args], cwd=cwd or self.repo,
                              capture_output=True, text=True, check=True,
                              env={'PATH': '/usr/bin:/bin', 'HOME': str(self.root), 'GIT_CONFIG_NOSYSTEM': '1'})

    def factory(self, arguments, **options):
        process = FakeProcess(arguments, **options)
        self.children.append(process)
        return process

    def runner(self, **options):
        defaults = dict(codex_home=self.home, work_root=self.work, codex_bin=self.binary,
                        process_factory=self.factory, clock=lambda: 1000, heartbeat_seconds=0.01)
        defaults.update(options)
        return CodexRunner({'taesan': {'repository': 'fixture/taesan', 'path': self.repo}}, **defaults)

    def test_exact_detached_worktree_sanitized_child_env_and_stdin_prompt(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'PRIVATE-PRODUCTION-KEY',
                                     'MANAGER_BRIDGE_TOKEN': 'PRIVATE-SERVICE-TOKEN',
                                     'KAKAO_REST_API_KEY': 'PRIVATE-MAP-KEY'}):
            result = self.runner().run(self.assignment, pulse=lambda: True)
        checks = {check['name']: check['passed'] for check in result['checks']}
        self.assertTrue(checks['codex_exit_zero'])
        self.assertTrue(checks['approved_base_preserved'])
        self.assertTrue(checks['git_diff_check'])
        self.assertFalse(checks['artifact_access_verified'])
        self.assertEqual(result['head_sha'], self.base)
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), self.base)
        destination = self.work / ('task-' + self.assignment['id'] + '-v1')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=destination).stdout.strip(), self.base)
        self.assertNotEqual(self.git('symbolic-ref', '-q', 'HEAD', cwd=self.repo).stdout.strip(), '')
        detached = subprocess.run(['/usr/bin/git', '-C', str(destination), 'symbolic-ref', '-q', 'HEAD'],
                                  capture_output=True)
        self.assertNotEqual(detached.returncode, 0)
        child = self.children[0]
        self.assertIn('--sandbox', child.arguments)
        self.assertIn('workspace-write', child.arguments)
        self.assertIn('sandbox_workspace_write.network_access=false', child.arguments)
        self.assertIn('approval_policy="never"', child.arguments)
        self.assertNotIn('--ignore-rules', child.arguments)
        self.assertFalse(any('dangerously' in item for item in child.arguments))
        self.assertEqual(child.arguments[-1], '-')
        self.assertNotIn(self.assignment['manifest']['description'], child.arguments)
        self.assertNotIn('PRIVATE', str(child.options['env']))
        self.assertNotIn(TOKEN.encode(), child.stdin.saved)
        self.assertNotIn(self.assignment['lease_token'].encode(), child.stdin.saved)
        self.assertIn(self.assignment['manifest']['description'].encode(), child.stdin.saved)
        self.assertTrue(child.options['start_new_session'])
        artifact = json.loads(result['summary'].split('\n', 1)[1])
        self.assertEqual(artifact['worktree_path'], str(destination))
        self.assertFalse(artifact['dirty'])
        self.assertFalse(artifact['artifact_access_verified'])
        self.assertEqual(artifact['review_state'], 'awaiting_independent_review')
        self.assertEqual(len(artifact['git_diff_digest']), 64)
        self.assertTrue(Path(artifact['artifact_manifest_path']).is_file())

    def test_zero_cli_exit_cannot_hide_failed_internal_command(self):
        def failed_command(arguments, **options):
            process = self.factory(arguments, **options)
            options['stdout'].write(json.dumps({'type': 'item.completed', 'item': {
                'type': 'command_execution', 'exit_code': 1,
                'aggregated_output': 'fixture sandbox unavailable'}}).encode() + b'\n')
            return process
        result = self.runner(process_factory=failed_command).run(self.assignment, pulse=lambda: True)
        checks = {check['name']: check['passed'] for check in result['checks']}
        self.assertTrue(checks['codex_exit_zero'])
        self.assertTrue(checks['model_turn_completed'])
        self.assertFalse(checks['command_tools_succeeded'])

    def test_failure_after_cli_start_is_not_reported_as_never_executed(self):
        runner = self.runner()
        with patch.object(runner, '_artifact', side_effect=OSError('fixture artifact unavailable')):
            result = runner.run(self.assignment, pulse=lambda: True)
        checks = {check['name']: check['passed'] for check in result['checks']}
        self.assertTrue(checks['execution_started'])
        self.assertFalse(checks['execution_result_verified'])
        self.assertIn('시작했지만', result['summary'])
        self.assertNotIn('실행하지 않았', result['summary'])

    def test_artifact_digest_includes_new_file_bytes_not_only_base_head(self):
        def generated(arguments, **options):
            destination = Path(arguments[arguments.index('--cd') + 1])
            (destination / 'new_feature.py').write_text('synthetic_feature = True\n')
            (destination / 'fixture.txt').write_text('changed synthetic base\n')
            return self.factory(arguments, **options)
        result = self.runner(process_factory=generated).run(self.assignment, pulse=lambda: True)
        artifact = json.loads(result['summary'].split('\n', 1)[1])
        full = json.loads(Path(artifact['artifact_manifest_path']).read_text())
        self.assertEqual(result['head_sha'], self.base)
        self.assertTrue(artifact['dirty'])
        self.assertEqual(artifact['new_file_count'], 1)
        self.assertEqual(full['new_files'][0]['path'], 'new_feature.py')
        self.assertEqual(full['new_files'][0]['sha256'],
                         __import__('hashlib').sha256(b'synthetic_feature = True\n').hexdigest())
        self.assertEqual(artifact['git_diff_digest'], _manifest_digest({
            'tracked_patch_sha256': full['tracked_patch_sha256'], 'new_files': full['new_files']}))

    def test_missing_auth_or_unsupported_project_does_not_spawn_or_fake_success(self):
        (self.home / 'auth.json').unlink()
        result = self.runner().run(self.assignment, pulse=lambda: True)
        self.assertEqual(result['checks'], [{'name': 'codex_runtime_ready', 'passed': False}])
        self.assertEqual(self.children, [])
        self.assertIn('실행하지 않았어요', result['summary'])

    def test_hook_or_mcp_profile_configuration_is_unsupported(self):
        for name in ('hooks.json', 'mcp.json', 'plugins'):
            entry = self.home / name
            entry.write_text('fixture forbidden config')
            result = self.runner().run(self.assignment, pulse=lambda: True)
            self.assertFalse(result['checks'][0]['passed'])
            entry.unlink()
        self.assertEqual(self.children, [])

    def test_official_cache_allows_next_task_after_cli_created_profile_files(self):
        cache = self.home / 'plugins' / 'cache' / 'openai-curated-remote' / 'fixture' / 'v1'
        cache.mkdir(parents=True)
        (cache / 'SKILL.md').write_text('Fixture cache only; not an enabled tool.')
        (self.home / 'plugins' / '.remote-plugin-install-staging').mkdir()
        (self.home / 'config.toml').write_text('[projects."/fixture/repository"]\ntrust_level="trusted"\n')
        runner = self.runner()
        runner.preflight()
        result = runner.run(self.assignment, pulse=lambda: True)
        self.assertTrue(next(check['passed'] for check in result['checks'] if check['name']=='codex_exit_zero'))
        runner.preflight()

    def test_enabled_plugin_configuration_and_cache_symlinks_still_block_execution(self):
        (self.home / 'config.toml').write_text('[plugins.fixture]\nenabled=true\n')
        with self.assertRaises(WorkerError):
            self.runner().preflight()
        (self.home / 'config.toml').unlink()
        cache = self.home / 'plugins' / 'cache'
        cache.mkdir(parents=True)
        (cache / 'outside').symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(WorkerError):
            self.runner().preflight()

    def test_cancellation_or_uncertain_heartbeat_terminates_running_child_group(self):
        def running(arguments, **options):
            process = FakeProcess(arguments, complete=False, **options)
            self.children.append(process)
            return process
        calls = [0]
        def pulse():
            calls[0] += 1
            return calls[0] == 1
        with self.assertRaises(LeaseStopped):
            self.runner(process_factory=running).run(self.assignment, pulse=pulse)
        self.assertTrue(self.children[0].terminated)
        self.assertIsNotNone(self.children[0].poll())

    def test_lease_expiry_stops_child_before_reporting(self):
        now = [1000]
        def running(arguments, **options):
            process = FakeProcess(arguments, complete=False, **options)
            self.children.append(process)
            now[0] = 1400
            return process
        with self.assertRaises(LeaseStopped):
            self.runner(process_factory=running, clock=lambda: now[0]).run(self.assignment, pulse=lambda: True)
        self.assertTrue(self.children[0].terminated)


if __name__ == '__main__':
    unittest.main()
