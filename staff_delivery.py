"""Read delivery rows once for reports, follow-ups and imminent-installation checks."""
import re
from collections import Counter
from io import BytesIO

from delivery_counts import ALL_CENTERS
from delivery_routes import EPOCH, minute, value

COLUMNS = ('주문번호', '배송일', '행사시간', '배송주소', '배송채널', '배송 완료', '배송원')
RANGES = tuple("'배송관리'!" + pos for pos in ('A3:D', 'G3:G', 'K3:K', 'O3:O'))


def rows_for(day, center, read):
    primary, channels, statuses, workers = read(RANGES)
    headers = tuple(cell for group in (primary, channels, statuses, workers) for cell in value(group, 0))
    if headers != COLUMNS:
        raise ValueError('배송관리 열 제목을 확인해 주세요.')
    key = re.sub(r'\s+', '', center or '')
    if key not in ALL_CENTERS and not any(key in re.sub(r'\s+', '', str(value(row, 0))) for row in channels[1:]):
        raise ValueError('배송센터 이름을 확인해 주세요.')
    serial = (day - EPOCH).days
    rows = []
    for i, row in enumerate(primary[1:], 1):
        number = str(value(row, 0))
        channel = str(value(value(channels, i), 0))
        if not re.fullmatch(r'[1-9][0-9]{0,6}', number) or value(row, 1) != serial:
            continue
        if key not in ALL_CENTERS and key not in re.sub(r'\s+', '', channel):
            continue
        checked = value(value(statuses, i), 0)
        rows.append({'number': number, 'event': minute(value(row, 2)), 'place': str(value(row, 3))[:180],
                     'center': channel[:48], 'worker': str(value(value(workers, i), 0))[:48],
                     'checked': checked if type(checked) is bool else None})
    counts = Counter(row['number'] for row in rows)
    for row in rows:
        if counts[row['number']] > 1:
            row['checked'] = None
    return list({row['number']: row for row in rows}.values())


def status(row):
    return {True: '완료', False: '미완료', None: '확인 필요'}[row['checked']]


def clock(value):
    return f'{value // 60:02d}:{value % 60:02d}' if value is not None and 0 <= value < 1440 else '시간 확인'


def excel(report):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    book = Workbook()
    sheet = book.active
    sheet.title = '배송 현황'
    sheet.append(('배송일', '주문번호', '행사시간', '장소', '센터', '담당자', '배송상태'))
    for row in report['rows']:
        sheet.append((report['day'], row['number'], clock(row['event']), row['place'],
                      row['center'], row['worker'] or '미배정', status(row)))
    for col, width in zip('ABCDEFG', (14, 12, 14, 42, 18, 16, 15)):
        sheet.column_dimensions[col].width = width
    for cell in sheet[1]:
        cell.fill = PatternFill('solid', fgColor='193D36')
        cell.font = Font(color='FFFFFF', bold=True)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = 's'
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def image(report):
    from importlib.util import find_spec
    from pathlib import Path
    from PIL import Image, ImageDraw, ImageFont
    fonts = Path(next(iter(find_spec('koreanize_matplotlib').submodule_search_locations))) / 'fonts'
    bold = lambda size: ImageFont.truetype(str(fonts / 'NanumGothicBold.ttf'), size)
    pending = [row for row in report['rows'] if row['checked'] is False]
    pending.sort(key=lambda row: row['event'] if row['event'] is not None else 1440)
    shown = pending[:15]
    canvas = Image.new('RGB', (760, 155 + 88 * len(shown)), '#F0F5F1')
    draw = ImageDraw.Draw(canvas)
    draw.text((34, 24), f'배송 미완료 {len(pending)}건', font=bold(36), fill='#193D36')
    draw.text((36, 78), f'{report["day"]} · {report["center"]}', font=bold(23), fill='#365C52')
    if len(shown) < len(pending):
        draw.text((36, 111), '이미지는 15건까지 표시 · 전체는 엑셀에서 확인', font=bold(20), fill='#365C52')
    for i, row in enumerate(shown):
        y = 143 + i * 88
        draw.rounded_rectangle((30, y, 730, y + 80), radius=12, fill='#FFFFFF')
        draw.text((47, y + 11), f'{clock(row["event"])} · {row["number"]}번 · {row["worker"] or "미배정"}'[:38],
                  font=bold(26), fill='#193D36')
        place = row['place'] or '장소 확인'
        font = bold(23)
        abbreviated = len(place) > 160
        place = place[:160]
        while draw.textlength(place + '…', font=font) > 652:
            place = place[:-1]
            abbreviated = True
        draw.text((47, y + 46), place + ('…' if abbreviated else ''), font=font, fill='#365C52')
    output = BytesIO()
    canvas.save(output, format='PNG')
    return output.getvalue()


def imminent(rows, minute_now):
    """Warn from 30 minutes before the installation deadline (one hour before the event)."""
    return [row for row in rows if row['checked'] is False and row['event'] is not None
            and 0 <= row['event'] < 1440 and 0 <= row['event'] - minute_now <= 90]


REPORT = re.compile(r'(?:배송\s*현황\s*(?:엑셀|내려받기)|(?:배송\s*)?미\s*완료\s*(?:목록|이미지|사진))')
FOLLOW = re.compile(r'(?:그럼\s*)?(?:(오늘|내일|모레)(?:은|는|도)?|([가-힣]{2,10}?)(?:\s*센터)?\s*만|전체(?:\s*센터)?)[?!.]?')


def request(message):
    match = REPORT.search(message)
    if not match or re.search(r'개발|기능|수정|삭제|취소|지난|어제|다음\s*주|\d+\s*(?:월|일)', message):
        return None
    rest = message[:match.start()].strip()
    from delivery_counts import CENTER, DAYS
    days = [i for i, day in enumerate(DAYS) if day in rest]
    centers = [m[1] for m in CENTER.finditer(re.sub(r'오늘|내일|모레', '', rest))
               if m[1] not in ('전체', '모든')]
    if len(days) > 1 or len(centers) > 1:
        return None
    leftover = CENTER.sub('', re.sub(r'오늘|내일|모레|전체|모든|그럼', '', rest)).strip()
    if leftover:
        return None
    return days[0] if days else 0, centers[0] if centers else None


def enqueue_imminent(connection, rows, now):
    from owner_alerts import enqueue
    for row in imminent(rows, now.hour * 60 + now.minute):
        enqueue(connection, f'delivery-imminent:{now.date()}:{row["number"]}',
                f'행사 임박 · {row["number"]}번 배송 미완료\n행사 {clock(row["event"])} · 배송관리에서 확인해 주세요.')


def start_alerts(db, read, now, sleep, make_thread, enabled):
    if not enabled:
        return

    def run():
        while True:
            try:
                at = now()
                rows = rows_for(at.date(), None, read)
                with db() as connection:
                    enqueue_imminent(connection, rows, at)
            except Exception as error:
                print('행사 임박 확인 실패:', type(error).__name__, flush=True)
            sleep(60)
    make_thread(target=run, daemon=True).start()
