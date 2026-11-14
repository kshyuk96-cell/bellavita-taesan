import contextlib
import json
import unittest
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ledger_access as access


TARGET = 'fixture_ledger_1234567890'
OTHER = 'fixture_other_1234567890'
EMAIL = 'fixture-only@fixture-project.iam.gserviceaccount.com'


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.closed = False
    def json(self):
        return self.payload
    def close(self):
        self.closed = True


def metadata(can_edit=True, target=TARGET):
    return {'id': target, 'mimeType': access.SHEET_MIME, 'capabilities': {'canEdit': can_edit}}


class LedgerAccessTests(unittest.TestCase):
    def run_case(self, response, principal=EMAIL, env=None):
        credentials = SimpleNamespace(service_account_email=principal)
        transport = Mock()
        transport.get.return_value = response
        self.result = access.diagnose(env or {'TAESAN_SHEET_ID': TARGET},
            credentials_loader=lambda _: credentials, session_factory=lambda _: transport)
        return self.result, transport

    def test_actual_credential_principal_and_true_capability_confirm_only_file_edit_access(self):
        response = Response(metadata())
        result, session = self.run_case(response)
        self.assertEqual(result['status'], 'allowed')
        self.assertEqual(result['principal'], EMAIL)
        self.assertTrue(result['principal_verified'])
        self.assertEqual(result['scope'], 'file_permission_only')
        self.assertIn('보호된 범위', result['summary'])
        self.assertEqual(session.get.call_args.args[0], 'https://www.googleapis.com/drive/v3/files/' + TARGET)
        self.assertEqual(session.get.call_args.kwargs['params']['fields'], 'id,mimeType,capabilities(canEdit)')
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertTrue(response.closed)
        session.close.assert_called_once()

    def test_order_sheet_target_precedence_is_exact_not_a_caller_selected_file(self):
        result, session = self.run_case(Response(metadata(target=OTHER)), env={
            'TAESAN_ORDER_SHEET_ID': OTHER, 'TAESAN_SHEET_ID': TARGET})
        self.assertEqual(result['ledger_id'], OTHER)
        self.assertEqual(result['status'], 'allowed')

    def test_missing_or_invalid_target_never_loads_credentials_or_calls_network(self):
        loader, factory = Mock(), Mock()
        for env in ({}, {'TAESAN_SHEET_ID': 'https://example.invalid/private-token'}):
            result = access.diagnose(env, credentials_loader=loader, session_factory=factory)
            self.assertEqual(result['status'], 'unverified')
            self.assertNotIn('private-token', json.dumps(result))
        loader.assert_not_called()
        factory.assert_not_called()

    def test_false_capability_is_denied_but_missing_or_truthy_non_boolean_is_unverified(self):
        for value, status in ((False, 'denied'), (None, 'unverified'), ('true', 'unverified'), (1, 'unverified')):
            with self.subTest(value=value):
                result, _ = self.run_case(Response(metadata(value)))
                self.assertEqual(result['status'], status)

    def test_metadata_scopes_are_not_interpreted_as_file_edit_permission(self):
        credentials = SimpleNamespace(service_account_email=EMAIL, scopes=[access.SHEETS_SCOPE])
        transport = Mock()
        transport.get.return_value = Response(metadata(False))
        result = access.diagnose({'TAESAN_SHEET_ID': TARGET}, credentials_loader=lambda _: credentials,
                                 session_factory=lambda _: transport)
        self.assertEqual(result['status'], 'denied')

    def test_disabled_api_insufficient_scope_forbidden_and_404_are_distinct(self):
        for status, reason, expected_status, code in (
            (403, 'accessNotConfigured', 'unverified', 'drive_api_disabled'),
            (403, 'insufficientPermissions', 'unverified', 'metadata_scope_required'),
            (403, 'rateLimitExceeded', 'unverified', 'metadata_rate_limited'),
            (403, 'userRateLimitExceeded', 'unverified', 'metadata_rate_limited'),
            (403, 'dailyLimitExceeded', 'unverified', 'metadata_rate_limited'),
            (403, 'forbidden', 'denied', 'permission_denied'),
            (404, 'notFound', 'unverified', 'target_not_found_or_hidden'),
            (401, 'authError', 'unverified', 'authentication_required'),
            (503, 'backendError', 'unverified', 'metadata_unavailable'),
        ):
            with self.subTest(status=status, reason=reason):
                result, _ = self.run_case(Response({'error': {'errors': [{'reason': reason}]}}, status))
                self.assertEqual((result['status'], result['code']), (expected_status, code))

    def test_adc_principal_comes_from_authenticated_about_not_file_readers(self):
        session = Mock()
        session.get.side_effect = [Response({'user': {'emailAddress': 'fixture-user@example.invalid'}}), Response(metadata())]
        result = access.diagnose({'TAESAN_SHEET_ID': TARGET}, credentials_loader=lambda _: object(),
                                 session_factory=lambda _: session)
        self.assertEqual(result['principal'], 'fixture-user@example.invalid')
        self.assertEqual(result['status'], 'allowed')
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args_list[0].args[0], 'https://www.googleapis.com/drive/v3/about')

    def test_unknown_principal_stays_unknown_even_when_file_edit_capability_is_observed(self):
        session = Mock()
        session.get.side_effect = [Response({}, 403), Response(metadata())]
        result = access.diagnose({'TAESAN_SHEET_ID': TARGET}, credentials_loader=lambda _: object(),
                                 session_factory=lambda _: session)
        self.assertEqual(result['status'], 'allowed')
        self.assertIsNone(result['principal'])
        self.assertFalse(result['principal_verified'])

    def test_wrong_target_or_non_sheet_response_is_unverified(self):
        for payload in (metadata(target=OTHER), {'id': TARGET, 'mimeType': 'application/pdf', 'capabilities': {'canEdit': True}}):
            result, _ = self.run_case(Response(payload))
            self.assertEqual(result['code'], 'unexpected_target')

    def test_credential_transport_errors_and_bad_api_payloads_never_expose_secrets_or_paths(self):
        secret = '/private/credential.json secret-token-value'
        output = StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = access.diagnose({'TAESAN_SHEET_ID': TARGET}, credentials_loader=Mock(side_effect=RuntimeError(secret)))
            session = Mock()
            session.get.side_effect = TimeoutError(secret)
            other = access.diagnose({'TAESAN_SHEET_ID': TARGET},
                credentials_loader=lambda _: SimpleNamespace(service_account_email=EMAIL), session_factory=lambda _: session)
        self.assertNotIn(secret, json.dumps([result, other]))
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(other['status'], 'unverified')

    def test_cli_default_and_diagnose_only_print_safe_result_and_never_write(self):
        expected = {'status': 'allowed', 'summary': 'fixture result'}
        for argv in ([], ['--diagnose']):
            output = StringIO()
            with patch.object(access, 'diagnose', return_value=expected), contextlib.redirect_stdout(output):
                self.assertEqual(access.main(argv), 0)
            self.assertEqual(json.loads(output.getvalue()), expected)


if __name__ == '__main__':
    unittest.main()
