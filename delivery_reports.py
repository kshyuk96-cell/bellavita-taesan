"""Render delivery drafts and authorize temporary report URLs."""
import hmac
import re
import time
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path


LINK = re.compile(r'/route-report/([a-f0-9]{12})/(summary\.png|delivery\.xlsx|pending\.png|status\.xlsx)/([0-9]{10})/([a-f0-9]{64})')
LIFETIME = 3600


def url(base, secret, job, filename):
    path = f'/route-report/{job}/{filename}/{int(time.time()) + (300 if filename in ('pending.png', 'status.xlsx') else LIFETIME)}'
    return f'{base}{path}/{hmac.digest(secret.encode(), path.encode(), "sha256").hex()}'


def authorized(path, secret):
    match = LINK.fullmatch(path)
    if not match or not secret or int(match[3]) <= time.time():
        return False
    expected = hmac.digest(secret.encode(), path.rsplit('/', 1)[0].encode(), 'sha256').hex()
    return hmac.compare_digest(match[4], expected)


def counts(day):
    return sum(len(visit['orders']) for route in day['routes'] for visit in route['visits']), len(day['missed'])


def clock(minutes):
    return '' if minutes is None else f'{minutes // 60:02d}:{minutes % 60:02d}'


def excel(days):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    summary = book.active
    summary.title = '배정 요약'
    schedule = book.create_sheet('배송원별 일정')
    missed = book.create_sheet('미배정')
    sheets = (
        (summary, ('배송일', '출고센터', '배정 건수', '미배정 건수', '투입 인원', '기존 일정으로 보류', '안내'),
         (17, 14, 14, 14, 14, 21, 60)),
        (schedule, ('배송일', '배송원', '방문 순서', '주문번호', '행사시간', '설치 완료 예상', '배송장소',
                    '방문 설치 수량', '방문 직전 운반 수량', '센터 재픽업'), (17, 18, 13, 18, 14, 18, 51, 18, 23, 17)),
        (missed, ('배송일', '주문번호', '미배정 사유'), (17, 17, 55)),
    )
    for sheet, headers, widths in sheets:
        sheet.append(headers)
        sheet.freeze_panes = 'A2'
        for i, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(i)].width = width
        for cell in sheet[1]:
            cell.fill = PatternFill('solid', fgColor='193D36')
            cell.font = Font(bold=True, color='FFFFFF')
            cell.alignment = Alignment(vertical='center')
        sheet.row_dimensions[1].height = 24
    for day, draft in days.items():
        assigned, unassigned = counts(draft)
        summary.append((day, '구로센터', assigned, unassigned, len(draft['routes']), draft['held'],
                        '거리 기반 추정 시간이며 운영 시트에 반영되지 않은 배정안입니다.'))
        for worker in draft['routes']:
            for i, visit in enumerate(worker['visits'], 1):
                schedule.append((day, worker['name'], i, ', '.join(visit['orders']), clock(visit['event']),
                                 clock(visit['done']), visit['address'], visit['quantity'], visit['carried'],
                                 '있음' if visit['repickup'] else ''))
        for item in draft['missed']:
            missed.append((day, item['order'], item['reason']))
    for sheet, _, _ in sheets:
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = 's'  # Keep even formula-looking source cells as literal text.
        sheet.auto_filter.ref = sheet.dimensions
    for sheet, column in ((summary, 7), (schedule, 7), (missed, 3)):
        for (cell,) in sheet.iter_rows(min_row=2, min_col=column, max_col=column):
            cell.alignment = Alignment(wrap_text=True, vertical='top')
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def image(days):
    from PIL import Image, ImageDraw, ImageFont

    package = find_spec('koreanize_matplotlib')
    fonts = Path(next(iter(package.submodule_search_locations))) / 'fonts'
    title = ImageFont.truetype(str(fonts / 'NanumGothicBold.ttf'), 48)
    strong = ImageFont.truetype(str(fonts / 'NanumGothicBold.ttf'), 36)
    regular = ImageFont.truetype(str(fonts / 'NanumGothic.ttf'), 30)
    small = ImageFont.truetype(str(fonts / 'NanumGothic.ttf'), 24)
    canvas = Image.new('RGB', (720, 202 + len(days) * 146), '#EEF4EF')
    draw = ImageDraw.Draw(canvas)
    draw.text((42, 32), '구로센터 배송루트', font=title, fill='#193D36')
    draw.text((44, 94), '검토용 배정안', font=regular, fill='#385850')
    for i, (day, draft) in enumerate(days.items()):
        y = 145 + i * 146
        assigned, missed = counts(draft)
        draw.rounded_rectangle((36, y, 684, y + 130), radius=18, fill='#FFFFFF')
        draw.text((60, y + 20), f'{int(day[5:7])}월 {int(day[8:])}일', font=strong, fill='#193D36')
        draw.text((60, y + 76), f'배정 {assigned}건   미배정 {missed}건   배송원 {len(draft["routes"])}명',
                  font=regular, fill='#193D36')
    draw.text((44, canvas.height - 40), '배송원별 일정과 미배정 사유는 엑셀에서 확인하세요.', font=small, fill='#385850')
    output = BytesIO()
    canvas.save(output, format='PNG', optimize=True)
    return output.getvalue()
