import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from manager_bridge import control
from manager_bridge.store import Store


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.data = Path(self.folder.name) / 'private-manager'
        code, result = self.run_cli('init', '--data', str(self.data), '--project', 'flowers',
                                    '--repository', 'fixture/flowers')
        self.assertEqual(code, 0, result)

    def tearDown(self):
        self.folder.cleanup()

    def run_cli(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = control.main(list(arguments))
        return code, json.loads(output.getvalue())

    def test_bootstrap_outputs_paths_not_tokens_and_refuses_overwrite(self):
        owner = control.secret(self.data / 'owner.token')
        code, result = self.run_cli('init', '--data', str(self.data), '--project', 'other', '--repository', 'fixture/other')
        self.assertEqual(code, 2)
        self.assertNotIn(owner, json.dumps(result))
        self.assertEqual(control.secret(self.data / 'owner.token'), owner)

    def test_duplicate_worker_registration_preserves_existing_connection(self):
        arguments = ('issue-worker', '--data', str(self.data), '--owner-token-file', str(self.data / 'owner.token'),
                     '--project', 'flowers', '--name', 'developer')
        code, result = self.run_cli(*arguments)
        self.assertEqual(code, 0, result)
        token = control.secret(result['worker_token_file'])
        self.assertNotIn(token, json.dumps(result))
        code, _ = self.run_cli(*arguments)
        self.assertEqual(code, 2)
        self.assertEqual(Store(self.data / 'manager.sqlite3').authenticate(token)['role'], 'worker')

    def test_owner_can_register_business_and_scope_one_manager_to_both(self):
        owner_file = str(self.data / 'owner.token')
        code, result = self.run_cli('register-project', '--data', str(self.data), '--owner-token-file', owner_file,
                                    '--project', 'camp', '--repository', 'fixture/camp')
        self.assertEqual(code, 0, result)
        code, result = self.run_cli('issue-manager', '--data', str(self.data), '--owner-token-file', owner_file,
                                    '--projects', 'flowers,camp', '--name', 'taesan-main')
        self.assertEqual(code, 0, result)
        identity = Store(self.data / 'manager.sqlite3').authenticate(control.secret(result['manager_token_file']))
        self.assertEqual(set(identity['projects']), {'flowers', 'camp'})
        code, _ = self.run_cli('register-project', '--data', str(self.data), '--owner-token-file',
                               str(self.data / 'manager.token'), '--project', 'unauthorized', '--repository', 'fixture/unauthorized')
        self.assertEqual(code, 2)

    def test_scoped_owner_cannot_issue_worker_for_another_business(self):
        store = Store(self.data / 'manager.sqlite3')
        limited = store.add_token('limited-owner', 'owner', ['flowers'])
        destination = self.data / 'limited-owner.token'
        control.write_secret(destination, limited)
        code, _ = self.run_cli('issue-worker', '--data', str(self.data), '--owner-token-file', str(destination),
                               '--project', 'camp', '--name', 'outside-scope')
        self.assertEqual(code, 2)
        self.assertFalse((self.data / 'worker-outside-scope.token').exists())
