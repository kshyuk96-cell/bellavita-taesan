import contextlib
from io import StringIO
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

from connection_diagnostics import ProbeEvidence, diagnose_connections, summarize_connections
from read_resilience import SchemaReadError


class ConnectionDiagnosticsTests(unittest.TestCase):
    def test_configured_but_no_actual_probe_is_unverified_not_healthy(self):
        results = diagnose_connections({}, configured={'sheets': True, 'model': True}, role='admin')
        states = {item['name']: item['status'] for item in results}
        self.assertEqual(states, {'sheets': 'unverified', 'model': 'unverified',
                                  'map': 'missing', 'pc': 'missing'})

    def test_actual_verified_read_and_zero_results_are_distinct_from_missing(self):
        probes = {'sheets': Mock(return_value=ProbeEvidence(True, empty=True)),
                  'model': Mock(return_value=ProbeEvidence(True))}
        results = diagnose_connections(probes, configured={'sheets': True, 'model': True}, role='admin')
        states = {item['name']: item['status'] for item in results}
        self.assertEqual(states['sheets'], 'empty')
        self.assertEqual(states['model'], 'healthy')
        self.assertEqual(states['map'], 'missing')
        probes['sheets'].assert_called_once_with()
        probes['model'].assert_called_once_with()

    def test_missing_config_does_not_run_probe_or_get_reported_as_zero(self):
        probe = Mock(return_value=ProbeEvidence(True, empty=True))
        results = diagnose_connections({'sheets': probe}, configured={'sheets': False}, role='admin')
        self.assertEqual(results[0]['status'], 'missing')
        self.assertNotIn('0건', results[0]['message'])
        probe.assert_not_called()

    def test_truthy_provider_payload_or_false_evidence_does_not_imply_health(self):
        for value in (True, 'ok private API key', {'success': True}, None, [],
                      ProbeEvidence(False), ProbeEvidence(1), ProbeEvidence(True, empty='yes')):
            result = diagnose_connections({'sheets': lambda: value}, configured={'sheets': True}, role='admin')
            self.assertEqual(result[0]['status'], 'unverified')

    def test_staff_writer_and_unknown_roles_cannot_execute_probes(self):
        probe = Mock(return_value=ProbeEvidence(True))
        probes = {name: probe for name in ('sheets', 'model', 'map', 'pc')}
        config = dict.fromkeys(probes, True)
        for role in ('staff', 'writer'):
            results = diagnose_connections(probes, configured=config, role=role)
            self.assertEqual([item['name'] for item in results], ['sheets', 'model', 'map'])
            self.assertTrue(all(item['status'] == 'restricted' for item in results))
        for role in (None, '', 'owner', 'admin\n', {}, True):
            self.assertEqual(diagnose_connections(probes, configured=config, role=role), [])
        probe.assert_not_called()

    def test_failures_are_categorized_by_actual_exception_and_no_secrets_returned(self):
        for error, category in ((HTTPError('https://private.invalid', 403, 'secret', {}, None), 'permission'),
                                (SchemaReadError('/private/path column secret'), 'schema'),
                                (TimeoutError('secret private key'), 'transport')):
            output = StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                results = diagnose_connections({'sheets': Mock(side_effect=error)},
                                               configured={'sheets': True}, role='admin')
            self.assertEqual(results[0]['status'], 'failed')
            self.assertEqual(results[0]['category'], category)
            self.assertNotIn('secret', str(results))
            self.assertNotIn('/private', str(results))
            self.assertEqual(output.getvalue(), '')

    def test_unknown_connection_names_and_config_values_are_not_disclosed(self):
        probe = Mock()
        result = diagnose_connections({'private/env/path': probe},
                                      configured={'private/env/path': 'secret', 'sheets': 'API key secret'},
                                      role='admin')
        self.assertEqual(result[0]['status'], 'missing')
        self.assertNotIn('secret', str(result))
        self.assertNotIn('private/env/path', str(result))
        probe.assert_not_called()

    def test_summary_uses_fixed_labels_and_safe_statuses_only(self):
        text = summarize_connections({'sheets': lambda: ProbeEvidence(True, empty=True)},
                                     configured={'sheets': True}, role='admin')
        self.assertIn('구글 시트 · 읽기는 성공했고 조회 결과는 0건', text)
        self.assertIn('AI · 연결 설정이 없어', text)
        self.assertEqual(summarize_connections({}, configured={}, role=None), '연결 점검 권한이 없어요.')


if __name__ == '__main__':
    unittest.main()
