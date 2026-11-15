import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from manager_bridge.store import Store, PermissionDenied
from manager_bridge.supervisor import VerificationBlocked, recovery_action, verify_report
from manager_bridge.worker import CodexRunner, WorkerClient


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root.chmod(0o700)
        self.store = Store(self.root / 'state.db', clock=lambda: 1000)
        self.owner = self.store.add_token('owner', 'owner', ['*'])
        self.store.register_project(self.owner, 'a', 'fixture/repo')
        self.store.register_project(self.owner, 'b', 'fixture/repo')
        self.manager = self.store.add_token('manager', 'manager', ['a'])
        self.worker = self.store.add_token('worker', 'worker', ['a'])
        self.other = self.store.add_token('other', 'worker', ['b'])
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        (self.repo / 'README.md').write_text('fixture\n')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'fixture')
        self.base = self.git('rev-parse', 'HEAD')
        self.work = self.root / 'work'
        self.work.mkdir(mode=0o700)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.runner = CodexRunner({}, codex_home=self.home, work_root=self.work)

    def git(self, *args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(self.repo), *args], text=True).strip()

    def approved(self, key='one', project='a'):
        manifest = {'project': project, 'repository': 'fixture/repo', 'base_sha': self.base,
                    'description': 'fixture', 'checks': ['read fixture'], 'kind': 'development'}
        task = self.store.submit(self.owner, key, manifest)
        return self.store.approve(self.owner, task['id'], task['version'], task['manifest_digest'])

    def reported(self):
        self.approved()
        task = self.store.claim(self.worker, 'a')
        target = self.work / ('task-' + task['id'] + '-v1')
        self.git('worktree', 'add', '--detach', '-q', str(target), self.base)
        artifact = self.runner._artifact(target, task['id'], self.work)
        report = {'head_sha': self.base, 'summary': 'fixture\n' + json.dumps(artifact),
                  'checks': [{'name': name, 'passed': True} for name in
                             ('codex_exit_zero', 'model_turn_completed', 'command_tools_succeeded',
                              'approved_base_preserved', 'git_diff_check')]}
        return self.store.report(self.worker, task['id'], task['lease_token'], report), target

    def commands(self, program=None):
        return {'read fixture': ['/usr/bin/python3', '-c', program or
                                "from pathlib import Path; assert Path('README.md').read_text() == 'fixture\\n'"]}

    def test_failed_readiness_never_claims_or_calls_model(self):
        self.runner.check_environment = Mock(return_value={'ready': False, 'reason': 'sandbox_unavailable'})
        transport = Mock()
        client = WorkerClient('http://127.0.0.1:8791', self.root/'unused.token', 'a', transport=transport)
        self.assertEqual(client.run_once(self.runner)['status'], 'environment_blocked')
        transport.assert_not_called()

    def test_real_sandbox_probe_requires_write_evidence_and_does_not_call_model(self):
        self.runner.preflight = Mock()
        self.runner.command = Mock(return_value=subprocess.CompletedProcess([], 0, 'TAESAN_SANDBOX_READY', ''))
        self.assertFalse(self.runner.check_environment()['ready'])  # A printed claim alone is insufficient.
        def successful(argv, **kwargs):
            Path(kwargs['cwd'], 'output.txt').write_text('taesan-probe')
            self.assertIn('sandbox', argv)
            self.assertNotIn('exec', argv)
            self.assertIn('sandbox_workspace_write.network_access=false', argv)
            return subprocess.CompletedProcess(argv, 0, 'TAESAN_SANDBOX_READY', '')
        self.runner.command.side_effect = successful
        self.assertTrue(self.runner.check_environment()['ready'])
        self.assertFalse(list(self.work.iterdir()))

    def test_cross_project_same_repository_conflict_survives_restart_and_report(self):
        first = self.approved()
        self.approved('two', 'b')
        claimed = self.store.claim(self.worker, 'a')
        restarted = Store(self.root / 'state.db', clock=lambda: 1000)
        self.assertIsNone(restarted.claim(self.other, 'b'))
        report = {'head_sha': self.base, 'summary': 'fixture', 'checks': [{'name': 'fixture', 'passed': True}]}
        restarted.report(self.worker, first['id'], claimed['lease_token'], report)
        self.assertIsNone(restarted.claim(self.other, 'b'))
        restarted.review(self.manager, first['id'], 1, first['manifest_digest'], 'explicit-manager-fixture')
        self.assertIsNotNone(restarted.claim(self.other, 'b'))

    def test_expired_uncertain_repository_is_not_reassigned(self):
        first = self.approved()
        self.store.claim(self.worker, 'a', lease_seconds=10)
        self.approved('two')
        restarted = Store(self.root / 'state.db', clock=lambda: 2000)
        self.assertIsNone(restarted.claim(self.worker, 'a'))
        tasks = restarted.list_tasks(self.manager, 'a')
        expired = next(task for task in tasks if task['id'] == first['id'])
        self.assertEqual(expired['state'], 'needs_verification')
        self.assertEqual(recovery_action(expired), 'inspect_existing_worktree_no_replay')

    def test_independent_commands_artifacts_and_exact_report_are_reviewed(self):
        task, target = self.reported()
        reviewed = verify_report(self.store, self.manager, task['id'], self.runner, self.commands())
        self.assertEqual(reviewed['state'], 'reviewed')
        self.assertTrue(reviewed['review']['evidence_reference'].startswith('independent-artifact:'))
        evidence = list((self.work / ('artifacts-' + task['id'])).glob('independent-review-*.json'))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].stat().st_mode & 0o777, 0o600)

    def test_changed_files_block_review_even_if_worker_claimed_success(self):
        task, target = self.reported()
        (target / 'README.md').write_text('changed after report')
        with self.assertRaises(VerificationBlocked):
            verify_report(self.store, self.manager, task['id'], self.runner, self.commands())
        self.assertEqual(self.store.get_task(self.manager, task['id'])['state'], 'reported')

    def test_untracked_result_tampering_blocks_review(self):
        task, target = self.reported()
        (target / 'new-result.txt').write_text('unreported')
        with self.assertRaises(VerificationBlocked):
            verify_report(self.store, self.manager, task['id'], self.runner, self.commands())

    def test_failing_or_mutating_verification_does_not_review(self):
        task, target = self.reported()
        with self.assertRaises(VerificationBlocked):
            verify_report(self.store, self.manager, task['id'], self.runner, self.commands('raise SystemExit(1)'))
        with self.assertRaises(VerificationBlocked):
            verify_report(self.store, self.manager, task['id'], self.runner,
                          self.commands("from pathlib import Path; Path('README.md').write_text('mutated')"))
        self.assertEqual(self.store.get_task(self.manager, task['id'])['state'], 'reported')

    def test_missing_required_check_and_worker_role_cannot_review(self):
        task, _ = self.reported()
        with self.assertRaises(VerificationBlocked):
            verify_report(self.store, self.manager, task['id'], self.runner, {})
        with self.assertRaises(VerificationBlocked):
            verify_report(self.store, self.worker, task['id'], self.runner, self.commands())

    def test_overview_counts_every_task_and_reveals_no_task_text_or_tokens(self):
        self.approved()
        self.store.claim(self.worker, 'a')
        result = self.store.overview(self.manager, 'a')
        self.assertEqual(result['counts']['running'], 1)
        self.assertEqual(result['actions'][0]['action'], 'reconcile_existing_worker')
        raw = json.dumps(result)
        for value in (self.owner, self.worker, 'description', 'lease_token'):
            self.assertNotIn(value, raw)
        self.assertFalse(result['operational_deployment_verified'])
        with self.assertRaises(PermissionDenied):
            self.store.overview(self.worker, 'a')
        self.assertNotIn('running', self.store.overview(self.owner, 'b')['actions'])


if __name__ == '__main__':
    unittest.main()
