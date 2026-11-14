import contextlib
from io import StringIO
import errno
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from requests.exceptions import ConnectionError as RequestConnectionError, Timeout
import httpx
from openai import APIConnectionError, APITimeoutError

from read_resilience import SchemaReadError, classify_read_error, read_with_retry


def http_error(status):
    return HTTPError('https://private.invalid/secret', status, 'PRIVATE TOKEN', {}, None)


class ReadResilienceTests(unittest.TestCase):
    def test_actual_http_codes_are_classified_without_raw_error(self):
        for status, category, retryable in ((401, 'permission', False), (403, 'permission', False),
                                           (404, 'not_found', False), (429, 'rate_limit', True),
                                           (503, 'service', True), (408, 'service', True),
                                           (400, 'unknown', False)):
            with self.subTest(status=status):
                result = classify_read_error(http_error(status))
                self.assertEqual((result.category, result.retryable), (category, retryable))
                self.assertNotIn('PRIVATE', result.message)
                self.assertNotIn('private.invalid', result.message)

    def test_standard_provider_status_locations_and_schema_type(self):
        for attr, value in (('status_code', 403), ('response', SimpleNamespace(status_code=403)),
                            ('resp', SimpleNamespace(status=403))):
            error = RuntimeError('private detail')
            setattr(error, attr, value)
            self.assertEqual(classify_read_error(error).category, 'permission')
        self.assertEqual(classify_read_error(SchemaReadError('private column')).category, 'schema')
        for value in ('403', True, None):
            error = RuntimeError('403 permission timeout')
            error.status_code = value
            self.assertEqual(classify_read_error(error).category, 'unknown')

    def test_network_exceptions_use_types_not_words(self):
        request = httpx.Request('GET', 'https://private.invalid')
        for error in (TimeoutError('private'), ConnectionError('private'), Timeout('private'),
                      RequestConnectionError('private'), URLError('private'),
                      OSError(errno.ECONNRESET, 'private'), httpx.ConnectError('private'),
                      httpx.ReadTimeout('private'), APIConnectionError(request=request),
                      APITimeoutError(request=request)):
            with self.subTest(kind=type(error).__name__):
                self.assertEqual(classify_read_error(error).category, 'transport')
        self.assertEqual(classify_read_error(RuntimeError('TimeoutError connection failed')).category, 'unknown')
        self.assertEqual(classify_read_error(PermissionError('/private/path')).category, 'permission')
        self.assertEqual(classify_read_error(FileNotFoundError('/private/path')).category, 'not_found')

    def test_transient_safe_read_retries_and_returns_actual_value(self):
        read = Mock(side_effect=[TimeoutError('private'), {'count': 3}])
        wait = Mock()
        result = read_with_retry(read, wait=wait)
        self.assertTrue(result.ok)
        self.assertEqual(result.value, {'count': 3})
        self.assertEqual(result.attempts, 2)
        self.assertIsNone(result.failure)
        self.assertEqual(read.call_count, 2)
        wait.assert_called_once_with(0.1)

    def test_permission_schema_not_found_unknown_never_retried(self):
        for error in (http_error(403), http_error(404), SchemaReadError('changed'), RuntimeError('private')):
            with self.subTest(kind=type(error).__name__):
                read, wait = Mock(side_effect=error), Mock()
                result = read_with_retry(read, max_attempts=3, wait=wait)
                self.assertFalse(result.ok)
                self.assertEqual(result.attempts, 1)
                read.assert_called_once_with()
                wait.assert_not_called()

    def test_rate_limit_bounded_and_never_returns_zero_as_failed_read(self):
        read, wait = Mock(side_effect=http_error(429)), Mock()
        result = read_with_retry(read, max_attempts=3, wait=wait, retry_delay=0.25)
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.failure.category, 'rate_limit')
        self.assertEqual(result.attempts, 3)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(wait.call_count, 2)

    def test_empty_result_and_none_preserved_without_invented_count(self):
        for value in ([], None, 0, {}):
            result = read_with_retry(lambda: value)
            self.assertTrue(result.ok)
            self.assertIs(result.value, value)
            self.assertEqual(result.attempts, 1)

    def test_errors_are_not_logged_or_echoed_and_values_hidden_from_repr(self):
        output = StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = read_with_retry(Mock(side_effect=RuntimeError('PRIVATE secret URL')))
        self.assertNotIn('PRIVATE', repr(result))
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('sensitive result', repr(read_with_retry(lambda: 'sensitive result')))

    def test_invalid_retry_settings_rejected_before_read(self):
        read = Mock()
        for settings in ({'max_attempts': 0}, {'max_attempts': 4}, {'max_attempts': True},
                         {'retry_delay': 1}, {'retry_delay': -1}, {'retry_delay': float('nan')},
                         {'retry_delay': float('inf')}, {'wait': 'not callable'}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                read_with_retry(read, **settings)
        read.assert_not_called()

    def test_wait_failure_does_not_start_another_read_or_expose_error(self):
        read = Mock(side_effect=TimeoutError('private read'))
        result = read_with_retry(read, wait=Mock(side_effect=RuntimeError('private wait')))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, 'unknown')
        self.assertEqual(result.attempts, 1)
        self.assertNotIn('private', result.failure.message)
        read.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
