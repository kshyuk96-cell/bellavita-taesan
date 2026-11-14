"""Find possible duplicates using only the relevant ledger columns."""
import re
import unicodedata
from datetime import date

from delivery_routes import minute, value
from order_ledger import EPOCH


REQUEST = re.compile(
    r'(?:주문\s*)?(?P<first>[0-9]{1,7})\s*번(?:\s*주문)?(?:이|은|는)?\s*'
    r'중복(?:\s*주문)?(?:인지|이야|인가요)?(?:\s*확인(?:\s*해\s*(?:줘|주세요))?)?[.!?]?'
    r'|(?:주문\s*)?중복\s*확인\s*(?:주문\s*)?(?P<last>[0-9]{1,7})\s*번?[.!?]?'
)
RANGES = ("'액자화환'!B4:C", "'액자화환'!F4:F", "'액자화환'!Q4:Q")
CELL_ERRORS = {'#n/a', '#ref!', '#value!', '#div/0!', '#num!', '#name?', '#null!', '#error!', '#spill!'}


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split()) if isinstance(value, str) else ''


def key(schedule, place, sender):
    day = value(schedule, 0)
    if isinstance(day, str):
        try:
            day = (date.fromisoformat(day.strip()) - EPOCH).days
        except ValueError:
            return None
    if type(day) not in (int, float) or not 0 < day <= (date.max - EPOCH).days or int(day) != day:
        return None
    time = minute(value(schedule, 1))
    place, sender = normalized(value(place, 0)), normalized(value(sender, 0))
    if time is None or time >= 1440 or not place or not sender or {place, sender} & CELL_ERRORS:
        return None
    return day, time, place, sender


def check(number, read_ranges):
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
    matches = [index + 4 for index, schedule in enumerate(schedules[1:], 1)
               if index != target and key(schedule, value(places, index), value(senders, index)) == expected]
    if not matches:
        return f'{number}번과 배송일·행사시간·설치장소·보내는 분 요약이 모두 같은 주문은 없어요.'
    examples = ', '.join(f'{row}번' for row in matches[:8])
    extra = f' 외 {len(matches) - 8}건' if len(matches) > 8 else ''
    return (f'중복 의심 · {number}번과 같은 주문 {len(matches)}건\n'
            f'원장 {examples}{extra}의 배송일·행사시간·설치장소·보내는 분 요약이 같아요. '
            '같은 행사에 따로 들어온 주문일 수 있으니 원장에서 확인해 주세요.')
