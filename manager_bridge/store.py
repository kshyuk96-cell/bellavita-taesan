"""Durable project-scoped delegation records; no code execution or deployment.

add_token is a trusted LOCAL bootstrap API, never an unauthenticated HTTP action.
All operational methods authenticate the raw bearer credential inside the same
SQLite write transaction as their transition. Only hashes are persisted. Lease
expiry/revocation means needs_verification, not proof that a worker stopped and
never permission to replay its work automatically.

Worker reports are claims. review records a trusted manager's evidence reference;
this module does not run an external verifier or prove a deployment occurred.
"""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class PermissionDenied(PermissionError):
    pass


class Conflict(ValueError):
    pass


class NotFound(LookupError):
    pass


_PROJECT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
_REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
_ID = re.compile(r'[a-f0-9]{32}')
_SHA = re.compile(r'[a-f0-9]{40}')
_DIGEST = re.compile(r'[a-f0-9]{64}')
_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_KINDS = frozenset({'development', 'test'})
_ROLES = frozenset({'owner', 'manager', 'worker'})
_STATES = frozenset({'pending_approval', 'queued', 'running', 'reported', 'reviewed',
                     'cancelled', 'needs_verification'})


def _hierarchy_enabled(c, project):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hierarchy_members'").fetchone() and
                c.execute('SELECT 1 FROM hierarchy_members WHERE project=?',(project,)).fetchone())


def _hierarchy_claim_allowed(c, actor, task):
    """Legacy transports cannot bypass managed assignment or required capacity."""
    if not _hierarchy_enabled(c,task['project']):return True
    row=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone()
    if row is None or row['state']!='development_queued' or row['version']!=task['version'] or row['manifest_digest']!=task['manifest_digest']:return False
    config=c.execute('SELECT fingerprint FROM maintenance_projects WHERE project=?',(task['project'],)).fetchone()
    if config is None or config['fingerprint']!=row['config_fingerprint']:return False
    member=c.execute('SELECT * FROM hierarchy_members WHERE project=? AND member_id=?',(task['project'],row['developer_id'])).fetchone()
    if member is None or member['role']!='developer' or member['credential_hash']!=actor['token_hash'] or member['subject']!=actor['subject'] or member['config_fingerprint']!=config['fingerprint']:return False
    for scope in ('*',task['project']):
        if not c.execute('SELECT 1 FROM bridge_capacity_limits WHERE scope=?',(scope,)).fetchone():return False
        if not c.execute('SELECT 1 FROM hierarchy_review_limits WHERE scope=?',(scope,)).fetchone():return False
    return True


def _hierarchy_acceptance(c,task):
    """Only exact technical acceptance releases a managed dependency."""
    if not _hierarchy_enabled(c,task['project']):return True
    row=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone()
    if row is None or row['state']!='accepted' or row['version']!=task['version'] or row['manifest_digest']!=task['manifest_digest']:return False
    config=c.execute('SELECT fingerprint FROM maintenance_projects WHERE project=?',(task['project'],)).fetchone()
    if config is None or row['config_fingerprint']!=config['fingerprint']:return False
    if not row['candidate'] or not row['evidence'] or not task['review']:return False
    candidate=json.loads(row['candidate']);evidence=json.loads(row['evidence']);review=json.loads(task['review'])
    assessment=evidence.get('code_assessment')
    if json.loads(task['manifest'])['kind']=='development' and not (type(assessment) is dict and assessment.get('recommendation')=='pass' and assessment.get('candidate_digest')==candidate.get('digest') and assessment.get('reviewer_subject')==evidence.get('reviewer_subject')):return False
    return bool(evidence.get('isolation_verified') is True and evidence.get('candidate_digest')==candidate.get('digest') and
                review.get('scope')=='three_tier_technical_only' and review.get('candidate_digest')==candidate.get('digest') and
                review.get('evidence_digest')==digest(evidence) and review.get('report_digest')==digest(json.loads(task['report'])) and
                evidence.get('checks') and all(type(x.get('exit_code')) is int and x['exit_code']==0 for x in evidence['checks']))


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def token_hash(token):
    if not isinstance(token, str) or not 32 <= len(token) <= 1024 or not token.isascii() or any(c.isspace() for c in token):
        raise PermissionDenied('관리 연결 인증을 확인해 주세요.')
    return hashlib.sha256(token.encode()).hexdigest()


def _text(value, limit, label):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError(label + ' 형식을 확인해 주세요.')
    return value


def _project(value):
    if not isinstance(value, str) or not _PROJECT.fullmatch(value):
        raise ValueError('프로젝트 식별값을 확인해 주세요.')
    return value


def _task_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError('작업 식별값을 확인해 주세요.')
    return value


def _lease_seconds(value):
    if type(value) is not int or not 1 <= value <= 3600:
        raise ValueError('작업 임대 시간을 확인해 주세요.')
    return value


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError('전체 커밋 식별값을 확인해 주세요.')
    return value


def _manifest(value):
    required = {'project', 'repository', 'base_sha', 'description', 'checks', 'kind'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('개발 요청의 프로젝트·저장소·기준 커밋·내용·검증·종류가 필요해요.')
    _project(value['project'])
    if not isinstance(value['repository'], str) or not _REPOSITORY.fullmatch(value['repository']):
        raise ValueError('저장소를 소유자/이름 형식으로 지정해 주세요.')
    _sha(value['base_sha'])
    _text(value['description'], 8000, '개발 내용')
    if not isinstance(value['kind'], str) or value['kind'] not in _KINDS:
        raise ValueError('개발 또는 검증 작업만 접수할 수 있어요.')
    checks = value['checks']
    if not isinstance(checks, list) or not 1 <= len(checks) <= 30:
        raise ValueError('검증 항목을 1~30개 지정해 주세요.')
    for check in checks:
        _text(check, 300, '검증 항목')
    if len(canonical_json(value).encode()) > 20000:
        raise ValueError('개발 요청을 나눠 접수해 주세요.')
    return json.loads(canonical_json(value))


def _report(value):
    if not isinstance(value, dict) or set(value) != {'head_sha', 'summary', 'checks'}:
        raise ValueError('작업 결과의 커밋·요약·검증 주장이 필요해요.')
    _sha(value['head_sha'])
    _text(value['summary'], 4000, '결과 요약')
    if not isinstance(value['checks'], list) or not 1 <= len(value['checks']) <= 100:
        raise ValueError('확인한 검증 항목이 필요해요.')
    for check in value['checks']:
        if not isinstance(check, dict) or set(check) != {'name', 'passed'} or type(check['passed']) is not bool:
            raise ValueError('검증 항목의 이름과 실제 불리언 결과가 필요해요.')
        _text(check['name'], 200, '검증 항목 이름')
    if len(canonical_json(value).encode()) > 30000:
        raise ValueError('작업 결과 요약이 너무 커요.')
    return json.loads(canonical_json(value))


def _reject_credentials(value, *credentials):
    serialized = canonical_json(value)
    if any(credential and credential in serialized for credential in credentials):
        raise ValueError('인증 정보를 작업 내용이나 결과에 포함하지 마세요.')


class Store:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path).resolve()
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._transaction() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS bridge_projects (
                    project TEXT PRIMARY KEY,repository TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS bridge_tokens (
                    token_hash TEXT PRIMARY KEY,subject TEXT NOT NULL,role TEXT NOT NULL,
                    projects TEXT NOT NULL,capabilities TEXT NOT NULL,expires REAL,revoked REAL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS bridge_tasks (
                    id TEXT PRIMARY KEY,project TEXT NOT NULL,request_key TEXT NOT NULL,
                    manifest TEXT NOT NULL,manifest_digest TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,
                    approval TEXT,worker_subject TEXT,worker_token_hash TEXT,lease_hash TEXT,lease_expires REAL,
                    progress TEXT,report TEXT,review TEXT,cancel_reason TEXT,created REAL NOT NULL,updated REAL NOT NULL,
                    UNIQUE(project,request_key));
                CREATE INDEX IF NOT EXISTS bridge_claim_index ON bridge_tasks(project,state,created,id);
                CREATE TABLE IF NOT EXISTS bridge_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,task TEXT NOT NULL,actor TEXT NOT NULL,
                    action TEXT NOT NULL,state TEXT NOT NULL,recorded REAL NOT NULL,payload_digest TEXT);
            ''')
            from .coordination import Coordination
            Coordination.create_tables(c)

    @contextmanager
    def _transaction(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            os.chmod(self.path, 0o600)
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('서버 시각을 확인해 주세요.')
        return float(value)

    def add_token(self, subject, role, projects, *, token=None, capabilities=('development', 'test'), expires_at=None):
        """Trusted local bootstrap only. Raw credential is returned once, never saved."""
        _text(subject, 100, '연결 주체')
        if not isinstance(role, str) or role not in _ROLES:
            raise ValueError('관리 연결 역할을 확인해 주세요.')
        if not isinstance(projects, (list, tuple)) or not 1 <= len(projects) <= 1000:
            raise ValueError('명시한 프로젝트 권한 범위가 필요해요.')
        if any(not isinstance(project, str) for project in projects):
            raise ValueError('프로젝트 권한 범위를 확인해 주세요.')
        scopes = sorted(set(projects))
        if '*' in scopes and (role != 'owner' or scopes != ['*']):
            raise ValueError('전체 프로젝트 범위는 소유자 전용이에요.')
        for project in scopes:
            if project != '*':
                _project(project)
        if (not isinstance(capabilities, (list, tuple)) or not capabilities
                or any(not isinstance(kind, str) or kind not in _KINDS for kind in capabilities)):
            raise ValueError('개발·검증 작업 권한을 확인해 주세요.')
        now = self._now()
        if expires_at is not None and (type(expires_at) not in (int, float) or not math.isfinite(expires_at) or expires_at <= now):
            raise ValueError('연결 만료 시각을 확인해 주세요.')
        token = secrets.token_urlsafe(32) if token is None else token
        hashed = token_hash(token)
        with self._transaction() as c:
            if role != 'owner' and any(c.execute('SELECT 1 FROM bridge_projects WHERE project=?', (project,)).fetchone() is None for project in scopes):
                raise ValueError('등록된 프로젝트에만 관리자·작업자를 연결할 수 있어요.')
            if c.execute('SELECT 1 FROM bridge_tokens WHERE token_hash=?', (hashed,)).fetchone():
                raise Conflict('이미 발급된 인증 정보를 덮어쓰지 않았어요.')
            c.execute('INSERT INTO bridge_tokens VALUES (?,?,?,?,?,?,NULL,?)',
                      (hashed, subject, role, canonical_json(scopes), canonical_json(sorted(set(capabilities))), expires_at, now))
        return token

    def _authenticate(self, c, raw):
        hashed = token_hash(raw)
        row = c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?', (hashed,)).fetchone()
        now = self._now()
        if not row or row['revoked'] is not None or (row['expires'] is not None and row['expires'] <= now):
            raise PermissionDenied('관리 연결 인증이 없거나 만료·해제됐어요.')
        web=c.execute('SELECT parent_hash FROM bridge_web_sessions WHERE token_hash=?',(hashed,)).fetchone()
        if web:
            parent=c.execute('SELECT revoked,expires FROM bridge_tokens WHERE token_hash=?',(web['parent_hash'],)).fetchone()
            if not parent or parent['revoked'] is not None or (parent['expires'] is not None and parent['expires']<=now):
                raise PermissionDenied('원래 관리 연결이 만료·해제됐어요.')
        return {'subject': row['subject'], 'role': row['role'], 'projects': json.loads(row['projects']),
                'capabilities': json.loads(row['capabilities']), 'token_hash': hashed}

    def authenticate(self, raw):
        with self._transaction() as c:
            return self._authenticate(c, raw)

    @staticmethod
    def _scope(identity, project):
        return project in identity['projects'] or (identity['role'] == 'owner' and identity['projects'] == ['*'])

    @staticmethod
    def _role(identity, allowed):
        if identity['role'] not in allowed:
            raise PermissionDenied('이 역할에서는 해당 작업을 처리할 수 없어요.')

    def _project_access(self, c, identity, project):
        _project(project)
        if not self._scope(identity, project):
            raise NotFound('이 연결의 프로젝트를 찾지 못했어요.')
        row = c.execute('SELECT repository FROM bridge_projects WHERE project=?', (project,)).fetchone()
        if not row:
            raise NotFound('등록된 프로젝트를 찾지 못했어요.')
        return row['repository']

    def register_project(self, token, project, repository):
        _project(project)
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise ValueError('저장소를 소유자/이름 형식으로 지정해 주세요.')
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            self._role(actor, ('owner',))
            if not self._scope(actor, project):
                raise PermissionDenied('이 소유자 연결의 프로젝트 범위를 벗어났어요.')
            old = c.execute('SELECT repository FROM bridge_projects WHERE project=?', (project,)).fetchone()
            if old:
                if old['repository'] != repository:
                    raise Conflict('등록된 프로젝트의 저장소를 조용히 바꾸지 않았어요.')
                return {'project': project, 'repository': repository}
            c.execute('INSERT INTO bridge_projects VALUES (?,?,?)', (project, repository, self._now()))
            return {'project': project, 'repository': repository}

    def _event(self, c, task, actor, action, state, payload=None):
        c.execute('INSERT INTO bridge_events(task,actor,action,state,recorded,payload_digest) VALUES (?,?,?,?,?,?)',
                  (task, actor, action, state, self._now(), digest(payload) if payload is not None else None))

    def _expire(self, c, project):
        now = self._now()
        rows = c.execute('''SELECT t.id FROM bridge_tasks t LEFT JOIN bridge_tokens a ON a.token_hash=t.worker_token_hash
            WHERE t.project=? AND t.state='running' AND (t.lease_expires<=? OR a.token_hash IS NULL
                OR a.revoked IS NOT NULL OR (a.expires IS NOT NULL AND a.expires<=?))''', (project, now, now)).fetchall()
        for row in rows:
            c.execute("UPDATE bridge_tasks SET state='needs_verification',updated=? WHERE id=? AND state='running'", (now, row['id']))
            self._event(c, row['id'], 'server', 'lease_or_credential_expired', 'needs_verification')

    def _get(self, c, identity, task_id):
        _task_id(task_id)
        row = c.execute('SELECT * FROM bridge_tasks WHERE id=?', (task_id,)).fetchone()
        if not row or not self._scope(identity, row['project']):
            raise NotFound('이 연결의 작업을 찾지 못했어요.')
        if identity['role'] == 'worker' and json.loads(row['manifest'])['kind'] not in identity['capabilities']:
            raise NotFound('이 연결의 작업을 찾지 못했어요.')
        return row

    @staticmethod
    def _public(row):
        return {'id': row['id'], 'project': row['project'], 'request_key': row['request_key'],
                'version': row['version'], 'state': row['state'], 'manifest': json.loads(row['manifest']),
                'manifest_digest': row['manifest_digest'],
                'approval': json.loads(row['approval']) if row['approval'] else None,
                'worker': row['worker_subject'], 'lease_expires_at': row['lease_expires'],
                'progress': row['progress'], 'report': json.loads(row['report']) if row['report'] else None,
                'review': json.loads(row['review']) if row['review'] else None,
                'cancel_reason': row['cancel_reason'], 'created_at': row['created'], 'updated_at': row['updated']}

    def get_task(self, token, task_id):
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            row = self._get(c, actor, task_id)
            self._expire(c, row['project'])
            return self._public(self._get(c, actor, task_id))

    def list_tasks(self, token, project, *, limit=100, state=None):
        if type(limit) is not int or not 1 <= limit <= 200 or (state is not None and state not in _STATES):
            raise ValueError('작업 목록 범위를 확인해 주세요.')
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            self._project_access(c, actor, project)
            self._expire(c, project)
            rows = c.execute('SELECT * FROM bridge_tasks WHERE project=? ORDER BY created,id', (project,)).fetchall()
            result = []
            for row in rows:
                if state is not None and row['state'] != state:
                    continue
                if actor['role'] == 'worker' and json.loads(row['manifest'])['kind'] not in actor['capabilities']:
                    continue
                result.append(self._public(row))
                if len(result) >= limit:
                    break
            return result

    def overview(self, token, project):
        """All project counts, bounded action IDs, no prompts/PII/credentials."""
        from .supervisor import recovery_action
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            self._role(actor, ('owner', 'manager'))
            self._project_access(c, actor, project)
            self._expire(c, project)
            rows = c.execute('SELECT * FROM bridge_tasks WHERE project=? ORDER BY updated DESC,id', (project,)).fetchall()
            counts = {state: 0 for state in sorted(_STATES)}
            actions, action_count = [], 0
            for row in rows:
                counts[row['state']] += 1
                if row['state'] == 'reviewed':
                    continue
                if row['state'] == 'cancelled' and (row['worker_subject'] is None or c.execute(
                        'SELECT 1 FROM bridge_stop_confirmations WHERE task=?', (row['id'],)).fetchone()):
                    continue
                action_count += 1
                failed = bool(row['report'] and any(item['passed'] is False for item in json.loads(row['report'])['checks']))
                if len(actions) < 100:
                    actions.append({'task_id': row['id'], 'state': row['state'],
                                    'action': recovery_action({'state': row['state']}),
                                    'failed_or_unverified_checks': failed})
            return {'counts': counts, 'actions': actions,
                    'actions_truncated': action_count > 100,
                    'operational_deployment_verified': False}

    def submit(self, token, request_key, manifest):
        manifest = _manifest(manifest)
        if not isinstance(request_key, str) or not _KEY.fullmatch(request_key):
            raise ValueError('프로젝트별 접수 대조 키를 지정해 주세요.')
        _reject_credentials(manifest, token)
        fingerprint = digest(manifest)
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            self._role(actor, ('owner', 'manager'))
            repository = self._project_access(c, actor, manifest['project'])
            if repository != manifest['repository']:
                raise Conflict('프로젝트에 등록된 저장소와 요청 저장소가 달라요.')
            old = c.execute('SELECT * FROM bridge_tasks WHERE project=? AND request_key=?', (manifest['project'], request_key)).fetchone()
            if old:
                if old['manifest_digest'] != fingerprint:
                    raise Conflict('같은 접수 키의 개발 범위를 바꾸지 않았어요.')
                return self._public(old)
            task_id, now = secrets.token_hex(16), self._now()
            c.execute('''INSERT INTO bridge_tasks
                (id,project,request_key,manifest,manifest_digest,version,state,created,updated)
                VALUES (?,?,?,?,?,1,'pending_approval',?,?)''',
                (task_id, manifest['project'], request_key, canonical_json(manifest), fingerprint, now, now))
            self._event(c, task_id, actor['subject'], 'submitted', 'pending_approval', manifest)
            return self._public(self._get(c, actor, task_id))

    def approve(self, token, task_id, version, manifest_digest, *, policy_digest=None):
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            self._role(actor, ('owner',))
            row = self._get(c, actor, task_id)
            if type(version) is not int or version != row['version'] or manifest_digest != row['manifest_digest']:
                raise Conflict('승인할 요청의 버전과 내용이 현재 검토안과 달라요.')
            policy_row = c.execute('SELECT policy_digest FROM bridge_task_policy WHERE task=?', (task_id,)).fetchone()
            expected_policy = policy_row['policy_digest'] if policy_row else None
            if policy_digest != expected_policy:
                raise Conflict('파일 범위·선행 작업 정책의 다이제스트까지 대조해야 승인할 수 있어요.')
            if row['state'] in ('queued', 'running', 'reported', 'reviewed') and row['approval']:
                return self._public(row)
            if row['state'] != 'pending_approval':
                raise Conflict('이 상태의 작업은 다시 개발 승인하지 않아요.')
            approval = {'actor': actor['subject'], 'version': version, 'manifest_digest': manifest_digest,
                        'approved_at': self._now()}
            if expected_policy is not None:
                approval['policy_digest'] = expected_policy
            c.execute("UPDATE bridge_tasks SET state='queued',approval=?,updated=? WHERE id=? AND state='pending_approval'",
                      (canonical_json(approval), self._now(), task_id))
            self._event(c, task_id, actor['subject'], 'approved', 'queued', approval)
            return self._public(self._get(c, actor, task_id))

    @staticmethod
    def _reserved_repositories(c):
        rows = c.execute("SELECT manifest FROM bridge_tasks t WHERE "
            "state IN ('running','reported','needs_verification') OR "
            "(state='cancelled' AND worker_subject IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM bridge_stop_confirmations s WHERE s.task=t.id))").fetchall()
        repositories = {json.loads(row['manifest'])['repository'] for row in rows}
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_check_reservations'").fetchone():
            repositories.update(row['repository'] for row in c.execute('SELECT repository FROM maintenance_check_reservations'))
        return repositories

    def claim(self, token, project, *, lease_seconds=300, task_id=None):
        _lease_seconds(lease_seconds)
        if task_id is not None:
            _task_id(task_id)
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            self._role(actor, ('worker',))
            self._project_access(c, actor, project)
            self._expire(c, project)
            busy = c.execute("SELECT 1 FROM bridge_tasks t WHERE worker_token_hash=? AND "
                "(state IN ('running','needs_verification') OR (state='cancelled' AND NOT EXISTS "
                "(SELECT 1 FROM bridge_stop_confirmations s WHERE s.task=t.id)))", (actor['token_hash'],)).fetchone()
            if busy:
                return None
            rows = c.execute("SELECT t.* FROM bridge_tasks t LEFT JOIN bridge_task_planning p ON p.task=t.id "
                "WHERE t.project=? AND t.state='queued' " + ("AND t.id=? " if task_id is not None else "") +
                "ORDER BY COALESCE(p.priority,0) DESC,COALESCE(p.due,1e30),t.created,t.id",
                (project,task_id) if task_id is not None else (project,)).fetchall()
            from .operations import Operations
            if Operations.capacity_blocked(c,project):
                return None
            reserved_repositories = self._reserved_repositories(c)
            for row in rows:
                if not _hierarchy_claim_allowed(c,actor,row):continue
                manifest = json.loads(row['manifest'])
                if manifest['kind'] not in actor['capabilities']:
                    continue
                approval = json.loads(row['approval']) if row['approval'] else {}
                if approval.get('version') != row['version'] or approval.get('manifest_digest') != row['manifest_digest']:
                    c.execute("UPDATE bridge_tasks SET state='needs_verification',updated=? WHERE id=?", (self._now(), row['id']))
                    self._event(c, row['id'], 'server', 'approval_binding_invalid', 'needs_verification')
                    continue
                try:
                    self._validate_execution(c,row,check_source_age=True)
                except (Conflict, PermissionDenied):
                    continue
                policy_row = c.execute('SELECT * FROM bridge_task_policy WHERE task=?', (row['id'],)).fetchone()
                if policy_row:
                    from .coordination import Coordination
                    policy = json.loads(policy_row['policy'])
                    if (approval.get('policy_digest') != policy_row['policy_digest']
                            or not Coordination.dependencies_ready(c, policy)):
                        continue
                    route = c.execute('SELECT worker_hash FROM bridge_task_route WHERE task=?', (row['id'],)).fetchone()
                    health = c.execute('SELECT ready,expires FROM bridge_worker_health WHERE worker_hash=?',
                                       (actor['token_hash'],)).fetchone()
                    if (not route or route['worker_hash'] != actor['token_hash'] or not health
                            or not health['ready'] or health['expires'] <= self._now()):
                        continue
                # Until independent review, reserve the whole repository. Unknown file
                # scopes must not be inferred from natural language. This is global
                # across projects which happen to register the same repository.
                if manifest['repository'] in reserved_repositories:
                    continue
                lease, expires = secrets.token_hex(32), self._now() + lease_seconds
                claimed = c.execute('''UPDATE bridge_tasks SET state='running',worker_subject=?,worker_token_hash=?,
                    lease_hash=?,lease_expires=?,updated=? WHERE id=? AND state='queued' ''',
                    (actor['subject'], actor['token_hash'], hashlib.sha256(lease.encode()).hexdigest(), expires, self._now(), row['id']))
                if not claimed.rowcount:
                    continue
                if _hierarchy_enabled(c,project):
                    c.execute("UPDATE hierarchy_tasks SET state='developing',updated=? WHERE task_id=? AND state='development_queued'",(self._now(),row['id']))
                    c.execute('INSERT INTO hierarchy_events(task_id,kind,state,recorded,payload_digest) VALUES (?,?,?,?,NULL)',(row['id'],'development_claimed','developing',self._now()))
                self._event(c, row['id'], actor['subject'], 'claimed', 'running')
                assignment = {**self._public(self._get(c, actor, row['id'])), 'lease_token': lease}
                if policy_row:
                    assignment['execution_policy'] = policy
                return assignment
            return None

    def _validate_execution(self,c,row,*,check_source_age=False):
        if c is not None and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='governance_restore_marker'").fetchone():
            if c.execute('SELECT 1 FROM governance_restore_marker WHERE reconciliation_required=1 LIMIT 1').fetchone():
                raise Conflict('restored state requires explicit verification before execution')
        approval=json.loads(row['approval']) if row['approval'] else {}
        if json.loads(row['manifest'])['kind']=='development':
            if c is None:raise Conflict('development execution requires a transactional fence')
            enrolled=(c.execute("SELECT 1 FROM sqlite_master WHERE name='governance_enrolled_projects'").fetchone()
                      and c.execute('SELECT 1 FROM governance_enrolled_projects WHERE project=?',(row['project'],)).fetchone())
            if enrolled and not approval.get('proposal_batch_id'):
                raise Conflict('enrolled development requires owner-approved ProposalBoard origin')
        if approval.get('proposal_batch_id') is not None:
            from .proposal_board import validate_execution
            validate_execution(c,self,row)
        if approval.get('delegation_id') is not None:
            if c is None:raise Conflict('delegated execution requires a transactional fence')
            from .delegation import validate_execution
            validate_execution(c,self,row,check_source_age=check_source_age)

    def _lease(self, row, actor, lease, *, c=None):
        if (not isinstance(lease, str) or not _DIGEST.fullmatch(lease)
                or row['worker_token_hash'] != actor['token_hash'] or row['worker_subject'] != actor['subject']
                or not row['lease_hash'] or not hmac.compare_digest(row['lease_hash'], hashlib.sha256(lease.encode()).hexdigest())):
            raise Conflict('현재 작업자의 임대 식별값과 일치하지 않아요.')
        self._validate_execution(c,row,check_source_age=False)

    def heartbeat(self, token, task_id, lease_token, *, lease_seconds=300, progress=None):
        _lease_seconds(lease_seconds)
        if progress is not None:
            _text(progress, 1000, '진행 내용')
            _reject_credentials(progress, token, lease_token)
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            self._role(actor, ('worker',))
            row = self._get(c, actor, task_id)
            self._lease(row, actor, lease_token, c=c)
            self._expire(c, row['project'])
            row = self._get(c, actor, task_id)
            if row['state'] != 'running':
                return {'id': task_id, 'state': row['state'], 'lease_expires_at': row['lease_expires'], 'cancel_requested': True}
            expires = self._now() + lease_seconds
            c.execute('UPDATE bridge_tasks SET lease_expires=?,progress=COALESCE(?,progress),updated=? WHERE id=? AND state=\'running\'',
                      (expires, progress, self._now(), task_id))
            return {'id': task_id, 'state': 'running', 'lease_expires_at': expires, 'cancel_requested': False}

    def report(self, token, task_id, lease_token, report):
        report = _report(report)
        _reject_credentials(report, token, lease_token)
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            self._role(actor, ('worker',))
            row = self._get(c, actor, task_id)
            self._lease(row, actor, lease_token, c=c)
            if row['state'] != 'running' or row['lease_expires'] <= self._now():
                raise Conflict('작업이 취소·만료되었거나 이미 보고돼 결과를 확정하지 않았어요.')
            c.execute("UPDATE bridge_tasks SET state='reported',report=?,updated=? WHERE id=? AND state='running'",
                      (canonical_json(report), self._now(), task_id))
            self._event(c, task_id, actor['subject'], 'worker_reported', 'reported', report)
            return self._public(self._get(c, actor, task_id))

    def review(self, token, task_id, version, manifest_digest, evidence_reference):
        _text(evidence_reference, 2000, '관리자 검증 근거')
        _reject_credentials(evidence_reference, token)
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            self._role(actor, ('owner', 'manager'))
            row = self._get(c, actor, task_id)
            if _hierarchy_enabled(c,row['project']):raise Conflict('managed tasks require independent three-tier technical acceptance')
            if type(version) is not int or version != row['version'] or manifest_digest != row['manifest_digest'] or row['state'] != 'reported':
                raise Conflict('보고된 정확한 검토안에만 관리자 검토를 기록할 수 있어요.')
            review = {'actor': actor['subject'], 'version': version, 'manifest_digest': manifest_digest,
                      'report_digest': digest(json.loads(row['report'])), 'evidence_reference': evidence_reference,
                      'reviewed_at': self._now(), 'scope': 'manager_record_only'}
            c.execute("UPDATE bridge_tasks SET state='reviewed',review=?,updated=? WHERE id=? AND state='reported'",
                      (canonical_json(review), self._now(), task_id))
            self._event(c, task_id, actor['subject'], 'manager_reviewed', 'reviewed', review)
            return self._public(self._get(c, actor, task_id))

    def cancel(self, token, task_id, *, reason='관리자 취소 요청'):
        _text(reason, 500, '취소 사유')
        _reject_credentials(reason, token)
        with self._transaction() as c:
            actor = self._authenticate(c, token)
            self._role(actor, ('owner', 'manager'))
            row = self._get(c, actor, task_id)
            if row['state'] == 'cancelled':
                return self._public(row)
            if row['state'] not in ('pending_approval', 'queued', 'running', 'reported', 'needs_verification'):
                raise Conflict('이미 검토된 작업을 되돌렸다고 기록하지 않았어요.')
            c.execute("UPDATE bridge_tasks SET state='cancelled',cancel_reason=?,updated=? WHERE id=?",
                      (reason, self._now(), task_id))
            self._event(c, task_id, actor['subject'], 'cancel_requested', 'cancelled')
            return self._public(self._get(c, actor, task_id))

    def revoke_token(self, owner_token, target_hash):
        if not isinstance(target_hash, str) or not _DIGEST.fullmatch(target_hash):
            raise ValueError('해제할 관리 연결의 식별값을 확인해 주세요.')
        with self._transaction() as c:
            owner = self._authenticate(c, owner_token)
            self._role(owner, ('owner',))
            target = c.execute('SELECT projects,revoked FROM bridge_tokens WHERE token_hash=?', (target_hash,)).fetchone()
            if not target:
                raise NotFound('해제할 관리 연결을 찾지 못했어요.')
            scopes = json.loads(target['projects'])
            if owner['projects'] != ['*'] and ('*' in scopes or not set(scopes) <= set(owner['projects'])):
                raise PermissionDenied('이 소유자 범위 밖의 연결은 해제할 수 없어요.')
            if target['revoked'] is not None:
                return False
            c.execute('UPDATE bridge_tokens SET revoked=? WHERE token_hash=?', (self._now(), target_hash))
            tasks = c.execute("SELECT id FROM bridge_tasks WHERE worker_token_hash=? AND state='running'", (target_hash,)).fetchall()
            for task in tasks:
                c.execute("UPDATE bridge_tasks SET state='needs_verification',updated=? WHERE id=?", (self._now(), task['id']))
                self._event(c, task['id'], owner['subject'], 'worker_connection_revoked', 'needs_verification')
            return True
