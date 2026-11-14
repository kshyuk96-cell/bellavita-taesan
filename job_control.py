"""Owner-scoped controls for existing jobs; cancellation is not thread termination.

The server must authenticate ``owner`` on each call. No identity or role may come
from a model answer or an uploaded file. Use the existing jobs table; this module
does not execute business actions. Mutations commit with the caller's connection
context. Commit request_cancel() before invoking cancel_queued() from a fresh
connection. Workers must use store_result() rather than unconditional UPDATEs.

Read jobs and explicitly guarded route generation can be canceled here. In-flight
work can finish, but its late result cannot replace the persisted cancellation.
Only server-created route jobs whose persisted input has cancel_safe exactly True
are eligible: that marker attests deferred session persistence guarded by the same
transaction as final job state. A request/model cannot grant that marker. Legacy
route jobs and invitation generation (which may create BlogPay products) remain
ineligible.
"""
import json
import re


SAFE_KINDS = frozenset({'delivery_count', 'venue_rank', 'staff_report', 'photo',
                        'photo_followup', 'order_duplicate', 'duplicate_refresh', 'diagnostics'})
ACTIVE_STATES = ('queued', 'running')
_JOB = re.compile(r'[a-f0-9]{12}')
_FINGERPRINT = re.compile(r'[a-f0-9]{64}')
CANCELED_SUMMARY = '조회 결과 전달을 중단했어요. 진행 중인 읽기는 끝날 수 있지만 결과는 표시하지 않아요.'
ROUTE_CANCELED_SUMMARY = '배송루트 결과 저장·전달을 중단했어요. 진행 중인 계산은 끝날 수 있지만 새 배정안으로 반영하지 않아요.'


def _owner(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError('직원 계정을 확인해 주세요.')


def _job(value):
    if not isinstance(value, str) or not _JOB.fullmatch(value):
        raise ValueError('작업을 다시 선택해 주세요.')


def _one(c, sql, args):
    cursor = c.execute(sql, args)
    row = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), row)) if row else None


def find_active(c, owner, fingerprint):
    """Reuse only the same owner's exact active request, including after restart."""
    _owner(owner)
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError('작업 조건을 다시 확인해 주세요.')
    return _one(c, '''SELECT id,owner,fingerprint,created,state,input FROM jobs
        WHERE owner=? AND fingerprint=? AND state='running'
        ORDER BY created DESC,id DESC LIMIT 1''', (owner, fingerprint))


def _response(status, summary, job=None):
    return {'status': status, 'summary': summary, 'job': job}


def _kind(row):
    try:
        body = json.loads(row['input'])
        value = body.get('kind') if isinstance(body, dict) else None
        return value if isinstance(value, str) else None
    except (ValueError, TypeError):
        return None


def is_read_or_safe_generation(row):
    """Inspect trusted persisted input; never a user-supplied cancel_safe field."""
    kind = _kind(row)
    if kind in SAFE_KINDS:
        return True
    if kind != 'route':
        return False
    try:
        body = json.loads(row['input'])
        return isinstance(body, dict) and body.get('cancel_safe') is True
    except (ValueError, TypeError):
        return False


def request_cancel(c, owner, job=None):
    """Persist cancellation for an eligible owned read; never claim to kill it."""
    _owner(owner)
    if job is not None:
        _job(job)
        row = _one(c, 'SELECT id,state,input FROM jobs WHERE owner=? AND id=?', (owner, job))
    else:
        row = _one(c, '''SELECT id,state,input FROM jobs WHERE owner=?
            AND state IN ('queued','running') ORDER BY created DESC,id DESC LIMIT 1''', (owner,))
    if not row:
        return _response('not_found', '이 계정의 진행 중인 작업을 찾지 못했어요.')
    job = row['id']
    if row['state'] == 'cancelled':
        return _response('already_cancelled', '이미 결과 전달을 중단한 조회예요.', job)
    if row['state'] not in ACTIVE_STATES:
        return _response('finished', '이미 끝난 작업이에요. 실행된 업무를 되돌리지는 않았어요.', job)
    if not is_read_or_safe_generation(row):
        return _response('not_cancelable', '이 작업은 실행 여부 확인이 필요해 여기서 취소하지 않았어요.', job)
    summary = ROUTE_CANCELED_SUMMARY if _kind(row) == 'route' else CANCELED_SUMMARY
    result = json.dumps({'summary': summary, 'cancelled': True}, ensure_ascii=False)
    changed = c.execute('''UPDATE jobs SET state='cancelled',result=?
        WHERE owner=? AND id=? AND state IN ('queued','running')''', (result, owner, job))
    if not changed.rowcount:
        latest = _one(c, 'SELECT state FROM jobs WHERE owner=? AND id=?', (owner, job))
        if latest and latest['state'] == 'cancelled':
            return _response('already_cancelled', '이미 결과 전달을 중단한 조회예요.', job)
        return _response('finished', '상태가 바뀌어 취소하지 않았어요. 최신 결과를 확인해 주세요.', job)
    return _response('cancelled', summary, job)


def is_cancelled(c, owner, job):
    _owner(owner)
    _job(job)
    row = _one(c, 'SELECT state FROM jobs WHERE owner=? AND id=?', (owner, job))
    return bool(row and row['state'] == 'cancelled')


def cancel_queued(c, owner, job, active):
    """After committed cancellation, attempt Future.cancel without killing threads.

    False means absent/already running/not canceled; it never implies rollback of
    a business action. Keep an in-flight future in ACTIVE until it actually ends.
    """
    if c.in_transaction or not is_cancelled(c, owner, job):
        return False
    future = active.get(job)
    if future is None:
        return False
    return bool(future.cancel())


def store_result(c, owner, job, result):
    """Atomically finish a running job only; canceled/finished results are immutable."""
    _owner(owner)
    _job(job)
    if not isinstance(result, dict):
        raise ValueError('작업 결과를 확인해 주세요.')
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    return bool(c.execute('''UPDATE jobs SET state='done',result=?
        WHERE owner=? AND id=? AND state='running' ''', (encoded, owner, job)).rowcount)
