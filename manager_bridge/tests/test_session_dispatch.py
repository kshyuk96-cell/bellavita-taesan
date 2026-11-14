import hashlib
import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from manager_bridge.session_dispatch import DispatchRejected, SessionTarget, dispatch_session


class SessionDispatchTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home, self.repo = folder / 'profile', folder / 'checkout'
        self.home.mkdir(mode=0o700)
        self.repo.mkdir()
        self.secret = folder / 'remote.token'
        self.secret.write_text('fixture-private-token-abcdefghijklmno')
        self.secret.chmod(0o600)
        self.target = SessionTarget('fixture', 'fixture/example', '11111111-1111-4111-8111-111111111111',
            str(self.home), str(self.repo), '/fixture/bin/codex', allowed_base_shas=('a' * 40,))
        manifest = dict(project='fixture', repository='fixture/example', base_sha='a' * 40,
                        description='합성 읽기 기능을 구현하고 검증합니다.', checks=['합성 단위 시험'], kind='development')
        digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.task = dict(id='b' * 32, version=1, state='running', worker='worker', manifest=manifest, manifest_digest=digest,
                         approval={'actor': 'owner', 'version': 1, 'manifest_digest': digest, 'approved_at': time.time() - 1},
                         lease_token='c' * 64,
                         lease_expires_at=time.time() + 300)
        self.run = Mock(return_value=SimpleNamespace(returncode=0, stdout='private task output', stderr='private warning'))
        self.verify = Mock(return_value=True)

    def dispatch(self, target=None, task=None, **options):
        return dispatch_session('worker', self.task if task is None else task,
            registry={'worker': target or self.target}, validate_task=self.verify, runner=self.run, **options)

    def test_registered_local_session_receives_exact_manifest_without_authority_or_lease_secrets(self):
        result = self.dispatch()
        self.assertEqual(result['status'], 'queued')
        self.assertFalse(result['execution_verified'])
        command = self.run.call_args.args[0]
        options = self.run.call_args.kwargs
        self.assertEqual(command[:4], ['/fixture/bin/codex', 'queue', '--thread', self.target.session_uuid])
        message = command[command.index('--message') + 1]
        self.assertIn('main 병합', message)
        self.assertIn('a' * 40, message)
        self.assertNotIn(self.task['lease_token'], message)
        self.assertNotIn('approval', message)
        self.assertEqual(command[-2:], ['--cd', str(self.repo)])
        self.assertFalse(options['shell'])
        self.assertEqual(options['timeout'], 20)
        self.assertEqual(options['stdout'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(self.verify.call_count, 2)
        self.assertNotIn('private task output', json.dumps(result))

    def test_remote_token_only_in_fixed_environment_name_never_argv_and_resume_remains_unverified(self):
        target = replace(self.target, remote='wss://fixture.invalid:443', credential_file=str(self.secret), resume=True)
        result = self.dispatch(target)
        command, options = self.run.call_args.args[0], self.run.call_args.kwargs
        token = self.secret.read_text()
        self.assertIn('--resume', command)
        self.assertEqual(command[command.index('--remote-auth-token-env') + 1], 'TAESAN_CODEX_SESSION_TOKEN')
        self.assertNotIn(token, repr(command))
        self.assertEqual(options['env']['TAESAN_CODEX_SESSION_TOKEN'], token)
        self.assertFalse(result['execution_verified'])
        self.assertNotIn(token, json.dumps(result))

    def test_private_or_production_environment_is_not_inherited(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'private-production-key', 'TAESAN_OWNER_TOKEN': 'private-owner'}):
            self.dispatch()
        environment = self.run.call_args.kwargs['env']
        self.assertEqual(environment['CODEX_HOME'], str(self.home))
        self.assertEqual(environment['HOME'], str(self.home))
        self.assertNotIn('OPENAI_API_KEY', environment)
        self.assertNotIn('TAESAN_OWNER_TOKEN', environment)

    def test_nonzero_timeout_and_exception_are_uncertain_without_private_outputs_or_retry(self):
        for outcome in (SimpleNamespace(returncode=1), subprocess.TimeoutExpired(['private-argv'], 20),
                        OSError('private path and token')):
            self.run.reset_mock()
            if isinstance(outcome, Exception):
                self.run.side_effect = outcome
            else:
                self.run.side_effect = None
                self.run.return_value = outcome
            result = self.dispatch()
            self.assertEqual(result['status'], 'unverified')
            self.assertFalse(result['automatic_retry_safe'])
            self.assertNotIn('private', json.dumps(result))
            self.run.assert_called_once()

    def test_approval_revocation_or_expired_lease_denies_before_cli(self):
        for verification in (False, PermissionError('private verifier details')):
            self.verify.side_effect = verification if isinstance(verification, Exception) else None
            self.verify.return_value = verification
            with self.assertRaises(DispatchRejected):
                self.dispatch()
        self.verify.side_effect = [True, False]
        with self.assertRaises(DispatchRejected):
            self.dispatch()
        self.verify.side_effect = None
        self.verify.return_value = True
        with self.assertRaises(DispatchRejected):
            self.dispatch(task={**self.task, 'lease_expires_at': time.time() - 1})
        self.run.assert_not_called()

    def test_changed_manifest_project_repository_base_or_arbitrary_session_fields_cannot_redirect(self):
        for change in ({'project': 'other'}, {'repository': 'other/repo'}, {'base_sha': 'c' * 40},
                       {'session_uuid': '22222222-2222-4222-8222-222222222222'}, {'kind': 'deploy'}):
            modified = {**self.task, 'manifest': {**self.task['manifest'], **change}}
            with self.subTest(change=change), self.assertRaises(DispatchRejected):
                self.dispatch(task=modified)
        with self.assertRaises(DispatchRejected):
            self.dispatch(task={**self.task, 'manifest_digest': 'd' * 64})
        self.run.assert_not_called()

    def test_actual_store_worker_and_approval_bindings_are_required(self):
        for changes in ({'worker': 'other'}, {'approval': {**self.task['approval'], 'version': 2}},
                        {'approval': {**self.task['approval'], 'manifest_digest': 'd' * 64}},
                        {'approval': {}}, {'version': 2}):
            with self.subTest(changes=changes), self.assertRaises(DispatchRejected):
                self.dispatch(task={**self.task, **changes})
        self.run.assert_not_called()

    def test_allowed_bases_from_a_json_registry_are_frozen_and_usable(self):
        bases = ['a' * 40]
        target = replace(self.target, allowed_base_shas=bases)
        bases.append('d' * 40)
        self.assertEqual(target.allowed_base_shas, ('a' * 40,))
        self.assertEqual(self.dispatch(target)['status'], 'queued')

    def test_registry_is_required_uuid_only_and_insecure_or_credential_url_is_rejected(self):
        for target in (replace(self.target, session_uuid='friendly-name'),
                       replace(self.target, remote='https://fixture.invalid'),
                       replace(self.target, remote='ws://remote.invalid:80'),
                       replace(self.target, remote='wss://token@fixture.invalid'),
                       replace(self.target, remote='wss://fixture.invalid?token=secret')):
            with self.subTest(target=target), self.assertRaises(DispatchRejected):
                self.dispatch(target)
        with self.assertRaises(DispatchRejected):
            dispatch_session('other', self.task, registry={'worker': self.target},
                             validate_task=self.verify, runner=self.run)
        self.run.assert_not_called()

    def test_unix_remote_supported_and_private_credential_file_failures_are_safe(self):
        self.dispatch(replace(self.target, remote='unix:///tmp/fixture.sock'))
        self.run.reset_mock()
        self.secret.chmod(0o644)
        with self.assertRaises(DispatchRejected) as error:
            self.dispatch(replace(self.target, remote='wss://fixture.invalid', credential_file=str(self.secret)))
        self.assertNotIn(str(self.secret), str(error.exception))
        self.run.assert_not_called()

    def test_mutating_verifier_cannot_change_frozen_sent_scope(self):
        def mutate(task, target):
            task['manifest']['description'] = 'malicious modified scope'
            return True
        self.verify.side_effect = mutate
        self.dispatch()
        message = self.run.call_args.args[0][5]
        self.assertNotIn('malicious modified scope', message)
        self.assertEqual(self.task['manifest']['description'], '합성 읽기 기능을 구현하고 검증합니다.')

    def test_actual_subprocess_with_fake_cli_keeps_token_out_of_argv_and_output_private(self):
        executable = self.repo / 'fixture-codex'
        executable.write_text('#!/usr/bin/python3\n'
            'import os, sys\n'
            'token=os.environ["TAESAN_CODEX_SESSION_TOKEN"]\n'
            'assert token not in repr(sys.argv)\n'
            'assert "OPENAI_API_KEY" not in os.environ\n'
            'assert sys.argv[1]=="queue"\n'
            'print("private fixture stdout " + token)\n'
            'print("private fixture stderr " + token, file=sys.stderr)\n')
        executable.chmod(0o700)
        target = replace(self.target, codex_binary=str(executable), remote='wss://fixture.invalid',
                         credential_file=str(self.secret), resume=True)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch.dict(os.environ, {'OPENAI_API_KEY': 'private-production-key'}):
            result = dispatch_session('worker', self.task, registry={'worker': target}, validate_task=self.verify)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), '')
        self.assertNotIn(self.secret.read_text(), json.dumps(result))

    def test_remote_secret_cannot_be_repeated_inside_an_approved_task_message(self):
        manifest = {**self.task['manifest'], 'description': self.secret.read_text()}
        digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        task = {**self.task, 'manifest': manifest, 'manifest_digest': digest,
                'approval': {**self.task['approval'], 'manifest_digest': digest}}
        with self.assertRaises(DispatchRejected):
            self.dispatch(replace(self.target, remote='wss://fixture.invalid', credential_file=str(self.secret)), task=task)
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
