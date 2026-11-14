"""Deterministic, private JPEG proof from the saved order, with no remote fetches."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

from PIL import Image, ImageDraw, ImageFont


def font_path():
    path = Path(os.getenv('SHOP_PREVIEW_FONT', str(Path(__file__).with_name('assets') / 'fonts' / 'NotoSansKR.ttf')))
    if not path.is_file():
        raise ValueError('SHOP_PREVIEW_FONT에 한글 글꼴 경로를 설정해 주세요.')
    return path


def lines(draw, text, font, width):
    result = []
    for paragraph in text.splitlines() or ['']:
        current = ''
        for char in paragraph:
            if current and draw.textlength(current + char, font=font) > width:
                result.append(current)
                current = ''
            current += char
        result.append(current)
    return result


def fit(draw, text, box, max_size=42):
    left, top, right, bottom = box
    if len(text.splitlines()) > 15:
        text = ' '.join(text.split())
    for size in range(max_size, 5, -1):
        font = ImageFont.truetype(str(font_path()), size)
        try:
            font.set_variation_by_axes([500])
        except (OSError, ValueError):
            pass  # A configured static Korean font has no variation axis.
        rows = lines(draw, text, font, right-left-12)
        step = int(size * 1.5)
        if len(rows) * step <= bottom-top-12:
            break
    else:
        raise ValueError('문구가 샘플 이미지 영역을 초과했어요.')
    y = top + (bottom-top-len(rows)*step)/2
    for row in rows:
        draw.text(((left+right)/2, y), row, font=font, fill='#24382d', anchor='mt')
        y += step


def render(order_id, data):
    image = Image.new('RGB', (900, 1200), '#f5f2ea')
    draw = ImageDraw.Draw(image)
    fit(draw, '벨라비타 · 주문 문구 확인', (30, 15, 870, 100), 32)
    # A clean proof contains only saved order text, without preprinted artwork
    # captions. No customer-controlled URL is fetched by the image renderer.
    draw.rounded_rectangle((50, 130, 850, 830), radius=14, fill='#fffefa', outline='#b9ab8e', width=2)
    choices = dict(data['options'])
    greeting = data['message'] or choices.get('축하 문구', '')
    fit(draw, choices.get('상품 선택', '쌀빛그림화환'), (100, 160, 800, 235), 32)
    fit(draw, greeting, (110, 250, 790, 570), 46)
    fit(draw, data['sender'], (110, 575, 790, 790), 38)
    fit(draw, data['title'], (40, 850, 860, 925), 25)
    fit(draw, data['venue'] + '\n' + data['address'], (40, 930, 860, 1030), 23)
    fit(draw, data['event_time'][:16].replace('T', ' ') + '\n주문번호 ' + order_id,
        (40, 1035, 860, 1115), 20)
    fit(draw, '입력 문구 확인용 샘플 · 실제 설치 사진이 아닙니다', (30, 1125, 870, 1185), 20)
    for quality in (88, 78, 65, 50):
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=quality, optimize=True)
        if len(output.getvalue()) <= 200_000:
            return output.getvalue()
    raise ValueError('MMS 이미지 용량을 확인해 주세요.')


def sample(root, row):
    data = json.loads(row['details'])
    digest = hashlib.sha256(('proof-v2:' + row['details']).encode()).hexdigest()[:16]
    directory = Path(root) / 'shop-previews'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f'{row["id"]}-{digest}.jpg'
    if not path.exists():
        content = render(row['id'], data)
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
            output.write(content)
            temporary = Path(output.name)
        temporary.replace(path)
    return path
