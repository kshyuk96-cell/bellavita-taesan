import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent


class LedgerPatchRouteTests(unittest.TestCase):
    def request(self, payload, *, origin='https://fixture.invalid', owner='owner'):
        body = json.dumps(payload).encode()
        request = object.__new__(agent.Handler)
        request.path = '/staff-orders/ledger-patch'
        request.headers = {'Origin': origin, 'Content-Length': str(len(body))}
        request.rfile = io.BytesIO(body)
        request.staff_web_owner = Mock(return_value=owner)
        request.send_json = Mock()
        return request

    def test_staff_never_constructs_write_service(self):
        request = self.request({'action': 'apply'})
        with patch.object(agent, 'role_of', return_value='staff'), patch.dict('sys.modules', {'ledger_patch': Mock()}) as modules:
            agent.Handler.do_POST(request)
            modules['ledger_patch'].LedgerPatchService.assert_not_called()
        self.assertEqual(request.send_json.call_args.args[1], 403)

    def test_wrong_origin_denied_before_service(self):
        request = self.request({'action': 'prepare'}, origin='https://other.invalid')
        with patch.object(agent, 'role_of', return_value='admin'), patch.dict('os.environ', {'TAESAN_PUBLIC_URL': 'https://fixture.invalid'}):
            agent.Handler.do_POST(request)
        self.assertEqual(request.send_json.call_args.args[1], 403)

    def test_prepare_uses_server_identity_target_and_audit_not_payload(self):
        request = self.request({'action': 'prepare', 'range': "'액자화환'!B5", 'values': [['x']],
                                'spreadsheet_id': 'attacker_target', 'role': 'admin'})
        service = Mock()
        service.prepare.return_value = {'status': 'prepared'}
        factory = Mock(return_value=service)
        with patch.object(agent, 'role_of', return_value='admin'), patch.dict('os.environ', {'TAESAN_PUBLIC_URL': 'https://fixture.invalid'}), patch.dict('sys.modules', {'ledger_patch': SimpleNamespace(LedgerPatchService=factory)}):
            agent.Handler.do_POST(request)
        service.prepare.assert_called_once_with('owner', "'액자화환'!B5", [['x']])
        self.assertNotIn('env', factory.call_args.kwargs)
        self.assertEqual(request.send_json.call_args.args[1], 200)

    def test_revoked_role_hides_verified_response(self):
        request = self.request({'action': 'prepare', 'range': 'B5', 'values': [[1]]})
        service = Mock()
        service.prepare.return_value = {'status': 'prepared', 'private': 'data'}
        with patch.object(agent, 'role_of', side_effect=['admin', 'staff']), patch.dict('os.environ', {'TAESAN_PUBLIC_URL': 'https://fixture.invalid'}), patch.dict('sys.modules', {'ledger_patch': SimpleNamespace(LedgerPatchService=Mock(return_value=service))}):
            agent.Handler.do_POST(request)
        self.assertNotIn('private', str(request.send_json.call_args))
        self.assertEqual(request.send_json.call_args.args[1], 403)
