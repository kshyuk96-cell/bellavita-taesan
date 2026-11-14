"""Bounded retries for explicit, side-effect-free reads only.

The trusted server caller must supply a read operation. Never wrap dispatch,
payment, submission, sending, approval, or a read that changes remote state.
This module does not infer retry safety from user/model text or HTTP verbs.
No raw exception message, response body, URL or credential is returned/logged.
"""
from dataclasses import dataclass, field
import errno
import math
import socket
import time
from urllib.error import URLError


class SchemaReadError(ValueError):
    """Server-side validation found changed/missing headers or response schema."""


@dataclass(frozen=True)
class ReadFailure:
    category: str
    message: str
    retryable: bool


@dataclass(frozen=True)
class ReadResult:
    ok: bool
    value: object = field(default=None, repr=False)
    failure: ReadFailure | None = None
    attempts: int = 0


def _status(error):
    try:
        candidates = [getattr(error, 'status_code', None), getattr(error, 'code', None)]
        for name in ('response', 'resp'):
            response = getattr(error, name, None)
            if response is not None:
                candidates.extend((getattr(response, 'status_code', None), getattr(response, 'status', None)))
        return next((value for value in candidates if type(value) is int and 100 <= value <= 599), None)
    except Exception:
        return None


def _transport(error):
    if isinstance(error, (TimeoutError, ConnectionError, socket.gaierror, URLError)):
        return True
    if isinstance(error, OSError) and error.errno in {
        errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET, errno.EHOSTUNREACH,
        errno.ENETUNREACH, errno.ENETDOWN, errno.ENOTCONN, errno.ETIMEDOUT, errno.EPIPE,
    }:
        return True
    try:
        from requests.exceptions import ConnectionError as RequestConnectionError, Timeout
        if isinstance(error, (RequestConnectionError, Timeout)):
            return True
    except ImportError:
        pass
    try:
        from httpx import NetworkError, TimeoutException
        if isinstance(error, (NetworkError, TimeoutException)):
            return True
    except ImportError:
        pass
    try:
        from openai import APIConnectionError
        if isinstance(error, APIConnectionError):
            return True
    except ImportError:
        pass
    try:
        from google.auth.exceptions import TransportError
        if isinstance(error, TransportError):
            return True
    except ImportError:
        pass
    return False


def classify_read_error(error):
    """Classify actual typed exceptions/status codes, never error prose."""
    if isinstance(error, SchemaReadError):
        return ReadFailure('schema', '자료의 열 제목이나 형식이 달라 조회를 멈췄어요. 확인이 필요해요.', False)
    status = _status(error)
    if status in (401, 403) or isinstance(error, PermissionError):
        return ReadFailure('permission', '자료를 읽을 권한이 없어요. 계정 권한을 확인해 주세요.', False)
    if status == 404 or isinstance(error, FileNotFoundError):
        return ReadFailure('not_found', '조회할 자료나 연결 대상을 찾지 못했어요.', False)
    if status == 429:
        return ReadFailure('rate_limit', '조회 요청이 몰려 잠시 제한됐어요. 잠시 뒤 다시 확인해 주세요.', True)
    if status == 408 or (status is not None and 500 <= status <= 599):
        return ReadFailure('service', '연결 서비스가 일시적으로 응답하지 못했어요. 잠시 뒤 다시 확인해 주세요.', True)
    if status is None and _transport(error):
        return ReadFailure('transport', '연결이 끊기거나 조회 시간이 초과됐어요. 잠시 뒤 다시 확인해 주세요.', True)
    return ReadFailure('unknown', '조회 결과를 확인하지 못했어요. 담당자 확인이 필요해요.', False)


def read_with_retry(read, *, max_attempts=2, wait=None, retry_delay=0.1):
    """Run only the explicitly supplied safe read, returning its real result.

    At most three attempts and two waits of at most 0.25 seconds are allowed.
    Provider-side retry loops must be disabled/bounded by the caller separately.
    A successful empty/None return remains a successful call, not a fabricated
    record count. Downstream schema/connection checks still decide its meaning.
    """
    if (not callable(read) or type(max_attempts) is not int or not 1 <= max_attempts <= 3
            or type(retry_delay) not in (int, float) or not math.isfinite(retry_delay)
            or not 0 <= retry_delay <= 0.25 or (wait is not None and not callable(wait))):
        raise ValueError('읽기 재시도 설정을 확인해 주세요.')
    wait = wait or time.sleep
    for attempt in range(1, max_attempts + 1):
        try:
            return ReadResult(ok=True, value=read(), attempts=attempt)
        except Exception as error:
            failure = classify_read_error(error)
        if not failure.retryable or attempt == max_attempts:
            return ReadResult(ok=False, failure=failure, attempts=attempt)
        try:
            wait(retry_delay)
        except Exception:
            return ReadResult(ok=False, failure=ReadFailure(
                'unknown', '조회 재시도를 이어가지 못했어요. 담당자 확인이 필요해요.', False), attempts=attempt)
