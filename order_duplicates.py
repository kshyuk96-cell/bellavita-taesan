"""Find possible duplicates using only the relevant ledger columns."""
import re
import unicodedata
import math
from datetime import date

from delivery_routes import value
from order_ledger import EPOCH
from order_locations import compare_locations


REQUEST = re.compile(
    r'(?:주문\s*)?(?P<first>[0-9]{1,7})\s*번(?:\s*주문)?(?:이|은|는)?\s*'
    r'중복(?:\s*주문)?(?:인지|이야|인가요)?(?:\s*확인(?:\s*해\s*(?:줘|주세요))?)?[.!?]?'
    r'|(?:주문\s*)?중복\s*확인\s*(?:주문\s*)?(?P<last>[0-9]{1,7})\s*번?[.!?]?'
)
RANGES = ("'액자화환'!B4:C", "'액자화환'!F4:F", "'액자화환'!Q4:Q")
CELL_ERRORS = {'#n/a', '#ref!', '#value!', '#div/0!', '#num!', '#name?', '#null!', '#error!', '#spill!'}


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split()) if isinstance(value, str) else ''


def clock_seconds(value):
    """Preserve seconds; rounding to minutes can merge different event times."""
    if type(value) in (int, float):
        seconds = round(value * 86400) if math.isfinite(value) and 0 <= value < 1 else -1
        return seconds if 0 <= seconds < 86400 else None
    match = re.fullmatch(r'(?:(오전|오후)\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?', normalized(value))
    if not match:
        return None
    period, hour, minute, second = match.groups()
    hour, minute, second = int(hour), int(minute), int(second or 0)
    if minute > 59 or second > 59 or hour > 23 or (period and not 1 <= hour <= 12):
        return None
    if period:
        hour = hour % 12 + (12 if period == '오후' else 0)
    return hour * 3600 + minute * 60 + second


def key(schedule, place, sender):
    day = value(schedule, 0)
    if isinstance(day, str):
        try:
            day = (date.fromisoformat(day.strip()) - EPOCH).days
        except ValueError:
            return None
    if type(day) not in (int, float) or not 0 < day <= (date.max - EPOCH).days or int(day) != day:
        return None
    time = clock_seconds(value(schedule, 1))
    place, sender = normalized(value(place, 0)), normalized(value(sender, 0))
    if time is None or not place or not sender or {place, sender} & CELL_ERRORS:
        return None
    return day, time, place, sender


def check(number, read_ranges, *, resolver=None):
    if number < 5:
        return '액자화환 원장에 적힌 주문번호를 알려주세요.'
    schedules, places, senders = read_ranges(RANGES)
    headers = [value(value(rows, 0), i) for rows, i in ((schedules, 0), (schedules, 1), (places, 0), (senders, 0))]
    if [normalized(header).replace(' ', '') for header in headers] != ['배송일', '행사시간', '설치장소', '보내는분요약']:
        return '액자화환 원장 제목이 달라 중복을 확인하지 못했어요.'
    target = number - 4
    fields = (value(schedules, target), value(places, target), value(senders, target))
    if not any(fields):
        return f'액자화환 원장에서 {number}번 주문을 찾지 못했어요. 현재 주문번호를 확인해 주세요.'
    expected = key(*fields)
    if expected is None:
        return f'{number}번 주문의 배송일, 행사시간, 설치장소, 보내는 분 요약을 확인해 주세요. 값이 비었거나 읽을 수 없어요.'
    matches, uncertain = [], []
    candidates = []
    for index, schedule in enumerate(schedules[1:], 1):
        if index == target:
            continue
        candidate = key(schedule, value(places, index), value(senders, index))
        if candidate is None:
            # A missing/invalid place cannot prove a different location.
            candidate = key(schedule, ['__location_missing__'], value(senders, index))
        if candidate and (candidate[0], candidate[1], candidate[3]) == (expected[0], expected[1], expected[3]):
            candidates.append(index)
    if len(candidates) > 40:
        return '동일 조건 주문이 많아 장소 대조를 보류했어요. 날짜·행사시간·보내는 분 요약을 원장에서 확인해 주세요.'
    for index in candidates:
        outcome = compare_locations(value(fields[1], 0), value(value(places, index), 0), resolver=resolver)
        if outcome['status'] == 'same':
            matches.append(index + 4)
        elif outcome['status'] == 'needs_review':
            uncertain.append(index + 4)
    lines = []
    def rows_text(rows):
        return ', '.join(f'{row}번' for row in rows[:8]) + (f' 외 {len(rows)-8}건' if len(rows) > 8 else '')
    if matches:
        lines.append(f'중복 의심 · {number}번과 같은 배송일·행사시간·실제 장소·보내는 분 DB 요약 {len(matches)}건\n원장 {rows_text(matches)}을 확인해 주세요.')
    if uncertain:
        lines.append(f'장소 확인 필요 · 원장 {rows_text(uncertain)}\n배송일·행사시간·보내는 분 요약은 같지만 주소 또는 층·홀을 하나로 확인하지 못했어요.')
    if not lines:
        lines.append(f'{number}번과 배송일·행사시간·실제 장소·보내는 분 DB 요약이 모두 같은 주문은 확인되지 않았어요.')
    return '\n'.join(lines)
