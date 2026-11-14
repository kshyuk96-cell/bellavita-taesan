import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent


class LedgerAccessCommandTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = patch.object(agent, 'ROOT', Path(self.folder.name))
        self.root.start()
        self.probe = Mock(return_value={'summary': '실제 편집 권한 미확인', 'status': 'unverified'})
        self.module = patch.dict('sys.modules', {'ledger_access': SimpleNamespace(diagnose=self.probe)})
        self.module.start()

    def tearDown(self):
        self.module.stop()
        self.root.stop()
        self.folder.cleanup()

    def test_staff_cannot_probe_service_account(self):
        with patch.object(agent, 'role_of', return_value='staff'):
            result = agent.ledger_access_status('employee')
        self.probe.assert_not_called()
        self.assertIn('대표', result['summary'])

    def test_access_revoked_during_probe_hides_principal(self):
        self.probe.return_value = {'summary': 'private principal', 'status': 'allowed'}
        with patch.object(agent, 'role_of', side_effect=['admin', 'staff']):
            result = agent.ledger_access_status('owner')
        self.assertNotIn('private', result['summary'])
        self.assertNotIn('ledger_access_status', result)

    def test_status_is_actual_probe_and_not_permission_grant(self):
        with patch.object(agent, 'role_of', return_value='admin'):
            result = agent.ledger_access_status('owner')
        self.probe.assert_called_once_with()
        self.assertEqual(result['ledger_access_status'], 'unverified')
        self.assertNotIn('success', result)

    def test_old_diagnostic_is_hidden_after_role_change(self):
        with patch.object(agent, 'role_of', return_value='staff'):
            result = agent.present('a'*12, {'summary': 'private principal', 'ledger_access': True}, 'owner')
        self.assertNotIn('private', str(result))

    def test_staff_dispatch_never_submits_or_calls_model(self):
        with patch.object(agent, 'role_of', return_value='staff'), patch.object(agent.POOL, 'submit') as submit:
            result = agent.dispatch({'userRequest': {'user': {'id': 'staff'}, 'utterance': '원장 권한 확인'}})
        submit.assert_not_called()
        self.assertIn('대표', str(result))
