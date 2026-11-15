"""Business decisions are proposals; registered providers establish execution facts."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time


class CoreError(ValueError):
    pass


class AccessDenied(CoreError):
    pass


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise CoreError('invalid identifier')
    return value


def encoded(value):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                          separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise CoreError('invalid JSON data') from exc
    if len(text.encode()) > 65536:
        raise CoreError('data too large')
    return text


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    tenant: str
    subject: str
    roles: frozenset

    def __post_init__(self):
        ident(self.tenant)
        ident(self.subject)
        if type(self.roles) is not frozenset or not self.roles or not self.roles <= {'reader', 'writer', 'admin'}:
            raise CoreError('invalid roles')


@dataclass(frozen=True)
class Evidence:
    source: str
    receipt: str
    observed_at: float

    def __post_init__(self):
        ident(self.source)
        ident(self.receipt)
        if type(self.observed_at) not in (float, int) or not math.isfinite(self.observed_at) or self.observed_at <= 0:
            raise CoreError('invalid observation time')


@dataclass(frozen=True)
class Result:
    status: str
    data: dict = field(default_factory=dict)
    evidence: Evidence | None = None
    count: int | None = None
    applied: bool = False
    readback_matches: bool = False

    def __post_init__(self):
        if self.status not in {'ok', 'empty', 'unavailable', 'denied', 'uncertain'}:
            raise CoreError('invalid result status')
        if type(self.data) is not dict:
            raise CoreError('invalid result data')
        encoded(self.data)
        if self.status in {'ok', 'empty'} and not isinstance(self.evidence, Evidence):
            raise CoreError('evidence required')
        if self.count is not None and (type(self.count) is not int or self.count < 0):
            raise CoreError('invalid count')
        if self.status == 'empty' and self.count != 0:
            raise CoreError('empty requires zero count')
        if self.status == 'ok' and self.count == 0:
            raise CoreError('zero results must be empty')
        if type(self.applied) is not bool or type(self.readback_matches) is not bool:
            raise CoreError('invalid execution proof')


@dataclass(frozen=True)
class Snapshot:
    target: str
    version: str
    preview: dict

    def __post_init__(self):
        ident(self.target)
        ident(self.version)
        if type(self.preview) is not dict:
            raise CoreError('invalid preview')
        encoded(self.preview)


@dataclass(frozen=True)
class Field:
    label: str
    kind: str = 'text'
    required: bool = True
    choices: tuple = ()

    def validate(self, value):
        if self.kind == 'text':
            valid = type(value) is str and 0 < len(value.strip()) <= 1000
        elif self.kind == 'integer':
            valid = type(value) is int and 0 <= value <= 1000000
        elif self.kind == 'boolean':
            valid = type(value) is bool
        else:
            raise CoreError('unknown field type')
        if not valid or (self.choices and value not in self.choices):
            raise CoreError('invalid field value')
        return value


@dataclass(frozen=True)
class Plan:
    tool: str
    arguments: dict = field(default_factory=dict)
    follow_up: bool = False

    def __post_init__(self):
        ident(self.tool)
        if type(self.arguments) is not dict or type(self.follow_up) is not bool:
            raise CoreError('invalid plan')
        encoded(self.arguments)


@dataclass(frozen=True)
class Attachment:
    """Ephemeral input. Host performs authenticated download and obtains consent."""
    kind: str
    content: bytes
    consent: bool

    def __post_init__(self):
        if self.kind not in {'image', 'audio'} or type(self.content) is not bytes or not 0 < len(self.content) <= 10 * 1024 * 1024:
            raise CoreError('invalid attachment')
        if self.consent is not True:
            raise AccessDenied('attachment consent required')


@dataclass(frozen=True)
class Tool:
    name: str
    fields: dict
    execute: object
    kind: str = 'read'
    roles: frozenset = frozenset({'reader', 'writer', 'admin'})
    context_fields: frozenset = frozenset()
    prepare: object = None
    enabled: bool = False
    version: str = 'v1'

    def __post_init__(self):
        ident(self.name)
        ident(self.version)
        if self.kind not in {'read', 'write'} or type(self.fields) is not dict:
            raise CoreError('invalid tool')
        for key, spec in self.fields.items():
            ident(key)
            if not isinstance(spec, Field):
                raise CoreError('invalid field')
        if type(self.context_fields) is not frozenset or not self.context_fields <= self.fields.keys():
            raise CoreError('invalid context fields')
        if type(self.roles) is not frozenset or not self.roles or not self.roles <= {'reader', 'writer', 'admin'}:
            raise CoreError('invalid tool roles')
        if self.kind == 'write' and ('reader' in self.roles or not callable(self.prepare)):
            raise CoreError('writes require preparation and writer/admin roles')
        if not callable(self.execute) or type(self.enabled) is not bool:
            raise CoreError('invalid executor')


class Registry:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        if not isinstance(tool, Tool) or tool.name in self._tools:
            raise CoreError('duplicate or invalid tool')
        # Copy mutable schema so the caller cannot silently change a registered contract.
        from dataclasses import replace
        self._tools[tool.name] = replace(tool, fields=dict(tool.fields))

    def get(self, name):
        return self._tools.get(name)


@dataclass(frozen=True)
class Reply:
    status: str
    message: str
    data: dict = field(default_factory=dict)


class Store:
    def __init__(self, path, *, clock=time.time, ttl=1800):
        self.path = Path(path).absolute()
        self.clock = clock
        if type(ttl) is not int or not 60 <= ttl <= 86400:
            raise CoreError('invalid retention')
        self.ttl = ttl
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise CoreError('symlink storage forbidden')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.stat().st_mode & 0o077:
            raise CoreError('private storage directory required')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS turns (
              tenant TEXT, subject TEXT, channel TEXT, conversation TEXT,
              state TEXT NOT NULL, expires REAL NOT NULL,
              PRIMARY KEY(tenant, subject, channel, conversation));
            CREATE TABLE IF NOT EXISTS requests (
              tenant TEXT, subject TEXT, request TEXT, fingerprint TEXT, state TEXT,
              response TEXT, tool TEXT, expires REAL, PRIMARY KEY(tenant, subject, request));
            CREATE TABLE IF NOT EXISTS previews (
              id TEXT PRIMARY KEY, tenant TEXT, subject TEXT, channel TEXT,
              conversation TEXT, tool TEXT, arguments TEXT, snapshot TEXT,
              expires REAL, state TEXT);
            CREATE TABLE IF NOT EXISTS locks (
              tenant TEXT, subject TEXT, channel TEXT, conversation TEXT, request TEXT,
              PRIMARY KEY(tenant, subject, channel, conversation));
            ''')

    @contextmanager
    def db(self):
        if self.path.is_symlink():
            raise CoreError('symlink storage forbidden')
        c = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise CoreError('invalid clock')
        return value

    def purge(self):
        """Erase expired draft content; retain hashed execution receipts to prevent replay."""
        with self.db() as c:
            c.execute('DELETE FROM turns WHERE expires<=?', (self.now(),))
            c.execute('UPDATE requests SET response=NULL WHERE expires<=? AND state=?', (self.now(), 'done'))
            c.execute("UPDATE previews SET arguments='{}', snapshot='{}', state=CASE WHEN state='pending' THEN 'expired' ELSE state END WHERE expires<=? AND state!='executing'", (self.now(),))


class Core:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry

    @staticmethod
    def scope(principal, channel, conversation):
        if not isinstance(principal, Principal):
            raise AccessDenied('authenticated principal required')
        return (principal.tenant, principal.subject, ident(channel), ident(conversation))

    def _result(self, value, *, write=False):
        if not isinstance(value, Result):
            return Reply('uncertain', '실제 처리 결과를 확인하지 못했습니다.')
        if write and value.status in {'ok', 'empty'} and not (
                value.status == 'ok' and value.applied and value.readback_matches):
            return Reply('uncertain', '반영 결과 재확인이 필요합니다.')
        messages = {'ok': '반영 후 재확인했습니다.' if write else '조회했습니다.',
                    'empty': '조회 결과 0건입니다.', 'unavailable': '연결 상태를 확인할 수 없습니다.',
                    'denied': '조회 또는 실행 권한이 없습니다.', 'uncertain': '처리 결과 확인이 필요합니다.'}
        data = {'result': value.data, 'count': value.count}
        if value.evidence:
            data['evidence'] = asdict(value.evidence)
        return Reply(value.status, messages[value.status], data)

    def handle(self, principal, channel, conversation, request_id, *, plan=None, confirm=None):
        """Called only by a host that freshly authenticated this request. confirm is a UI action ID.

        Neither the model nor attachments may set confirm or Principal. Mutation providers must
        atomically compare Snapshot.version and deduplicate the supplied idempotency_key.
        """
        scope = self.scope(principal, channel, conversation)
        self.store.purge()
        ident(request_id)
        if (plan is None) == (confirm is None) or (plan is not None and not isinstance(plan, Plan)):
            raise CoreError('provide exactly one plan or confirmation')
        if confirm is not None:
            ident(confirm)
        fingerprint = digest({'scope': scope, 'plan': asdict(plan) if plan else None, 'confirm': confirm})
        key = (principal.tenant, principal.subject, request_id)
        with self.store.db() as c:
            old = c.execute('SELECT * FROM requests WHERE tenant=? AND subject=? AND request=?', key).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise CoreError('request ID reused with different content')
                denied = self._allowed(principal, self.registry.get(old['tool']))
                if denied:
                    return denied
                if old['expires'] <= self.store.now() and old['state'] == 'done':
                    return Reply('expired', '이전 요청의 상세 결과 보관 기간이 지났습니다. 기존 요청은 재실행하지 않습니다.')
                if old['response']:
                    return Reply(**json.loads(old['response']))
                return Reply('uncertain', '이 요청은 처리 중이거나 결과 확인이 필요합니다. 다시 실행하지 마십시오.')
            locked = c.execute('SELECT 1 FROM locks WHERE tenant=? AND subject=? AND channel=? AND conversation=?', scope).fetchone()
            if locked:
                return Reply('busy', '앞선 요청을 확인 중입니다.')
            if plan:
                tool_name = plan.tool
            else:
                preview_row = c.execute('SELECT tool FROM previews WHERE id=? AND tenant=? AND subject=? AND channel=? AND conversation=?', (confirm, *scope)).fetchone()
                tool_name = preview_row['tool'] if preview_row else ''
            c.execute('INSERT INTO requests VALUES (?,?,?,?,?,NULL,?,?)', (*key, fingerprint, 'running', tool_name, self.store.now() + self.store.ttl))
            c.execute('INSERT INTO locks VALUES (?,?,?,?,?)', (*scope, request_id))
        try:
            response = self._handle(principal, scope, request_id, plan, confirm)
        except Exception:
            self._clear_scope(scope)
            # An executor may have applied a change before failing. Never claim failure = no change.
            response = Reply('uncertain', '처리 결과 확인이 필요합니다. 같은 요청을 다시 실행하지 마십시오.')
        with self.store.db() as c:
            c.execute('UPDATE requests SET state=?,response=? WHERE tenant=? AND subject=? AND request=?',
                      ('done', encoded(asdict(response)), *key))
            c.execute('DELETE FROM locks WHERE tenant=? AND subject=? AND channel=? AND conversation=? AND request=?', (*scope, request_id))
        return response

    def _clear_scope(self, scope):
        with self.store.db() as c:
            c.execute('DELETE FROM turns WHERE tenant=? AND subject=? AND channel=? AND conversation=?', scope)
            c.execute("UPDATE previews SET state='cancelled',arguments='{}',snapshot='{}' WHERE tenant=? AND subject=? AND channel=? AND conversation=? AND state='pending'", scope)

    def _allowed(self, principal, tool):
        if tool is None or not tool.enabled:
            return Reply('unavailable', '이 기능은 아직 연결되지 않았습니다.')
        if not principal.roles & tool.roles:
            return Reply('denied', '이 업무를 실행할 권한이 없습니다.')
        return None

    def _handle(self, principal, scope, request_id, plan, confirm):
        now = self.store.now()
        if confirm:
            with self.store.db() as c:
                row = c.execute('SELECT * FROM previews WHERE id=?', (confirm,)).fetchone()
                if not row or tuple(row[k] for k in ('tenant', 'subject', 'channel', 'conversation')) != scope:
                    return Reply('denied', '이 변경 확인 요청에 접근할 수 없습니다.')
                if row['state'] != 'pending' or row['expires'] <= now:
                    return Reply('expired', '변경 확인 요청이 만료되었거나 이미 처리되었습니다.')
                tool = self.registry.get(row['tool'])
                denied = self._allowed(principal, tool)
                if denied:
                    return denied
                if tool.kind != 'write':
                    return Reply('uncertain', '도구 설정이 변경되어 다시 확인해야 합니다.')
                args, saved = json.loads(row['arguments']), json.loads(row['snapshot'])
                old = saved['snapshot']
                if saved['tool_version'] != tool.version:
                    return Reply('stale', '도구 버전이 바뀌었습니다. 변경 내용을 다시 확인해 주십시오.')
                c.execute('UPDATE previews SET state=? WHERE id=?', ('executing', confirm))
            try:
                current = tool.prepare(principal, dict(args))
                if not isinstance(current, Snapshot) or digest(asdict(current)) != digest(old):
                    reply = Reply('stale', '주문 정보가 바뀌었습니다. 최신 내용을 다시 확인해 주십시오.')
                else:
                    operation_key = digest({'scope': scope, 'preview': confirm})
                    reply = self._result(tool.execute(principal, dict(args), current,
                                                     idempotency_key=operation_key), write=True)
            except Exception:
                reply = Reply('uncertain', '변경 반영 여부를 확인해야 합니다. 재실행하지 마십시오.')
            with self.store.db() as c:
                c.execute('UPDATE previews SET state=? WHERE id=?',
                          ('uncertain' if reply.status == 'uncertain' else 'used', confirm))
                c.execute('UPDATE previews SET arguments=?,snapshot=? WHERE id=?', ('{}', '{}', confirm))
            return reply
        tool = self.registry.get(plan.tool)
        denied = self._allowed(principal, tool)
        if denied:
            self._clear_scope(scope)
            return denied
        with self.store.db() as c:
            row = c.execute('SELECT * FROM turns WHERE tenant=? AND subject=? AND channel=? AND conversation=?', scope).fetchone()
            state = json.loads(row['state']) if row and row['expires'] > now else {}
            if plan.follow_up and state.get('tool') == plan.tool:
                prior = state.get('arguments', {})
                # Missing-field drafts preserve supplied fields; completed reads preserve only allowed filters.
                keep = tool.fields.keys() if state.get('draft') else tool.context_fields
                args = {k: v for k, v in prior.items() if k in keep}
            else:
                args = {}
            args.update(plan.arguments)
            if not args.keys() <= tool.fields.keys():
                raise CoreError('unregistered argument')
            args = {k: tool.fields[k].validate(v) for k, v in args.items()}
            missing = [k for k, spec in tool.fields.items() if spec.required and k not in args]
            state = {'tool': plan.tool, 'arguments': args, 'draft': bool(missing)}
            c.execute('INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?,?)', (*scope, encoded(state), now + self.store.ttl))
            # Any new task/condition invalidates earlier previews for this conversation.
            c.execute('UPDATE previews SET state=? WHERE tenant=? AND subject=? AND channel=? AND conversation=? AND state=?', ('cancelled', *scope, 'pending'))
        if missing:
            return Reply('clarify', ' · '.join(tool.fields[k].label for k in missing) + '만 알려주십시오.', {'missing': missing})
        if tool.kind == 'read':
            return self._result(tool.execute(principal, dict(args)))
        snapshot = tool.prepare(principal, dict(args))
        if not isinstance(snapshot, Snapshot):
            return Reply('unavailable', '변경 전 정보를 읽을 수 없습니다.')
        preview = secrets.token_hex(16)
        with self.store.db() as c:
            c.execute('INSERT INTO previews VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (preview, *scope, plan.tool, encoded(args), encoded({'snapshot': asdict(snapshot), 'tool_version': tool.version}), now + 300, 'pending'))
        return Reply('preview', '변경 내용을 확인한 뒤 승인해 주십시오.', {'confirmation': preview, 'snapshot': asdict(snapshot)})
