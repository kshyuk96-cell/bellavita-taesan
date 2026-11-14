"""Explicit approved Apps Script source edits using a server-injected user OAuth session.

Only getContent/updateContent are used. Source verification does NOT prove a
script ran, a deployment/trigger changed, or Google consent granted new scopes.
No credentials are provisioned, scopes expanded, execution/deploy API called, or
HTTP request retried here. Updating HEAD may immediately affect EXISTING simple
or installed triggers/events even without an explicit scripts.run request.
All environment/session/role/approval/test/backup
callbacks are trusted SERVER adapters, never supplied by model/source/user text.

Backups must actually be persisted durably outside Git by the injected callback
before returning its bound receipt. Approval and copy receipts must be checked
against real server records by the supplied verifiers; JSON flags alone confer
no authority. This module never logs source, tokens, keys or provider exceptions.
Apps Script updateContent has no atomic compare-and-swap: per-project process
locks and fresh baseline reads protect our threads; external writers remain a
race and must also coordinate project editing.
"""
import hashlib
import json
import os
import re
import stat
import threading


PROJECT_SCOPE = 'https://www.googleapis.com/auth/script.projects'
_ID = re.compile(r'[A-Za-z0-9_-]{20,200}')
_HASH = re.compile(r'[a-f0-9]{64}')
_REFERENCE = re.compile(r'[A-Za-z0-9_-]{1,128}')
_TYPES = {'SERVER_JS', 'HTML', 'JSON'}
_ACTIVATION_SCOPE = 'project_head_existing_events_may_use_new_source'
_BOUND_FIELDS = {'environment', 'project_id', 'baseline_digest', 'patch_digest', 'after_digest', 'manifest_change', 'activation_scope'}
_PROJECT_LOCKS = {}
_PROJECT_LOCKS_GUARD = threading.Lock()


class SourceAccessError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Apps Script source operation blocked: ' + code)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and not any(ord(c) < 32 for c in value)


def configured_target(environment, env=None):
    env = os.environ if env is None else env
    if environment not in ('copy', 'production'):
        raise SourceAccessError('configured_target_required')
    key = 'TAESAN_SCRIPT_TEST_PROJECT_ID' if environment == 'copy' else 'TAESAN_SCRIPT_PROJECT_ID'
    target = env.get(key)
    if not isinstance(target, str) or not _ID.fullmatch(target):
        raise SourceAccessError('missing_or_invalid_target')
    if env.get('TAESAN_SCRIPT_PROJECT_ID') == env.get('TAESAN_SCRIPT_TEST_PROJECT_ID'):
        raise SourceAccessError('test_and_production_not_isolated')
    return target


def _admin(actor, role_lookup):
    if not _text(actor, 512) or not callable(role_lookup):
        raise PermissionError('Authenticated owner approval is required')
    try:
        role = role_lookup(actor)
    except Exception:
        role = None
    if role != 'admin':
        raise PermissionError('Authenticated owner approval is required')


def require_user_oauth(session):
    """Reject service-account/impersonated/unknown credentials, including for execution.

    This validates source-edit OAuth only. scripts.run additionally requires its
    actual deployment, common standard Cloud project, API enablement and script
    scopes; calling this function does not establish those prerequisites.
    """
    from google.oauth2.credentials import Credentials
    credentials = getattr(session, 'credentials', None)
    if not isinstance(credentials, Credentials):
        raise SourceAccessError('user_oauth_required_service_accounts_unsupported')
    try:
        scopes = credentials.granted_scopes or credentials.scopes or []
    except Exception:
        raise SourceAccessError('user_oauth_scope_unverified') from None
    if not isinstance(scopes, (list, tuple, set, frozenset)) or PROJECT_SCOPE not in scopes:
        raise SourceAccessError('script_projects_scope_required')


def _credential_file(path):
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 65536:
            raise SourceAccessError('unsafe_user_oauth_file')
        raw = os.read(descriptor, 65537)
        if len(raw) > 65536:
            raise SourceAccessError('unsafe_user_oauth_file')
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict) or data.get('type') != 'authorized_user':
            raise SourceAccessError('authorized_user_file_required')
        scopes = data.get('granted_scopes') or data.get('scopes')
        if not isinstance(scopes, list) or PROJECT_SCOPE not in scopes:
            raise SourceAccessError('initial_script_projects_consent_unverified')
        return hashlib.sha256(raw).hexdigest(), (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except SourceAccessError:
        raise
    except Exception:
        raise SourceAccessError('user_oauth_file_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def configured_session(env=None, *, credentials_loader=None, session_factory=None):
    """Load a separately consented Render user-OAuth secret file, never server SA.

    Does not create credentials, run consent, refresh tokens or make API calls.
    The session may refresh on a later authorized request. Existing stored scopes
    must already include script.projects; merely requesting a scope is not consent.
    """
    env = os.environ if env is None else env
    path = env.get('TAESAN_SCRIPT_GOOGLE_CREDENTIALS')
    if not _text(path, 4096):
        raise SourceAccessError('script_user_oauth_file_not_configured')
    before = _credential_file(path)
    session = None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import AuthorizedSession
        credentials = (credentials_loader or Credentials.from_authorized_user_file)(path, scopes=[PROJECT_SCOPE])
        if _credential_file(path) != before:
            raise SourceAccessError('user_oauth_file_changed')
        session = (session_factory or AuthorizedSession)(credentials)
        require_user_oauth(session)
        return session
    except SourceAccessError:
        if session is not None:
            _close(session)
        raise
    except Exception:
        if session is not None:
            _close(session)
        raise SourceAccessError('script_user_oauth_connection_unavailable') from None


def _close(response):
    try:
        response.close()
    except Exception:
        pass


def _files(files):
    if not isinstance(files, list) or not 1 <= len(files) <= 500:
        raise SourceAccessError('invalid_project_files')
    result, keys, size = [], set(), 0
    for item in files:
        if not isinstance(item, dict):
            raise SourceAccessError('invalid_project_files')
        name, kind, source = item.get('name'), item.get('type'), item.get('source')
        if not _text(name, 200) or kind not in _TYPES or not isinstance(source, str):
            raise SourceAccessError('invalid_project_files')
        key = (name, kind)
        if key in keys or len(source) > 5_000_000:
            raise SourceAccessError('ambiguous_or_oversized_project_files')
        keys.add(key)
        size += len(source.encode('utf-8'))
        if size > 20_000_000:
            raise SourceAccessError('oversized_project')
        result.append({'name': name, 'type': kind, 'source': source})
    return result


def source_digest(files):
    """Full executable project content digest, excluding provider output metadata."""
    return _digest(sorted(_files(files), key=lambda item: (item['name'], item['type'])))


def _read(project_id, session):
    response = None
    try:
        response = session.get('https://script.googleapis.com/v1/projects/' + project_id + '/content',
                               timeout=10, allow_redirects=False)
        if response.status_code != 200 or getattr(response, 'history', None):
            raise SourceAccessError('source_read_unavailable')
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('scriptId') != project_id:
            raise SourceAccessError('source_target_unverified')
        return _files(payload.get('files'))
    except SourceAccessError:
        raise
    except Exception:
        raise SourceAccessError('source_read_failed') from None
    finally:
        _close(response)


def read_source(environment, *, env=None, session, actor, role_lookup):
    _admin(actor, role_lookup)
    target = configured_target(environment, env)
    require_user_oauth(session)
    files = _read(target, session)
    _admin(actor, role_lookup)
    return {'environment': environment, 'project_id': target, 'files': files, 'source_digest': source_digest(files)}


def _patch(before, patch):
    patch = _files(patch)
    by_key = {(item['name'], item['type']): item for item in before}
    for item in patch:
        key = (item['name'], item['type'])
        if key not in by_key:
            raise SourceAccessError('only_explicit_existing_file_edits_supported')
        by_key[key] = item
    # Preserve every other source, type, name and the actual existing file order.
    after = [by_key[(item['name'], item['type'])] for item in before]
    changed_manifest = any(item['name'] == 'appsscript' and item['type'] == 'JSON'
                           and item['source'] != next(old['source'] for old in before
                               if (old['name'], old['type']) == (item['name'], item['type'])) for item in patch)
    return patch, after, changed_manifest


def prepare_change(environment, patch, expected_source_digest, *, env=None, session, actor, role_lookup):
    """Read actual baseline and prepare an exact reviewable source-only candidate."""
    snapshot = read_source(environment, env=env, session=session, actor=actor, role_lookup=role_lookup)
    if not isinstance(expected_source_digest, str) or not _HASH.fullmatch(expected_source_digest):
        raise SourceAccessError('exact_expected_source_digest_required')
    if snapshot['source_digest'] != expected_source_digest:
        raise SourceAccessError('baseline_changed')
    patch, after, changed_manifest = _patch(snapshot['files'], patch)
    binding = dict(environment=environment, project_id=snapshot['project_id'], baseline_digest=expected_source_digest,
                   patch_digest=source_digest(patch), after_digest=source_digest(after), manifest_change=changed_manifest,
                   activation_scope=_ACTIVATION_SCOPE)
    return {'binding': binding, 'before': snapshot['files'], 'patch': patch, 'after': after,
            'request_digest': _digest(binding)}


def _verified(callback, *args):
    if not callable(callback):
        return False
    try:
        return callback(*args) is True
    except Exception:
        return False


def _approval(plan, receipt, actor, verifier):
    binding = plan['binding']
    if (not isinstance(receipt, dict) or set(receipt) != _BOUND_FIELDS | {'approved', 'approved_by', 'approval_id'}
            or receipt.get('approved') is not True or receipt.get('approved_by') != actor
            or any(receipt.get(key) != value for key, value in binding.items())
            or type(receipt.get('manifest_change')) is not bool
            or not isinstance(receipt.get('approval_id'), str) or not _REFERENCE.fullmatch(receipt['approval_id'])
            or not _verified(verifier, receipt)):
        raise SourceAccessError('exact_owner_approval_required')


def _candidate(plan, env):
    if not isinstance(plan, dict) or set(plan) != {'binding', 'before', 'patch', 'after', 'request_digest'}:
        raise SourceAccessError('invalid_source_plan')
    binding = plan['binding']
    if not isinstance(binding, dict) or set(binding) != _BOUND_FIELDS:
        raise SourceAccessError('invalid_source_binding')
    if binding['activation_scope'] != _ACTIVATION_SCOPE:
        raise SourceAccessError('source_head_activation_scope_required')
    target = configured_target(binding['environment'], env)
    if target != binding['project_id'] or plan['request_digest'] != _digest(binding):
        raise SourceAccessError('configured_target_or_binding_changed')
    before = _files(plan['before'])
    patch, after, changed_manifest = _patch(before, plan['patch'])
    if (source_digest(before) != binding['baseline_digest'] or source_digest(patch) != binding['patch_digest']
            or source_digest(after) != binding['after_digest'] or after != plan['after']
            or type(binding['manifest_change']) is not bool or changed_manifest != binding['manifest_change']):
        raise SourceAccessError('source_plan_changed')
    return target, before, after


def _copy_test(plan, receipt, env, verifier):
    target = configured_target('copy', env)
    binding = plan['binding']
    required = {'status', 'copy_project_id', 'patch_digest', 'after_digest', 'checks', 'report_digest', 'trigger_effects_reviewed'}
    if (not isinstance(receipt, dict) or set(receipt) != required or receipt.get('status') != 'passed'
            or receipt.get('copy_project_id') != target or target == binding['project_id']
            or receipt.get('patch_digest') != binding['patch_digest'] or receipt.get('after_digest') != binding['after_digest']
            or receipt.get('trigger_effects_reviewed') is not True
            or not isinstance(receipt.get('checks'), list) or not receipt['checks']
            or any(not _text(item, 500) for item in receipt['checks'])
            or not isinstance(receipt.get('report_digest'), str) or not _HASH.fullmatch(receipt['report_digest'])
            or not _verified(verifier, receipt, binding)):
        raise SourceAccessError('actual_bound_copy_test_required')


def _backup(callback, target, before, baseline):
    if not callable(callback):
        raise SourceAccessError('durable_outside_git_backup_required')
    try:
        receipt = callback({'project_id': target, 'source_digest': baseline, 'files': json.loads(_json(before))})
    except Exception:
        raise SourceAccessError('backup_failed') from None
    required = {'project_id', 'source_digest', 'backup_id', 'durable', 'outside_git'}
    if (not isinstance(receipt, dict) or set(receipt) != required or receipt['project_id'] != target
            or receipt['source_digest'] != baseline or receipt['durable'] is not True or receipt['outside_git'] is not True
            or not isinstance(receipt['backup_id'], str) or not _REFERENCE.fullmatch(receipt['backup_id'])):
        raise SourceAccessError('durable_outside_git_backup_unverified')
    return receipt['backup_id']


def _apply_locked(plan, approval, *, env=None, session, actor, role_lookup, verify_approval, backup,
                  copy_receipt=None, verify_copy_receipt=None):
    _admin(actor, role_lookup)
    try:
        plan, approval = json.loads(_json(plan)), json.loads(_json(approval))
    except (ValueError, TypeError, RecursionError):
        raise SourceAccessError('invalid_source_plan_or_approval') from None
    target, before, after = _candidate(plan, env)
    binding = plan['binding']
    require_user_oauth(session)
    _approval(plan, approval, actor, verify_approval)
    if binding['environment'] == 'production':
        _copy_test(plan, copy_receipt, env, verify_copy_receipt)
    if source_digest(_read(target, session)) != binding['baseline_digest']:
        raise SourceAccessError('baseline_changed')
    backup_id = _backup(backup, target, before, binding['baseline_digest'])
    # The backup operation may take time: reread actual source and authority.
    _admin(actor, role_lookup)
    _approval(plan, approval, actor, verify_approval)
    if configured_target(binding['environment'], env) != target:
        raise SourceAccessError('configured_target_changed')
    if source_digest(_read(target, session)) != binding['baseline_digest']:
        raise SourceAccessError('baseline_changed_after_backup')
    _admin(actor, role_lookup)
    _approval(plan, approval, actor, verify_approval)
    if binding['environment'] == 'production':
        _copy_test(plan, copy_receipt, env, verify_copy_receipt)
    _admin(actor, role_lookup)
    response = None
    status = 'uncertain'
    try:
        response = session.put('https://script.googleapis.com/v1/projects/' + target + '/content',
                               json={'files': after}, timeout=15, allow_redirects=False)
        if response.status_code != 200 or getattr(response, 'history', None):
            raise SourceAccessError('update_response_unverified')
        observed = _read(target, session)
        if source_digest(observed) == binding['after_digest']:
            _admin(actor, role_lookup)
            status = 'source_verified'
    except Exception:
        # Timeout/mismatch/error may follow a completed remote write. No retries.
        pass
    finally:
        _close(response)
    return {'status': status, 'project_id': target, 'backup_id': backup_id,
            'expected_source_digest': binding['after_digest'], 'scope': 'source_only',
            'execution_requested': False, 'deployment_requested': False,
            'trigger_configuration_changed': False, 'trigger_effects_verified': False,
            'summary': ('소스 수정 후 전체 내용을 다시 읽어 일치를 확인했어요.' if status == 'source_verified'
                        else '소스 변경 결과가 미확인입니다. 자동 재시도하지 않았어요.')
                       + ' 기존 트리거·이벤트가 새 HEAD 소스를 실행할 수 있어요.'
                       + ' 직접 실행 요청·배포·트리거 설정 변경은 하지 않았고, 운영 실행 영향과 새 OAuth 동의는 별도 확인이 필요해요.'}


def apply_change(plan, approval, *, env=None, session, actor, role_lookup, verify_approval, backup,
                 copy_receipt=None, verify_copy_receipt=None):
    """Serialize own project writers; one PUT max and no automatic retry on uncertainty."""
    _admin(actor, role_lookup)
    try:
        frozen = json.loads(_json(plan))
    except (ValueError, TypeError, RecursionError):
        raise SourceAccessError('invalid_source_plan') from None
    target, _, _ = _candidate(frozen, env)
    with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.setdefault(target, threading.Lock())
    with lock:
        return _apply_locked(frozen, approval, env=env, session=session, actor=actor,
            role_lookup=role_lookup, verify_approval=verify_approval, backup=backup,
            copy_receipt=copy_receipt, verify_copy_receipt=verify_copy_receipt)
