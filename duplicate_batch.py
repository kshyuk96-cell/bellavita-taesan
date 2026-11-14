"""Persistent, owner-scoped work queue for read-only duplicate scan chunks.

This module stores row numbers and result categories only, never order contents.
The caller must authenticate ``owner``, obtain a fresh digest of the entire scan
snapshot (including venue-registry version), and do the actual comparisons.
``checked_rows`` attests that every target in a leased chunk was checked against
that same baseline; this queue cannot verify a scanner's substantive findings.
No order writes, provider calls, triggers, or worker startup happen here.

Mutations commit their own transaction unless the caller already has one; in
that case a savepoint is used and the caller remains responsible for commit.
Use one SQLite connection per worker, with an appropriate busy timeout.
"""
import contextlib
import hashlib
import json
import re
import secrets
import time

MAX_ROWS = 50_000
MAX_CHUNK_SIZE = 500
MAX_ACTIVE_JOBS = 10
MAX_CHUNK_RESULTS = 10_000
MAX_RESULTS = 100_000
MAX_ATTEMPTS = 5
CATEGORIES = ('duplicate', 'review', 'different')


class NotFound(ValueError):
    """The authenticated owner has no such job."""


class SnapshotChanged(ValueError):
    """The live scan snapshot differs from the enqueued snapshot."""


class StaleLease(ValueError):
    """The chunk is not held by this unexpired claim."""


def setup(connection):
    statements = (
        '''CREATE TABLE IF NOT EXISTS duplicate_batch_jobs (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, snapshot TEXT NOT NULL,
            fingerprint TEXT NOT NULL, row_numbers TEXT NOT NULL, total INTEGER NOT NULL,
            chunk_size INTEGER NOT NULL, status TEXT NOT NULL, created REAL NOT NULL)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS duplicate_batch_active
            ON duplicate_batch_jobs(owner,fingerprint)
            WHERE status IN ('queued','running')''',
        '''CREATE TABLE IF NOT EXISTS duplicate_batch_chunks (
            job TEXT NOT NULL, chunk_index INTEGER NOT NULL, targets TEXT NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            lease_token TEXT, lease_until REAL, PRIMARY KEY(job,chunk_index))''',
        '''CREATE TABLE IF NOT EXISTS duplicate_batch_results (
            job TEXT NOT NULL, row_a INTEGER NOT NULL, row_b INTEGER NOT NULL,
            category TEXT NOT NULL, PRIMARY KEY(job,row_a,row_b))''',
    )
    for statement in statements:
        connection.execute(statement)


@contextlib.contextmanager
def _write(connection):
    nested = connection.in_transaction
    name = 'duplicate_batch_' + secrets.token_hex(8)
    connection.execute('SAVEPOINT ' + name if nested else 'BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        if nested:
            connection.execute('ROLLBACK TO ' + name)
            connection.execute('RELEASE ' + name)
        else:
            connection.rollback()
        raise
    else:
        if nested:
            connection.execute('RELEASE ' + name)
        else:
            connection.commit()


def _one(connection, sql, args=()):
    cursor = connection.execute(sql, args)
    row = cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description), row)) if row is not None else None


def _job(connection, owner, job_id):
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 512:
        raise NotFound('작업을 확인할 수 없어요.')
    row = _one(connection, 'SELECT * FROM duplicate_batch_jobs WHERE id=? AND owner=?', (job_id, owner))
    if row is None:
        raise NotFound('작업을 확인할 수 없어요.')
    return row


def _snapshot(job, current):
    if not isinstance(current, str) or current != job['snapshot']:
        raise SnapshotChanged('원장 또는 장소 매핑이 바뀌었어요. 최신 자료로 새 점검을 시작해 주세요.')


def _rows(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_ROWS:
        raise ValueError('대상 행 수를 확인해 주세요.')
    if any(type(row) is not int or not 1 <= row <= 10_000_000 for row in values):
        raise ValueError('행 번호를 확인해 주세요.')
    if len(set(values)) != len(values):
        raise ValueError('대상 행 번호가 중복됐어요.')
    return sorted(values)


def enqueue(connection, owner, snapshot_hash, row_numbers, *, chunk_size=100):
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 512:
        raise ValueError('직원 계정을 확인해 주세요.')
    if not isinstance(snapshot_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', snapshot_hash):
        raise ValueError('조회 자료의 SHA-256 근거가 필요해요.')
    if type(chunk_size) is not int or not 1 <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError('청크 크기를 확인해 주세요.')
    rows = _rows(row_numbers)
    encoded = json.dumps(rows, separators=(',', ':'))
    fingerprint = hashlib.sha256((snapshot_hash + ':' + encoded).encode()).hexdigest()
    with _write(connection):
        existing = _one(connection, '''SELECT id FROM duplicate_batch_jobs
            WHERE owner=? AND fingerprint=? AND status IN ('queued','running')''', (owner, fingerprint))
        if existing:
            return existing['id']
        active = connection.execute('''SELECT COUNT(*) FROM duplicate_batch_jobs
            WHERE owner=? AND status IN ('queued','running')''', (owner,)).fetchone()[0]
        if active >= MAX_ACTIVE_JOBS:
            raise ValueError('진행 중인 점검이 많아요. 기존 점검을 완료하거나 취소해 주세요.')
        job_id = secrets.token_hex(16)
        connection.execute('INSERT INTO duplicate_batch_jobs VALUES (?,?,?,?,?,?,?,?,?)',
            (job_id, owner, snapshot_hash, fingerprint, encoded, len(rows), chunk_size, 'queued', time.time()))
        connection.executemany('''INSERT INTO duplicate_batch_chunks
            (job,chunk_index,targets,status) VALUES (?,?,?,'pending')''',
            ((job_id, start // chunk_size, json.dumps(rows[start:start + chunk_size]))
             for start in range(0, len(rows), chunk_size)))
        return job_id


def claim(connection, owner, job_id, current_snapshot_hash, *, lease_seconds=60):
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 900:
        raise ValueError('작업 임대 시간을 확인해 주세요.')
    with _write(connection):
        job = _job(connection, owner, job_id)
        _snapshot(job, current_snapshot_hash)
        if job['status'] not in ('queued', 'running'):
            return None
        now = time.time()
        chunk = _one(connection, '''SELECT * FROM duplicate_batch_chunks WHERE job=?
            AND (status='pending' OR (status='leased' AND lease_until<=?))
            ORDER BY chunk_index LIMIT 1''', (job_id, now))
        if chunk is None:
            return None
        if chunk['attempts'] >= MAX_ATTEMPTS:
            connection.execute("UPDATE duplicate_batch_jobs SET status='failed' WHERE id=?", (job_id,))
            return None
        token = secrets.token_hex(16)
        updated = connection.execute('''UPDATE duplicate_batch_chunks
            SET status='leased',attempts=attempts+1,lease_token=?,lease_until=?
            WHERE job=? AND chunk_index=? AND attempts=?
            AND (status='pending' OR (status='leased' AND lease_until<=?))''',
            (token, now + lease_seconds, job_id, chunk['chunk_index'], chunk['attempts'], now))
        if updated.rowcount != 1:
            return None
        connection.execute("UPDATE duplicate_batch_jobs SET status='running' WHERE id=?", (job_id,))
        return {'job_id': job_id, 'chunk_index': chunk['chunk_index'], 'lease_token': token,
                'row_numbers': json.loads(chunk['targets']), 'snapshot_hash': job['snapshot'],
                'lease_until': now + lease_seconds, 'attempt': chunk['attempts'] + 1}


def submit(connection, owner, job_id, chunk_index, lease_token, current_snapshot_hash,
           checked_rows, results):
    if type(chunk_index) is not int or chunk_index < 0 or not isinstance(lease_token, str):
        raise StaleLease('유효한 점검 작업을 확인할 수 없어요.')
    with _write(connection):
        job = _job(connection, owner, job_id)
        _snapshot(job, current_snapshot_hash)
        chunk = _one(connection, 'SELECT * FROM duplicate_batch_chunks WHERE job=? AND chunk_index=?',
                     (job_id, chunk_index))
        if (job['status'] != 'running' or chunk is None or chunk['status'] != 'leased'
                or chunk['lease_token'] != lease_token or chunk['lease_until'] <= time.time()):
            raise StaleLease('만료되거나 이미 처리된 점검 결과예요.')
        targets = json.loads(chunk['targets'])
        if _rows(checked_rows) != targets:
            raise ValueError('청크의 모든 대상 행을 같은 자료로 확인해야 해요.')
        if not isinstance(results, dict) or set(results) - set(CATEGORIES):
            raise ValueError('점검 결과 분류를 확인해 주세요.')
        if any(not isinstance(pairs, (list, tuple)) for pairs in results.values()):
            raise ValueError('행 번호 쌍만 결과로 제출해 주세요.')
        if sum(len(pairs) for pairs in results.values()) > MAX_CHUNK_RESULTS:
            raise ValueError('결과가 너무 많아요. 점검 범위를 나눠 주세요.')
        allowed, target_set, prepared = set(json.loads(job['row_numbers'])), set(targets), {}
        for category, pairs in results.items():
            for pair in pairs:
                if (not isinstance(pair, (list, tuple)) or len(pair) != 2 or
                        any(type(row) is not int or row not in allowed for row in pair) or
                        pair[0] == pair[1] or not any(row in target_set for row in pair)):
                    raise ValueError('등록된 대상 행 번호 쌍만 결과로 제출해 주세요.')
                key = tuple(sorted(pair))
                if key in prepared and prepared[key] != category:
                    raise ValueError('같은 행 쌍의 점검 결과가 충돌해요.')
                prepared[key] = category
        count = connection.execute('SELECT COUNT(*) FROM duplicate_batch_results WHERE job=?', (job_id,)).fetchone()[0]
        new_results = []
        for (a, b), category in prepared.items():
            existing = _one(connection, '''SELECT category FROM duplicate_batch_results
                WHERE job=? AND row_a=? AND row_b=?''', (job_id, a, b))
            if existing and existing['category'] != category:
                raise ValueError('앞서 확인한 행 쌍의 결과와 달라요. 자료를 다시 확인해 주세요.')
            if not existing:
                new_results.append((job_id, a, b, category))
        if count + len(new_results) > MAX_RESULTS:
            raise ValueError('누적 결과 한도를 넘었어요. 점검 범위를 나눠 주세요.')
        connection.executemany('INSERT INTO duplicate_batch_results VALUES (?,?,?,?)', new_results)
        updated = connection.execute('''UPDATE duplicate_batch_chunks
            SET status='done',lease_token=NULL,lease_until=NULL
            WHERE job=? AND chunk_index=? AND status='leased' AND lease_token=? AND lease_until>?''',
            (job_id, chunk_index, lease_token, time.time()))
        if updated.rowcount != 1:
            raise StaleLease('결과를 저장하기 전에 작업이 만료됐어요.')
        remaining = connection.execute("SELECT COUNT(*) FROM duplicate_batch_chunks WHERE job=? AND status!='done'",
                                       (job_id,)).fetchone()[0]
        if remaining == 0:
            connection.execute("UPDATE duplicate_batch_jobs SET status='completed' WHERE id=?", (job_id,))
        return progress(connection, owner, job_id)


def progress(connection, owner, job_id):
    job = _job(connection, owner, job_id)
    chunks = connection.execute('''SELECT chunk_index,targets,status FROM duplicate_batch_chunks
        WHERE job=? ORDER BY chunk_index''', (job_id,)).fetchall()
    checked, cursor, done, contiguous = 0, 0, 0, True
    for _, targets, status in chunks:
        length = len(json.loads(targets))
        if status == 'done':
            checked += length
            done += 1
            if contiguous:
                cursor += length
        else:
            contiguous = False
    counts = {category: 0 for category in CATEGORIES}
    counts.update(dict(connection.execute('''SELECT category,COUNT(*) FROM duplicate_batch_results
        WHERE job=? GROUP BY category''', (job_id,)).fetchall()))
    return {'job_id': job_id, 'status': job['status'], 'snapshot_hash': job['snapshot'],
            'total_rows': job['total'], 'checked_rows': checked, 'cursor': cursor,
            'total_chunks': len(chunks), 'completed_chunks': done, 'result_counts': counts}


def get_results(connection, owner, job_id, *, category=None, offset=0, limit=100):
    _job(connection, owner, job_id)
    if category is not None and category not in CATEGORIES:
        raise ValueError('점검 결과 분류를 확인해 주세요.')
    if type(offset) is not int or not 0 <= offset <= MAX_RESULTS or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('결과 조회 범위를 확인해 주세요.')
    rows = connection.execute('''SELECT category,row_a,row_b FROM duplicate_batch_results
        WHERE job=? AND (? IS NULL OR category=?) ORDER BY row_a,row_b LIMIT ? OFFSET ?''',
        (job_id, category, category, limit, offset)).fetchall()
    return [{'category': item[0], 'rows': [item[1], item[2]]} for item in rows]


def cancel(connection, owner, job_id):
    with _write(connection):
        job = _job(connection, owner, job_id)
        if job['status'] in ('queued', 'running'):
            connection.execute("UPDATE duplicate_batch_jobs SET status='canceled' WHERE id=?", (job_id,))
            connection.execute('''UPDATE duplicate_batch_chunks SET lease_token=NULL,lease_until=NULL
                WHERE job=? AND status='leased' ''', (job_id,))
        return progress(connection, owner, job_id)
