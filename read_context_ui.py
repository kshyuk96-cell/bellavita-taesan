"""Pure display and edit helpers for authenticated, successful read results.

This module never reads Sheets, calls models, authenticates owners, or executes a
query. Metadata is a scope/time description, not evidence that a read succeeded.
The caller adds it ONLY after a real successful read, loads the referenced job for
the authenticated owner, and enforces context expiry before applying an action.
"""
import math
import re
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo('Asia/Seoul')
KINDS = frozenset({'delivery_count', 'venue_rank', 'staff_report'})
MODES = {'complete': '완료', 'pending': '미완료', 'total': '전체'}
FIELDS = {'날짜': 'date', '센터': 'center', '상태': 'status'}
FIELD_LABELS = {value: key for key, value in FIELDS.items()}
_JOB = re.compile(r'[a-f0-9]{12}')
_ACTION = re.compile(r'조회조건 ([a-f0-9]{12}) (날짜|센터|상태) (선택|오늘|내일|모레|구로|수원|전체|complete|pending|total)')
_DAYS = {'오늘': 0, '내일': 1, '모레': 2}
_CENTERS = {'구로': '구로', '수원': '수원', '전체': None}


def _aware(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('시간대가 확인된 조회 시각이 필요해요.')
    return now.astimezone(SEOUL)


def _center(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('센터 조건을 확인해 주세요.')
    value = re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))
    if value in ('', '전체', '전체센터', '모든센터'):
        return None
    if not re.fullmatch(r'[가-힣A-Za-z0-9]{1,20}', value):
        raise ValueError('센터 조건을 확인해 주세요.')
    return value.removesuffix('센터') or None


def normalize_query(query):
    if not isinstance(query, (tuple, list)) or len(query) != 4:
        raise ValueError('조회 조건을 확인해 주세요.')
    kind, offset, center, mode = query
    if (not isinstance(kind, str) or kind not in KINDS or type(offset) is not int
            or not -7 <= offset <= 365):
        raise ValueError('지원하는 조회 조건을 확인해 주세요.')
    if kind == 'delivery_count':
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError('배송 상태를 확인해 주세요.')
    elif mode is not None:
        raise ValueError('이 조회에는 배송 상태 필터를 적용하지 않아요.')
    return kind, offset, _center(center), mode


def _source(kind, mode):
    if kind == 'venue_rank':
        return '원장 배송일·설치장소'
    if kind == 'staff_report':
        return '배송관리 완료 체크'
    return '원장 배송일' if mode == 'total' else '원장·배송관리 완료 체크'


def build_metadata(query, *, now, read_context_at=None):
    """Describe an already successful read; never infer success from summary text."""
    kind, offset, center, mode = normalize_query(query)
    now = _aware(now)
    if read_context_at is None:
        context_at = now.timestamp()
    elif isinstance(read_context_at, datetime):
        context_at = _aware(read_context_at).timestamp()
    elif type(read_context_at) in (int, float) and math.isfinite(read_context_at):
        context_at = float(read_context_at)
    else:
        raise ValueError('조회 맥락 시각을 확인해 주세요.')
    if not 0 <= context_at <= now.timestamp():
        raise ValueError('조회 맥락 시각을 확인해 주세요.')
    return dict(kind=kind, resolved_day=(now.date() + timedelta(days=offset)).isoformat(),
                center=center, mode=mode, read_at=now.isoformat(timespec='seconds'),
                source=_source(kind, mode), read_context_at=context_at)


def _metadata(metadata):
    if not isinstance(metadata, dict):
        raise ValueError('조회 근거 정보를 확인해 주세요.')
    try:
        kind, _, center, mode = normalize_query([metadata['kind'], 0, metadata['center'], metadata['mode']])
        resolved = date.fromisoformat(metadata['resolved_day'])
        read_at = _aware(datetime.fromisoformat(metadata['read_at']))
        context_at = metadata['read_context_at']
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('조회 근거 정보를 확인해 주세요.') from None
    if (resolved.isoformat() != metadata['resolved_day'] or metadata.get('source') != _source(kind, mode)
            or type(context_at) not in (int, float) or not math.isfinite(context_at)
            or not 0 <= context_at <= read_at.timestamp() + 1):
        raise ValueError('조회 근거 정보를 확인해 주세요.')
    return kind, center, mode, resolved, read_at


def format_metadata(metadata):
    kind, center, mode, resolved, read_at = _metadata(metadata)
    status = MODES[mode] if kind == 'delivery_count' else '설치장소 집계' if kind == 'venue_rank' else '배송 현황 전체'
    return (f'기준 {resolved:%m/%d} · {center or "전체 센터"} · {status}\n'
            f'조회 {read_at:%m/%d %H:%M:%S} KST · {metadata["source"]}')


def preview(query, metadata):
    kind, offset, center, mode = normalize_query(query)
    actual_kind, actual_center, actual_mode, resolved, read_at = _metadata(metadata)
    if ((kind, center, mode) != (actual_kind, actual_center, actual_mode)
            or read_at.date() + timedelta(days=offset) != resolved):
        raise ValueError('원래 조회와 근거 정보가 달라요. 다시 조회해 주세요.')
    return format_metadata(metadata)


def _job(job_id):
    if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
        raise ValueError('조회 결과를 다시 선택해 주세요.')


def _field(field):
    field = FIELDS.get(field, field) if isinstance(field, str) else None
    if field not in FIELD_LABELS:
        raise ValueError('변경할 조회 조건을 확인해 주세요.')
    return field


def buttons_for(job_id, query):
    _job(job_id)
    kind, *_ = normalize_query(query)
    fields = ['date', 'center'] + (['status'] if kind == 'delivery_count' else [])
    return [(FIELD_LABELS[field] + ' 바꾸기', f'조회조건 {job_id} {FIELD_LABELS[field]} 선택') for field in fields]


def options_for(job_id, query, field):
    _job(job_id)
    kind, *_ = normalize_query(query)
    field = _field(field)
    if field == 'status' and kind != 'delivery_count':
        raise ValueError('이 조회에는 배송 상태 필터를 적용하지 않아요.')
    options = [(day, day) for day in _DAYS] if field == 'date' else (
        [(center, center) for center in _CENTERS] if field == 'center' else
        [(label, value) for value, label in MODES.items()])
    return [(label, f'조회조건 {job_id} {FIELD_LABELS[field]} {value}') for label, value in options]


def parse_action(message):
    if not isinstance(message, str) or not message.startswith('조회조건'):
        return None
    match = _ACTION.fullmatch(message)
    if not match:
        raise ValueError('조회 조건 버튼을 다시 눌러 주세요.')
    job, field, value = match.groups()
    field = FIELDS[field]
    allowed = _DAYS if field == 'date' else _CENTERS if field == 'center' else MODES
    if value != '선택' and value not in allowed:
        raise ValueError('조회 조건 버튼을 다시 눌러 주세요.')
    return {'job': job, 'field': field, 'value': value}


def apply_change(query, metadata, field, value, now):
    """Return (new query tuple, actual day delta), preserving absolute day.

    TTL/ownership checks happen in the caller before this helper. It never starts
    the returned query and cannot authorize a server action.
    """
    preview(query, metadata)
    kind, _, center, mode = normalize_query(query)
    resolved = date.fromisoformat(metadata['resolved_day'])
    now = _aware(now)
    field = _field(field)
    if field == 'date':
        if not isinstance(value, str) or value not in _DAYS:
            raise ValueError('조회 날짜를 확인해 주세요.')
        resolved = now.date() + timedelta(days=_DAYS[value])
    elif field == 'center':
        if not isinstance(value, str) or value not in _CENTERS:
            raise ValueError('조회 센터를 확인해 주세요.')
        center = _CENTERS[value]
    else:
        if kind != 'delivery_count' or not isinstance(value, str) or value not in MODES:
            raise ValueError('지원하는 배송 상태 필터를 확인해 주세요.')
        mode = value
    delta = (resolved - now.date()).days
    return normalize_query((kind, delta, center, mode)), delta
