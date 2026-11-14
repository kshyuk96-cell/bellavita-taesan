"""Read-only Sheets adapter for the resumable duplicate-scan queue.

Caller authenticates owner and supplies a read-only ``read_ranges`` transport.
``resolver_factory()`` creates a fresh trusted order_locations resolver per
chunk; its optional ``registry_version``/``version`` identifies the configured
registry snapshot. Live providers without such a version are bound to the
provider contract namespace, not a claim that upstream data is immutable.
Only row numbers, digests and status metadata are persisted. AC is included as
an opaque row marker in the digest, never inferred to be a unique order ID.
There is no worker startup, trigger installation, order write, or send adapter.
"""
import contextlib
import hashlib
import json
from pathlib import Path

import duplicate_batch as queue
import order_duplicates
import order_locations
import order_location_provider

RANGES = (*order_duplicates.RANGES, "'액자화환'!AC4:AC")
CHUNK_SIZE = 10
MAX_CANDIDATES = 5000
MAX_LEDGER_ROWS = 100_000


class SourceReadError(RuntimeError):
    pass


class SourceDataError(ValueError):
    pass


def _setup(connection):
    queue.setup(connection)
    connection.execute('''CREATE TABLE IF NOT EXISTS duplicate_batch_context (
        job TEXT PRIMARY KEY, owner TEXT NOT NULL, target_row INTEGER NOT NULL,
        candidate_count INTEGER NOT NULL, halted_reason TEXT)''')


def _version(resolver_factory):
    source_hash = hashlib.sha256()
    for module in (order_duplicates, order_locations):
        source_hash.update(Path(module.__file__).read_bytes())
    source_hash.update(b'duplicate-batch-service-v1')
    if resolver_factory is None:
        registry = 'unconnected'
    else:
        registry = getattr(resolver_factory, 'registry_version', None)
        if registry is None:
            registry = getattr(resolver_factory, 'version', order_location_provider.PROVIDER)
        if not isinstance(registry, str) or not registry.strip() or len(registry) > 200:
            raise SourceDataError('장소 제공자 버전 설정을 확인해 주세요.')
    return [source_hash.hexdigest(), registry]


def _cell(rows, index, column=0):
    if 0 <= index < len(rows) and isinstance(rows[index], (list, tuple)) and column < len(rows[index]):
        return rows[index][column]
    return ''


def _base(schedule, place, sender):
    # Missing place must remain a candidate for review, never disappear silently.
    result = order_duplicates.key(schedule, [place], [sender])
    if result is None:
        result = order_duplicates.key(schedule, ['__location_missing__'], [sender])
    return (result[0], result[1], result[3]) if result is not None else None


def _read(target_row, read_ranges, resolver_factory):
    if type(target_row) is not int or not 5 <= target_row <= MAX_LEDGER_ROWS + 4:
        raise SourceDataError('액자화환 원장의 현재 행 번호를 확인해 주세요.')
    try:
        values = read_ranges(RANGES)
    except Exception:
        raise SourceReadError('원장을 읽지 못해 중복 대조를 진행하지 못했어요. 잠시 뒤 다시 조회해 주세요.') from None
    if not isinstance(values, (list, tuple)) or len(values) != len(RANGES):
        raise SourceDataError('원장 조회 범위를 확인하지 못했어요.')
    if any(not isinstance(rows, (list, tuple)) or len(rows) > MAX_LEDGER_ROWS + 1 for rows in values):
        raise SourceDataError('원장 조회 범위가 너무 크거나 읽기 형식이 달라요.')
    schedules, places, senders, markers = values
    headers = [_cell(schedules, 0), _cell(schedules, 0, 1), _cell(places, 0), _cell(senders, 0)]
    if [order_duplicates.normalized(value).replace(' ', '') for value in headers] != [
            '배송일', '행사시간', '설치장소', '보내는분요약']:
        raise SourceDataError('액자화환 원장 제목이 달라 중복 대조를 진행하지 못했어요.')
    index = target_row - 4
    fields = [schedules[index] if index < len(schedules) else [],
              _cell(places, index), _cell(senders, index), _cell(markers, index)]
    if not any(fields):
        raise SourceDataError('원장에서 대상 행을 찾지 못했어요. 현재 행 번호를 확인해 주세요.')
    expected = _base(fields[0], fields[1], fields[2])
    if expected is None:
        raise SourceDataError('대상 행의 배송일·행사시간·보내는 분 요약을 먼저 확인해 주세요.')
    records = {target_row: fields}
    for offset, schedule in enumerate(schedules[1:], 1):
        row = offset + 4
        if row == target_row:
            continue
        place, sender = _cell(places, offset), _cell(senders, offset)
        if _base(schedule, place, sender) != expected:
            continue
        records[row] = [schedule, place, sender, _cell(markers, offset)]
        if len(records) > MAX_CANDIDATES + 1:
            raise SourceDataError('동일 조건 후보가 5,000행을 넘어 점검을 시작하지 않았어요. 범위를 나눠 주세요.')
    material = {'target': target_row, 'rows': sorted(records.items()), 'version': _version(resolver_factory)}
    try:
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise SourceDataError('원장 값의 형식을 확인하지 못해 점검을 보류했어요.') from None
    return records, hashlib.sha256(encoded.encode()).hexdigest()


def _context(connection, owner, job_id):
    progress = queue.progress(connection, owner, job_id)
    row = connection.execute('''SELECT target_row,candidate_count,halted_reason
        FROM duplicate_batch_context WHERE job=? AND owner=?''', (job_id, owner)).fetchone()
    if row is None:
        raise queue.NotFound('이 직원의 분할 점검 작업을 확인할 수 없어요.')
    return progress, {'target_row': row[0], 'candidate_count': row[1], 'halted_reason': row[2]}


def _show(connection, owner, job_id):
    state, details = _context(connection, owner, job_id)
    state.update(details)
    state['results'] = {category: [item['rows'] for item in queue.get_results(
        connection, owner, job_id, category=category, limit=8)] for category in queue.CATEGORIES}
    statuses = {'queued': '점검 대기', 'running': '장소 대조 중', 'completed': '분할 점검 완료',
                'canceled': '점검 취소', 'failed': '재시도 한도 초과·점검 미완료'}
    status = '자료가 바뀌어 점검 중지' if state['halted_reason'] == 'snapshot_changed' else statuses[state['status']]
    counts = state['result_counts']
    lines = [f'{status} · 기준 원장 {state["target_row"]}행',
             f'같은 날짜·행사시간·보내는 분 요약 후보 {state["candidate_count"]}행',
             f'점검행 {state["checked_rows"]}/{state["total_rows"]} · 기준 행 포함',
             f'중복 의심 {counts["duplicate"]}건 · 장소 확인 필요 {counts["review"]}건 · 다른 장소 {counts["different"]}건']
    for category, title in (('duplicate', '중복 의심'), ('review', '장소 확인 필요')):
        if state['results'][category]:
            pairs = ', '.join(f'{a}행·{b}행' for a, b in state['results'][category])
            lines.append(title + ' · ' + pairs + (' 외 추가 결과 있음' if counts[category] > 8 else ''))
    if state['status'] != 'completed':
        lines.append('전체 대조 완료가 아니에요. 현재 결과는 확인한 부분만 표시해요.')
    if counts['review']:
        lines.append('장소 근거가 부족한 행을 중복 없음으로 처리하지 않았어요.')
    state['summary'] = '\n'.join(lines)
    return state


def start(db_factory, owner, target_row, read_ranges, resolver_factory=None):
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 512:
        raise queue.NotFound('직원 계정을 확인해 주세요.')
    records, snapshot = _read(target_row, read_ranges, resolver_factory)
    if len(records) == 1:
        return {'job_id': None, 'status': 'completed', 'target_row': target_row, 'candidate_count': 0,
                'total_rows': 0, 'checked_rows': 0, 'cursor': 0,
                'result_counts': dict.fromkeys(queue.CATEGORIES, 0),
                'results': {category: [] for category in queue.CATEGORIES},
                'summary': f'원장 {target_row}행과 같은 배송일·행사시간·보내는 분 요약의 다른 행이 없어요. 실제 장소 대조는 실행하지 않았어요.'}
    with contextlib.closing(db_factory()) as connection:
        _setup(connection)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            job_id = queue.enqueue(connection, owner, snapshot, sorted(records), chunk_size=CHUNK_SIZE)
            connection.execute('''INSERT OR IGNORE INTO duplicate_batch_context
                (job,owner,target_row,candidate_count) VALUES (?,?,?,?)''', (job_id, owner, target_row, len(records) - 1))
        return _show(connection, owner, job_id)


def _fresh(connection, owner, job_id, target_row, read_ranges, resolver_factory, expected):
    records, snapshot = _read(target_row, read_ranges, resolver_factory)
    if snapshot != expected:
        with connection:
            queue.cancel(connection, owner, job_id)
            connection.execute('''UPDATE duplicate_batch_context SET halted_reason='snapshot_changed'
                WHERE job=? AND owner=?''', (job_id, owner))
        raise queue.SnapshotChanged('원장 또는 장소 매핑이 바뀌어 기존 점검을 중지했어요. 최신 자료로 새 점검을 시작해 주세요.')
    return records, snapshot


def process_next(db_factory, owner, job_id, read_ranges, resolver_factory=None):
    with contextlib.closing(db_factory()) as connection:
        _setup(connection)
        state, details = _context(connection, owner, job_id)
        if state['status'] not in ('queued', 'running'):
            return _show(connection, owner, job_id)
        target_row = details['target_row']
        records, snapshot = _fresh(connection, owner, job_id, target_row, read_ranges, resolver_factory,
                                   state['snapshot_hash'])
        lease = queue.claim(connection, owner, job_id, snapshot, lease_seconds=120)
        if lease is None:
            return _show(connection, owner, job_id)
        try:
            resolver = resolver_factory() if resolver_factory is not None else None
        except Exception:
            resolver = None
        results = {category: [] for category in queue.CATEGORIES}
        for row in lease['row_numbers']:
            if row == target_row:
                continue
            outcome = order_locations.compare_locations(records[target_row][1], records[row][1], resolver=resolver)
            category = {'same': 'duplicate', 'different': 'different', 'needs_review': 'review'}.get(outcome.get('status'), 'review')
            results[category].append([target_row, row])
        # Re-read after potentially slow provider calls; never publish a changed row snapshot.
        _, snapshot = _fresh(connection, owner, job_id, target_row, read_ranges, resolver_factory,
                             state['snapshot_hash'])
        queue.submit(connection, owner, job_id, lease['chunk_index'], lease['lease_token'], snapshot,
                     lease['row_numbers'], results)
        return _show(connection, owner, job_id)


def show(db_factory, owner, job_id):
    with contextlib.closing(db_factory()) as connection:
        _setup(connection)
        return _show(connection, owner, job_id)


def cancel(db_factory, owner, job_id):
    with contextlib.closing(db_factory()) as connection:
        _setup(connection)
        _context(connection, owner, job_id)
        queue.cancel(connection, owner, job_id)
        return _show(connection, owner, job_id)
