"""Read-only delivery counts for the ledger and the dispatch board."""
import re
from read_resilience import SchemaReadError

from delivery_routes import EPOCH, value


DAYS = ('오늘', '내일', '모레')
OPERATIONS_SHEET_ID = '16d6lM1o4C45Hy7beIyp2eqrNEHjNW0FhGA4Fx-DVK2s'
ALL_CENTERS = {'', '전체', '전체센터', '모든센터'}
COUNT = re.compile(r'몇|건수|얼마나|수량|총\s*건')
PENDING = re.compile(r'미\s*(?:완료|처리)|안\s*(?:됐|된|한|끝)|'
                     r'(?:체크|완료|처리)(?:\s*(?:가|이|를|을|되지|하지))?\s*(?:안|않|못)|'
                     r'남(?:은|았)|아직|체크\s*전|끝나지\s*않')
COMPLETE = re.compile(r'배송\s*완료|완료\s*(?:체크|된|처리|건|했|한)|체크\s*(?:된|한)|끝난')
TOTAL = re.compile(r'전체|모든\s*센터|총\s*(?:몇|건|배송|수량)|합계|전부')
CENTER = re.compile(r'([가-힣]{2,10})\s*센터(?:에서|의|는|만|에)?')
REPLACEMENT = re.compile(r'말고|대신')
EXCLUDED = re.compile(r'개발|기능|수정|바꿔|버그|개선|늘려|줄여|체크해|처리해|완료해|지난|어제|이번\s*주|다음\s*주|'
                      r'\d+\s*(?:월|일)|[월화수목금토일]요일|택배|재고|제작|미배정|이미지|엑셀|보고서|루트')
SHORT_STATUS = re.compile(r'(?:배송\s*)?(?:미\s*완료|완료|남은\s*건)(?:만|도)?[.!?]?')
CONTEXT_STATUS = re.compile(r'(?:그럼\s*)?(?:배송\s*)?(미\s*완료|완료|남은\s*건|전체)(?:\s*건)?\s*(?:만|도)[.!?]?')
COUNT_CHALLENGE = re.compile(r'(?:뭔|무슨|왜|어떻게)\s*\d+\s*(?:건|개)|이렇게\s*밖에.*남')
PHOTO_MESSAGE = re.compile(r'(?:\[\s*(?:사진|이미지)\s*\]|사진|이미지|사진을 보냈습니다\.?|이미지를 보냈습니다\.?)')
COUNT_BUTTONS = (
    ('오늘 배송 미완료', '오늘 배송 미완료 몇 건?'),
    ('오늘 배송 완료', '오늘 배송완료 몇 건?'),
    ('오늘 배송 전체', '오늘 전체 배송 몇 건?'),
)


def metric(message):
    before, *after = REPLACEMENT.split(message)
    if after and (PENDING.search(before) or COMPLETE.search(before) or TOTAL.search(before)):
        message = after[-1]
    if PENDING.search(message):
        return 'pending'
    if COMPLETE.search(message):
        return 'complete'
    return 'total' if TOTAL.search(message) else 'pending'


def request(message):
    if '배송' not in message or not COUNT.search(message) or EXCLUDED.search(message) or '\n' in message:
        return None
    days = [index for index, label in enumerate(DAYS) if label in message]
    if len(days) > 1:
        return None
    undated = re.sub(r'오늘|내일|모레', ' ', message)
    centers = [m[1] for m in CENTER.finditer(undated) if m[1] not in ('전체', '모든')]
    if len(centers) > 1:
        return None
    center = centers[0] if centers else None
    remainder = CENTER.sub('', undated)
    remainder = re.sub(r'전체|모든|배송|[.,!?]', '', remainder)
    if re.match(r'\s*(?!(?:중|몇|건|총|다|아직|안|체크|끝|남|완료|미완료|처리|말고|대신|은|는|이|가|을|를|확인|얼마|수량))\S+\s+', remainder):
        return None  # Let the existing language router interpret an unlabelled center or extra qualifier.
    return days[0] if days else 0, center, metric(message)


def follow_up(message):
    """Recognize a correction to today's delivery counts without carrying a prior answer."""
    if EXCLUDED.search(message) or ('\n' in message) or CENTER.search(message):
        return None
    if SHORT_STATUS.fullmatch(message) and '배송' in message:
        return 0, None, metric(message)
    if any(label in message for label in DAYS[1:]) or not PENDING.search(message):
        return None
    if re.search(r'미\s*완료|배송', message) and (COUNT.search(message) or COUNT_CHALLENGE.search(message)):
        return 0, None, 'pending'
    return None


def count(day, center, kind, read):
    if center is not None and not isinstance(center, str):
        raise ValueError('어느 센터 배송을 확인할까요?')
    center_key = re.sub(r'\s+', '', center or '')
    selected_center = center_key not in ALL_CENTERS
    ranges = ["'액자화환'!B4:B"]
    if selected_center:
        ranges.append("'액자화환'!I4:I")
    if kind != 'total':
        ranges.extend(("'배송관리'!A3:B", "'배송관리'!K3:K"))
    dates, *data = read(ranges)
    channels = data.pop(0) if selected_center else []
    if dates[:1] != [['배송일']] or (selected_center and channels[:1] != [['배송 채널']]):
        raise SchemaReadError('주문 원장의 배송 열 제목을 확인해 주세요.')
    if selected_center:
        keys = {re.sub(r'\s+', '', str(value(row, 0))) for row in channels[1:]}
        if not any(center_key in key for key in keys):
            raise ValueError('해당 배송센터를 찾지 못했어요. 센터 이름을 확인해 주세요.')
    serial = (day - EPOCH).days
    orders = {index + 4 for index, row in enumerate(dates[1:], 1) if value(row, 0) == serial
              and (not selected_center or center_key in re.sub(r'\s+', '', str(value(value(channels, index), 0))))}
    label = f'{day.month}월 {day.day}일 {center.strip() if selected_center else "전체"}'
    if kind == 'total':
        return f'{label} 배송은 {len(orders)}건이에요.'
    visible, flags = data
    if visible[:1] != [['주문번호', '배송일']] or flags[:1] != [['배송 완료']]:
        raise SchemaReadError('배송관리의 배송완료 열 제목을 확인해 주세요.')
    states = {}
    uncertain = set()
    for index, row in enumerate(visible[1:], 1):
        order = value(row, 0)
        if isinstance(order, str) and re.fullmatch(r'[0-9]{1,7}', order):
            order = int(order)
        if type(order) not in (int, float) or order not in orders or value(row, 1) != serial:
            continue
        state = value(value(flags, index), 0)
        if type(state) is not bool or order in states:
            uncertain.add(order)
        else:
            states[order] = state
    target = kind == 'complete'
    confirmed = sum(state is target for order, state in states.items() if order not in uncertain)
    status = '배송완료' if target else '배송 미완료'
    unknown = len((orders - states.keys()) | uncertain)
    if unknown:
        return f'{label} {status}로 확인된 건은 {confirmed}건이에요. {unknown}건은 배송관리에서 상태 확인이 필요해요.'
    return f'{label} {status}는 {confirmed}건이에요.'
