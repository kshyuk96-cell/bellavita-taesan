"""Read-only diagnostics for the actual configured Taesan ledger identity.

Drive capabilities.canEdit is a file permission observation, not proof a Sheets
write, protected-cell edit or order automation succeeded. Never infer the runtime
principal from ChatGPT's connected account or from a file's list of readers.
The server authenticates the caller separately before and after this function.
"""
import argparse
import json
import os
import re


DRIVE_METADATA_SCOPE = 'https://www.googleapis.com/auth/drive.metadata.readonly'
SHEETS_SCOPE = 'https://www.googleapis.com/auth/spreadsheets'
SHEET_MIME = 'application/vnd.google-apps.spreadsheet'
_ID = re.compile(r'[A-Za-z0-9_-]{20,200}')
_EMAIL = re.compile(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


def configured_ledger(env=None):
    env = os.environ if env is None else env
    value = env.get('TAESAN_ORDER_SHEET_ID') or env.get('TAESAN_SHEET_ID')
    if not value:
        return None
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError('서버에 등록한 원장 파일 ID 형식을 확인해 주세요.')
    return value


def load_server_credentials(env=None, *, scopes=(DRIVE_METADATA_SCOPE,)):
    """Load the same server credential selection; no token/key is returned to UI."""
    env = os.environ if env is None else env
    path = env.get('TAESAN_GOOGLE_CREDENTIALS')
    if path:
        from google.oauth2.service_account import Credentials
        return Credentials.from_service_account_file(path, scopes=list(scopes))
    from google.auth import default
    credentials, _ = default(scopes=list(scopes))
    return credentials


def authorized_session(credentials):
    from google.auth.transport.requests import AuthorizedSession
    return AuthorizedSession(credentials)


def _email(value):
    return value if isinstance(value, str) and len(value) <= 254 and _EMAIL.fullmatch(value) else None


def _principal(credentials):
    try:
        return _email(getattr(credentials, 'service_account_email', None))
    except Exception:
        return None


def _close(resource):
    try:
        close = getattr(resource, 'close', None)
        if callable(close):
            close()
    except Exception:
        pass


def _error_reason(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('error'), dict):
        return set()
    error = payload['error']
    values = list(error.get('errors') or []) + list(error.get('details') or [])
    return {value.get('reason') for value in values if isinstance(value, dict) and isinstance(value.get('reason'), str)}


def _result(status, code, summary, ledger_id=None, principal=None):
    if principal:
        summary += '\n서버 연결 계정: ' + principal
    return {'status': status, 'code': code, 'summary': summary, 'ledger_id': ledger_id,
            'principal': principal, 'principal_verified': bool(principal), 'scope': 'file_permission_only'}


def diagnose(env=None, *, credentials_loader=None, session_factory=None):
    """Observe actual runtime principal + canEdit with read-only metadata calls."""
    env = os.environ if env is None else env
    try:
        ledger_id = configured_ledger(env)
    except ValueError:
        return _result('unverified', 'invalid_target', '서버에 등록한 원장 파일 ID 형식을 확인해 주세요.')
    if ledger_id is None:
        return _result('unverified', 'missing_target', '서버에 원장 파일이 연결되지 않아 편집 권한을 확인하지 못했어요.')
    try:
        credentials = (credentials_loader or load_server_credentials)(env)
    except Exception:
        # Credential loader exceptions can contain private paths, keys or tokens.
        return _result('unverified', 'credentials_unavailable', '태산이 서버의 Google 계정 연결을 확인해야 해요.', ledger_id)
    principal = _principal(credentials)
    session = None
    try:
        session = (session_factory or authorized_session)(credentials)
        if principal is None:
            response = None
            try:
                response = session.get('https://www.googleapis.com/drive/v3/about',
                    params={'fields': 'user(emailAddress)'}, timeout=10, allow_redirects=False)
                if response.status_code == 200:
                    data = response.json()
                    principal = _email(data.get('user', {}).get('emailAddress')) if isinstance(data, dict) else None
            except Exception:
                pass
            finally:
                _close(response)
        response = None
        try:
            response = session.get('https://www.googleapis.com/drive/v3/files/' + ledger_id,
                params={'fields': 'id,mimeType,capabilities(canEdit)', 'supportsAllDrives': 'true'},
                timeout=10, allow_redirects=False)
            status = response.status_code
            try:
                payload = response.json()
            except Exception:
                payload = None
        finally:
            _close(response)
        if status == 403:
            reasons = _error_reason(payload)
            if reasons & {'accessNotConfigured', 'SERVICE_DISABLED', 'API_DISABLED'}:
                return _result('unverified', 'drive_api_disabled', '권한 확인용 Google Drive API가 활성화되지 않아 확인하지 못했어요.', ledger_id, principal)
            if reasons & {'insufficientPermissions', 'ACCESS_TOKEN_SCOPE_INSUFFICIENT'}:
                return _result('unverified', 'metadata_scope_required', '이 연결에는 파일 권한을 조회할 인증 범위가 부족해요. 원장 편집 권한과는 별개예요.', ledger_id, principal)
            if reasons & {'rateLimitExceeded', 'userRateLimitExceeded', 'dailyLimitExceeded',
                          'quotaExceeded', 'RESOURCE_EXHAUSTED', 'RATE_LIMIT_EXCEEDED', 'QUOTA_EXCEEDED'}:
                return _result('unverified', 'metadata_rate_limited', 'Google 조회 한도에 걸려 파일 편집 권한을 판정하지 못했어요. 잠시 뒤 다시 확인해 주세요.', ledger_id, principal)
            return _result('denied', 'permission_denied', '현재 서버 계정으로 원장 파일에 접근할 수 없어요. 해당 원장에 편집자로 공유되어 있는지 확인해 주세요.', ledger_id, principal)
        if status == 404:
            return _result('unverified', 'target_not_found_or_hidden', '설정된 원장이 없거나 이 서버 계정에 보이지 않아요. 파일 ID와 공유 대상을 확인해 주세요.', ledger_id, principal)
        if status == 401:
            return _result('unverified', 'authentication_required', 'Google 인증 상태를 확인하지 못했어요. 파일 편집 권한을 판정하지 않았어요.', ledger_id, principal)
        if status == 429:
            return _result('unverified', 'metadata_rate_limited', 'Google 조회 한도에 걸려 파일 편집 권한을 판정하지 못했어요. 잠시 뒤 다시 확인해 주세요.', ledger_id, principal)
        if status != 200:
            return _result('unverified', 'metadata_unavailable', '파일 권한 응답을 확인하지 못했어요. 잠시 뒤 다시 확인해 주세요.', ledger_id, principal)
        if (not isinstance(payload, dict) or payload.get('id') != ledger_id
                or payload.get('mimeType') != SHEET_MIME):
            return _result('unverified', 'unexpected_target', '설정한 Google 원장과 응답 파일이 일치하는지 확인하지 못했어요.', ledger_id, principal)
        capabilities = payload.get('capabilities')
        can_edit = capabilities.get('canEdit') if isinstance(capabilities, dict) else None
        if can_edit is True:
            return _result('allowed', 'file_editor_confirmed', '이 서버 계정의 원장 파일 편집 권한을 확인했어요. 보호된 범위와 실제 셀 변경 성공은 별도 확인이 필요해요.', ledger_id, principal)
        if can_edit is False:
            return _result('denied', 'file_read_only', '이 서버 계정은 원장 파일을 편집할 수 없어요. 이 계정에 해당 원장의 편집자 권한을 부여해 주세요.', ledger_id, principal)
        return _result('unverified', 'capability_missing', '파일 접근은 확인했지만 편집 가능 여부가 응답에 없어 판정하지 못했어요.', ledger_id, principal)
    except Exception:
        return _result('unverified', 'metadata_connection_failed', 'Google 파일 권한 조회 연결에 실패했어요. 편집 권한을 판정하지 않았어요.', ledger_id, principal)
    finally:
        _close(session)


def main(argv=None):
    parser = argparse.ArgumentParser(description='태산이 서버 계정의 원장 편집 권한을 읽기 전용으로 확인합니다.')
    parser.add_argument('--diagnose', action='store_true', help='파일 권한만 조회하며 셀을 변경하지 않습니다.')
    parser.parse_args(argv)
    result = diagnose()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'allowed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
