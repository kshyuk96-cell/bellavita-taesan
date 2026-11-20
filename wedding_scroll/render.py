"""Local rendering functions for the production site and Sheets integration."""
import io
import os
import re
from pathlib import Path
import fitz
from PIL import Image, ImageOps, ImageDraw
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from .sender import SENDER_TITLES, split_sender, canonical_sender_plan, SENDER_REFERENCE_NAME_SIZE

ASSETS = Path(os.environ.get('ERP_WEDDING_SCROLL_ASSETS', Path(__file__).resolve().parent / 'assets')).resolve()
FONT = ASSETS / 'seoul.ttf'
FONT_NAME = 'Seoul'
pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT)))
SENDER_REFERENCE_FONT = 'BellavitaSenderReference'
pdfmetrics.registerFont(TTFont(SENDER_REFERENCE_FONT, str(FONT)))

def read_image(path):
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert('RGB')

def catalog():
    return {int(p.name.split()[0]): p for p in sorted(
        (ASSETS / 'catalog').glob('*.jpg'), key=lambda p: int(p.name.split()[0]))}

def render_pages(doc):
    for page in doc:
        pix = page.get_pixmap(dpi=144)
        yield Image.frombytes('RGB', (pix.width, pix.height), pix.samples)

def stack(images):
    width = max(im.width for im in images)
    result = Image.new('RGB', (width, sum(im.height for im in images)), 'white')
    y = 0
    for im in images:
        result.paste(im, ((width-im.width)//2, y))
        y += im.height
    return result

def balanced_sender_parts(text, box, maximum, count, font_name=None):
    from itertools import combinations

    font_name = font_name or FONT_NAME
    text = ' '.join(text.split())
    word_cuts = [i for i, char in enumerate(text) if char == ' ']
    best = None
    for positions in (word_cuts, range(1, len(text))):
        if len(positions) > 100:
            continue
        for cuts in combinations(positions, count - 1):
            edges = (0, *cuts, len(text))
            lines = [text[a:b].strip() for a, b in zip(edges, edges[1:])]
            if any(not line or line in ('일동', '회장', '대표', '사장') for line in lines):
                continue
            widths = [pdfmetrics.stringWidth(line, font_name, 1) for line in lines]
            size = min(maximum, box.width/max(widths), box.height/(1.16*count))
            height = (1.16*(count-1) + .8)*size
            if min(widths)*size < (box.x0+box.x1)/3 or height < box.height/3:
                continue
            score = (sum(cut in word_cuts for cut in cuts), size, min(widths)/max(widths))
            if best is None or score > best[0]:
                best = score, lines
        if best:
            return best[1]
    return None


def short_name_tracking(name, row_width, page_width):
    """Use modest letter spacing to meet the minimum width for a two-syllable name."""
    if not re.fullmatch(r'[가-힣]{2}', name):
        return 0
    extra = page_width / 3 - row_width
    return extra if 0 < extra <= 8 else 0


def cohort_sender_layout(source, parsed, box, page_width):
    name = parsed['name']
    if not re.fullmatch(r'[가-힣]{2,4}', name):
        return None
    label = ' '.join(parsed.get('label', '').split())
    original = ' '.join(source.split())
    if original.endswith(' ' + name) and original[:-len(name)].replace(' ', '') == label.replace(' ', ''):
        label = original[:-len(name)].strip()
    match = re.fullmatch(
        r'(.+?)\s+((?:제\s*)?\d+\s*(?:회|기)\s+.+(?:회장|위원장|대표|이사장|고문|총무|회장단|부장|사무국장))',
        label,
    )
    if not match or re.search(r'(?:회장|고문|총무)\s+.+\s+(?:대표|이사장|사장)$', match[2]):
        return None
    name_size = min(175.5, box.width / pdfmetrics.stringWidth(name, FONT_NAME, 1))
    units = [pdfmetrics.stringWidth(line, FONT_NAME, 1) for line in match.groups()]
    label_size = min(90, box.width / max(units), (box.height / 1.075 - name_size) / 2)
    name_width = pdfmetrics.stringWidth(name, FONT_NAME, name_size)
    if min(min(units) * label_size, name_width + short_name_tracking(name, name_width, page_width)) < page_width / 3 - 1e-7:
        return None
    return [[(line, label_size)] for line in match.groups()] + [[(name, name_size)]]


def sender_a3_layout(label, name, box, page_width):
    """Fit a single sender with the approved A3 name and affiliation hierarchy."""
    roles = '|'.join(re.escape(role) for role in sorted(SENDER_TITLES, key=len, reverse=True))
    match = re.fullmatch(r'(.+?)\s+(' + roles + r')', ' '.join(label.split()))
    if not match or not re.fullmatch(r'[가-힣]{2,4}', name):
        return None
    organization, title = match.groups()
    words = organization.split()
    options = [([organization + ' ' + title], False), ([organization], True)]
    options.extend(([' '.join(words[:i]), ' '.join(words[i:])], True) for i in range(1, len(words)))
    candidates = []
    for labels, inline in options:
        units = [pdfmetrics.stringWidth(line, FONT_NAME, 1) for line in labels]
        baseline = min(195, box.height / (1.16 * (1 + sum(
            min(.48, box.width / (195 * unit)) for unit in units
        ))), box.width / pdfmetrics.stringWidth(name, FONT_NAME, 1))
        title_unit = pdfmetrics.stringWidth(title + ' ', FONT_NAME, 1) if inline else 0
        title_size = min((.68 if len(labels) == 1 else .56) * baseline,
                         box.width / max(units), (box.height / 1.075 - baseline) / len(labels))
        if inline:
            title_size = min(title_size, (box.width - pdfmetrics.stringWidth(name, FONT_NAME, baseline)) / title_unit)
        name_size = min(baseline, 175.5)
        label_size = min(title_size, 119.34)
        name_width = pdfmetrics.stringWidth(name, FONT_NAME, name_size)
        tracking = short_name_tracking(name, name_width, page_width) if not inline else 0
        if min(units) * label_size < page_width / 3:
            continue
        if name_width + title_unit * title_size + tracking + 1e-7 < page_width / 3:
            continue
        rows = [[(line, label_size)] for line in labels]
        rows.append(([(title + ' ', title_size)] if inline else []) + [(name, name_size)])
        score = title_size / baseline - .24 * abs(max(units) * title_size / page_width - .72)
        score += .012 if inline else 0
        if len(labels) == 2 and len(organization.replace(' ', '')) >= 15:
            score += .045
        candidates.append((score, rows))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def write_text(page, text, box, maximum, color, sender=False, wrap=True, sender_lines=0, name_scale=1, text_scale=1, leading=1.16):
    if sender:
        # Canonical Seoul metrics choose rows for every template/font. Font size
        # overrides affect only the sizes, never the organization/title split.
        rows = canonical_sender_plan(text, SENDER_REFERENCE_FONT)
        parsed = split_sender(' '.join(text.split()))
        individual = bool(parsed.get('name') and not parsed.get('group'))
        if sender_lines and not individual:
            # An explicit 'different layout' choice is a separate user override;
            # its rows also use the same reference geometry in every template.
            lines = balanced_sender_parts(text,fitz.Rect(45,800,796.89,1193),195,sender_lines,font_name=SENDER_REFERENCE_FONT)
            if lines is None:
                raise ValueError('가로 폭 기준을 만족하는 줄바꿈이 없습니다.')
            rows = [[(line,1.)] for line in lines]
        if not rows:
            return
        points = [[(line, ratio*SENDER_REFERENCE_NAME_SIZE*(name_scale if individual and ratio == 1 else text_scale))
                   for line, ratio in row] for row in rows]
        heights = [max(size for _,size in row) for row in points]
        widths = [sum(pdfmetrics.stringWidth(line,FONT_NAME,size) for line,size in row) for row in points]
        fit = min(1, maximum/195, box.width/max(widths), box.height/(sum(heights)*leading))
        points = [[(line,size*fit) for line,size in row] for row in points]
        if min(size for row in points for _,size in row) < 11:
            raise ValueError('보내는 분이 인쇄 영역에 비해 너무 깁니다. 명의와 주문 메모를 확인해 주세요.')
        # Do not apply destination-dependent name tracking: it changes the ratio.
        draw_text_rows(page,box,color,points,leading,'',center_ink=True)
        return
    rows = []
    for paragraph in text.splitlines():
        line = ''
        for word in paragraph.split():
            trial = f'{line} {word}'.strip()
            if wrap and line and pdfmetrics.stringWidth(trial,FONT_NAME,maximum*text_scale)>box.width:
                rows.append(line)
                line = word
            else:
                line = trial
        if line:
            rows.append(line)
    if not rows:
        return
    units = [pdfmetrics.stringWidth(line,FONT_NAME,text_scale) for line in rows]
    size = min(maximum,box.width/max(units),box.height/(len(rows)*text_scale*leading))
    if size*text_scale < 11:
        raise ValueError('인쇄 영역에 비해 문구가 너무 깁니다.')
    draw_text_rows(page,box,color,[[(line,size*text_scale)] for line in rows],leading,'')


def write_custom_message(page, text, box, maximum, color, text_scale):
    paragraphs = text.strip().splitlines()
    if not paragraphs:
        return

    def wrap_at(size):
        rows = []
        for paragraph in paragraphs:
            line = ''
            for word in paragraph.split():
                trial = f'{line} {word}'.strip()
                if line and pdfmetrics.stringWidth(trial, FONT_NAME, size) > box.width:
                    rows.append(line)
                    line = ''
                if not line and pdfmetrics.stringWidth(word, FONT_NAME, size) > box.width:
                    for character in word:
                        if line and pdfmetrics.stringWidth(line + character, FONT_NAME, size) > box.width:
                            rows.append(line)
                            line = ''
                        line += character
                else:
                    line = f'{line} {word}'.strip()
            rows.append(line)
        return rows

    leading = 1.3
    lower, upper = 11, max(11, maximum * 1.7 * text_scale)
    # Keep a short single-line greeting on one line.
    if len(paragraphs) == 1:
        width = pdfmetrics.stringWidth(paragraphs[0], FONT_NAME, 1)
        one_line = min(upper, box.width / width, box.height / leading)
        if one_line >= maximum * text_scale:
            draw_text_rows(page, box, color, [[(paragraphs[0], one_line)]], leading, '')
            return

    if len(wrap_at(lower)) * lower * leading > box.height:
        raise ValueError('인쇄 영역에 비해 문구가 너무 깁니다.')
    for _ in range(14):
        size = (lower + upper) / 2
        if len(wrap_at(size)) * size * leading <= box.height:
            lower = size
        else:
            upper = size
    rows = [[(line, lower)] for line in wrap_at(lower)]
    draw_text_rows(page, box, color, rows, leading, '')


def draw_text_rows(page, box, color, rows, leading, name, center_ink=False):
    heights = [max(points for _, points in row) for row in rows]
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(page.rect.width, page.rect.height))
    c.setFillColorRGB(*color)
    y = (box.y0 + box.y1 - sum(heights) * leading) / 2
    for row, height in zip(rows, heights):
        row_width = sum(pdfmetrics.stringWidth(line, FONT_NAME, points) for line, points in row)
        tracking = short_name_tracking(name, row_width, page.rect.width) if (
            len(row) == 1 and row[0][0] == name
        ) else 0
        x = (box.x0 + box.x1 - row_width - tracking) / 2
        baseline = page.rect.height - y - height
        for line, points in row:
            if tracking:
                text = c.beginText(x, baseline)
                text.setFont(FONT_NAME, points)
                text.setCharSpace(tracking)
                text.textOut(line)
                c.drawText(text)
            else:
                c.setFont(FONT_NAME, points)
                c.drawString(x, baseline, line)
            x += pdfmetrics.stringWidth(line, FONT_NAME, points)
        y += leading * height
    c.save()
    with fitz.open(stream=buffer.getvalue(), filetype='pdf') as layer:
        destination = page.rect
        if center_ink:
            probe = layer[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=True)
            alpha = Image.frombytes('RGBA', (probe.width, probe.height), probe.samples).getchannel('A')
            bounds = alpha.getbbox()
            if bounds:
                ink_center = (probe.y + (bounds[1] + bounds[3]) / 2) / 2
                offset = (box.y0 + box.y1) / 2 - ink_center
                destination = page.rect + (0, offset, 0, offset)
        page.show_pdf_page(destination, layer, 0)

def crop_photo(image, size, subject=None, safe=None):
    return image.resize(size, Image.Resampling.LANCZOS)


RICE_PHOTO_RECT = fitz.Rect(44, 117.9578, 797.8898, 620.5510)


def wedding_photo_size(paper, layout='general'):
    if layout == 'rice':
        return round(RICE_PHOTO_RECT.width*2), round(RICE_PHOTO_RECT.height*2)
    with fitz.open(ASSETS/'layout.pdf') as template:
        page = template[0 if paper == 'A4' else 12]
        rect = page.rect if paper == 'A4' else fitz.Rect(page.get_image_info()[0]['bbox'])
        return round(rect.width*2), round(rect.height*2)


def replace_memorial_heading(page, rice_kg, offset=0):
    import cv2
    import numpy as np

    w = page.rect.width
    region = fitz.Rect(w * .15, 30, w * .85, 345)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=region)
    pixels = np.array(Image.frombytes('RGB', (pix.width, pix.height), pix.samples))
    # The title is the bright lettering in the center of the dark header.
    mask = (pixels.min(axis=2) >= 170).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    keep = np.flatnonzero(stats[1:count, cv2.CC_STAT_AREA] >= 30) + 1
    title_mask = np.isin(labels, keep)
    points = cv2.findNonZero(title_mask.astype(np.uint8))
    if points is None:
        raise ValueError('근조 양식의 상단 한자 영역을 찾지 못했습니다.')
    x, y, width, height = cv2.boundingRect(points)
    if width < 60 or height < 36:
        raise ValueError('근조 양식의 상단 한자 영역이 너무 작습니다.')
    title_box = fitz.Rect((pix.x+x)/2, (pix.y+y)/2, (pix.x+x+width)/2, (pix.y+y+height)/2)
    colors = pixels[title_mask]
    bright = colors[colors.min(axis=1) >= np.percentile(colors.min(axis=1), 65)]
    color = tuple(int(value) for value in np.median(bright, axis=0))
    # Remove searchable title glyphs without touching the background image.
    hits = [rect for glyph in ('謹', '谨', '弔', '吊') for rect in page.search_for(glyph)
            if rect.intersects(title_box)]
    if len(hits) >= 2:
        for rect in hits:
            page.add_redact_annot(rect, fill=None, cross_out=False)
        page.apply_redactions(images=0, graphics=0)
    else:
        # Flattened templates need only the small region occupied by the title restored.
        mask = cv2.dilate(title_mask.astype(np.uint8), np.ones((15, 15), np.uint8))
        # Work at reduced resolution so thick glyph strokes do not leave a visible outline.
        reduced_size = (max(1, pix.width // 8), max(1, pix.height // 8))
        reduced = cv2.resize(pixels, reduced_size, interpolation=cv2.INTER_AREA)
        reduced_mask = cv2.resize(mask.astype(np.float32), reduced_size, interpolation=cv2.INTER_AREA) > 0
        clean = cv2.inpaint(reduced, reduced_mask.astype(np.uint8), 4, cv2.INPAINT_TELEA)
        clean = cv2.resize(clean, (pix.width, pix.height), interpolation=cv2.INTER_CUBIC)
        blend = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), 3)[..., None]
        left, top = max(0, x-20), max(0, y-20)
        right, bottom = min(pix.width, x+width+20), min(pix.height, y+height+20)
        repaired = np.clip(pixels * (1-blend) + clean * blend, 0, 255).astype(np.uint8)[top:bottom, left:right]
        replacement = io.BytesIO()
        Image.fromarray(repaired).save(replacement, 'PNG')
        rect = fitz.Rect((pix.x+left)/2, (pix.y+top)/2, (pix.x+right)/2, (pix.y+bottom)/2)
        page.insert_image(rect, stream=replacement.getvalue(), keep_proportion=False)
    with Image.open(ASSETS / f'rice-{rice_kg}kg.png') as source:
        alpha = source.convert('RGBA').getchannel('A')
    alpha = alpha.crop(alpha.getbbox())
    logo = Image.new('RGBA', alpha.size, (*color, 255))
    logo.putalpha(alpha)
    stream = io.BytesIO()
    logo.save(stream, 'PNG')
    center_x, center_y = (title_box.x0 + title_box.x1)/2, (title_box.y0 + title_box.y1)/2
    previous_height = min(title_box.height * 1.46, 2 * min(center_y - 24, 350 - center_y))
    logo_height = previous_height * 1.10
    logo_width = min(title_box.width, previous_height * logo.width / logo.height) * 1.10
    center_y = min(350 - logo_height / 2, max(24 + logo_height / 2, center_y))
    center_y += offset
    page.insert_image(fitz.Rect(center_x-logo_width/2, center_y-logo_height/2,
                               center_x+logo_width/2, center_y+logo_height/2), stream=stream.getvalue())


RICE_FORM_URL = 'https://form.jotform.com/251220921797458'


def draw_memorial_rice_notice(page):
    import cv2
    import numpy as np

    scale = page.rect.width / 841.89
    qr_box = fitz.Rect(734, 24, 812, 102) * scale
    modules = cv2.QRCodeEncoder_create().encode(RICE_FORM_URL)
    image = Image.fromarray(np.pad(modules, 4, constant_values=255))
    image = image.resize((image.width * 10, image.height * 10), Image.Resampling.NEAREST)
    data = io.BytesIO()
    image.save(data, 'PNG')
    page.insert_image(qr_box, stream=data.getvalue())
    page.insert_link({'kind': fitz.LINK_URI, 'from': qr_box, 'uri': RICE_FORM_URL})
    lines = (
        '설치 액자 또는 쌀을 받으시거나, 상주님 명의로 쌀을 기부하실 수 있습니다.',
        '우측 QR로 정보 전달 부탁드리며 발인 후 5일까지 회신 없으시면 쌀을 기부처리합니다.',
    )
    font_size = min(21, 666 / max(pdfmetrics.stringWidth(line, FONT_NAME, 1) for line in lines)) * scale
    buffer = io.BytesIO()
    drawing = canvas.Canvas(buffer, pagesize=(page.rect.width, page.rect.height))
    drawing.setFillColorRGB(.99, .96, .89)
    drawing.setFont(FONT_NAME, font_size)
    for index, line in enumerate(lines):
        drawing.drawRightString(710 * scale, page.rect.height - (47 + 25 * index) * scale, line)
    drawing.save()
    with fitz.open(stream=buffer.getvalue(), filetype='pdf') as layer:
        page.show_pdf_page(page.rect, layer, 0)


def match_wedding_rice_background(page):
    # Recolor only the original full-page beige fill in the two rice templates.
    document = page.parent
    for xref in page.get_contents():
        stream = document.xref_stream(xref)
        recolored, count = re.subn(
            rb'(?<!\S)0?\.847059\s+0?\.788235\s+0?\.729412(?=\s+rg\s+0\s+0\s+)',
            b'.949020 .913725 .847059', stream, count=1,
        )
        if count:
            document.update_stream(xref, recolored)
            return


def wedding_rice_plain_layout(page, badge):
    # Retain the top anchor while reducing both logo dimensions by exactly 20%.
    badge = fitz.Rect(badge)
    center_x = (badge.x0 + badge.x1) / 2
    width, height = badge.width * .8, badge.height * .8
    badge = fitz.Rect(center_x-width/2, badge.y0, center_x+width/2, badge.y0+height)
    # Remove only the separator path; retain the background, notice and frame.
    document = page.parent
    pattern = rb'(?<![0-9.])78(?:\.0+)?\s+329(?:\.0+)?\s+m\s+763\.8898\s+329(?:\.0+)?\s+l\s+S\b'
    for xref in page.get_contents():
        stream, count = re.subn(pattern, b'',
                                document.xref_stream(xref), count=1)
        if count:
            document.update_stream(xref, stream)
            break
    else:
        raise ValueError('Rice wedding separator not found in the selected template')
    return badge, (55,630,787,748)


def balance_rice_plain_text(page, draw_greeting, draw_sender, logo_path):
    """Use painted ink, not font boxes, to equalize the three vertical gaps."""
    image_box = fitz.Rect(page.get_image_info()[-1]['bbox'])
    with Image.open(logo_path).convert('RGBA') as logo:
        bounds = logo.getchannel('A').getbbox()
        logo_bottom = image_box.y0 + image_box.height * bounds[3] / logo.height
    frames = [d['rect'] for d in page.get_drawings() if d.get('color')
              and d['rect'].width > page.rect.width*.85
              and d['rect'].height > page.rect.height*.85]
    lower_edge = min(r.y1 for r in frames)
    with fitz.open() as text_layers:
        ink = []
        for draw in (draw_greeting, draw_sender):
            layer = text_layers.new_page(width=page.rect.width,height=page.rect.height)
            draw(layer)
            pix = layer.get_pixmap(matrix=fitz.Matrix(2,2),alpha=True)
            alpha = Image.frombytes('RGBA',(pix.width,pix.height),pix.samples).getchannel('A')
            bounds = alpha.getbbox()
            if bounds is None:
                ink.append(None)
            else:
                ink.append(fitz.Rect(bounds[0]/2,bounds[1]/2,bounds[2]/2,bounds[3]/2))
        visible = [(i,box) for i,box in enumerate(ink) if box is not None]
        if not visible:
            raise ValueError('Rice wedding sender is required')
        gap = (lower_edge-logo_bottom-sum(box.height for _,box in visible))/(len(visible)+1)
        if gap < 0:
            raise ValueError('Rice wedding text exceeds the available print area')
        top = logo_bottom+gap
        for i,box in visible:
            shift = top-box.y0
            destination = fitz.Rect(0,shift,page.rect.width,page.rect.height+shift)
            page.show_pdf_page(destination,text_layers,i)
            top += box.height+gap
    return gap


def memorial_sender_box(page):
    """Center visible sender ink between the actual divider and paper bottom.

    Equal 45pt safety insets preserve that center while keeping large glyphs
    clear of the divider and the lower print edge.
    """
    w,h=page.rect.width,page.rect.height
    rules=[shape['rect'] for shape in page.get_drawings()
           if shape['rect'].width>w*.45 and shape['rect'].height<2
           and h*.45<shape['rect'].y0<h*.7]
    divider=max(rules,key=lambda rect:rect.width).y0 if rules else h/2
    return fitz.Rect(45,divider+45,w-45,h-45)


def make_scroll(output, sender, message='', kind='wedding', paper='A3', wedding_photo=None, subject=None, safe=None, sender_lines=0, layout='general', name_scale=1, text_scale=1, rice_kg=10, custom_message=False):
    scaling = dict(name_scale=name_scale, text_scale=text_scale)
    def draw_message(page, text, box, maximum, color, wrap=True):
        if custom_message:
            write_custom_message(page, text, box, maximum, color, text_scale)
        else:
            write_text(page, text, box, maximum, color, wrap=wrap, leading=1.3, **scaling)

    if sender_lines and (name_scale != 1 or text_scale != 1):
        parsed = split_sender(sender)
        if parsed.get('name') and not parsed.get('group') and name_scale != 1:
            sender_lines = 0
    if kind not in ('wedding','memorial') or paper not in ('A3','A4','A4 1장') or not sender.strip():
        raise ValueError('종류, 용지와 보내는 분을 확인해 주세요.')
    memorial_rice = layout == 'rice' and kind == 'memorial'
    if layout == 'rice' and rice_kg not in (10, 20, 30):
        raise ValueError('쌀빛 중량은 10kg, 20kg, 30kg 중 선택해 주세요.')
    if memorial_rice and paper not in ('A3', 'A4'):
        raise ValueError('근조 쌀빛은 A3 또는 A4 두 장에서 선택해 주세요.')
    if layout == 'rice' and not memorial_rice:
        if (kind, paper) != ('wedding', 'A3'):
            raise ValueError('결혼 쌀빛은 A3에서 선택해 주세요.')
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        with fitz.open(ASSETS/('rice-photo.pdf' if wedding_photo else 'rice-plain.pdf')) as doc:
            page = doc[0]
            match_wedding_rice_background(page)
            badge = fitz.Rect(63, 646.55, 208, 760.55) if wedding_photo else fitz.Rect(116.44, 196.55, 725.45, 667.55)
            dx, dy = badge.width * .075, badge.height * .075
            badge += (-dx, -dy, dx, dy)
            if not wedding_photo:
                badge, plain_message_box = wedding_rice_plain_layout(page, badge)
            page.insert_image(badge, filename=str(ASSETS/f'rice-{rice_kg}kg.png'))
            if wedding_photo:
                photo = crop_photo(read_image(wedding_photo), wedding_photo_size(paper, layout))
                data = io.BytesIO()
                photo.save(data, 'JPEG', quality=94)
                page.insert_image(RICE_PHOTO_RECT, stream=data.getvalue(), keep_proportion=False)
                message_box, sender_box = (220, 645, 791, 770), (45, 805, 797, 1150)
            else:
                message_box, sender_box = plain_message_box, (45, 860, 797, 1150)
            if not wedding_photo:
                balance_rice_plain_text(page,
                    lambda target: draw_message(target,message,fitz.Rect(message_box),35,(.08,.08,.08),wrap=True),
                    lambda target: write_text(target,sender,fitz.Rect(sender_box),195,(.09,.16,.28),sender=True,sender_lines=sender_lines,**scaling),
                    ASSETS/f'rice-{rice_kg}kg.png')
            else:
                draw_message(page,message,fitz.Rect(message_box),35,(.08,.08,.08),wrap="\n" not in message)
                write_text(page,sender,fitz.Rect(sender_box),195,(.09,.16,.28),sender=True,sender_lines=sender_lines,**scaling)
            doc.save(output/'scroll.pdf', garbage=4, deflate=True)
            next(render_pages(doc)).save(output/'scroll.png')
        return
    if paper == 'A4 1장':
        if kind != 'memorial':
            raise ValueError('A4 1장은 근조 전용입니다.')
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        with fitz.open() as doc:
            page = doc.new_page(width=841.89, height=595.28)
            ink = (.08,.08,.08)
            draw_message(page, message or '하늘의 별이 되신 고인을 추모하며\n유가족 분들께 깊은 위로의 마음을 전합니다',
                       fitz.Rect(34,46,807.89,215), 48, ink, wrap=False)
            write_text(page, sender, fitz.Rect(65,210,776.89,540), 150, ink, sender=True, sender_lines=sender_lines, **scaling)
            doc.save(output/'scroll.pdf', garbage=4, deflate=True)
            next(render_pages(doc)).save(output/'scroll.png')
        return
    memorial = kind == 'memorial'
    photo = bool(wedding_photo) and not memorial
    indices = ((16,) if memorial else (12,) if photo else (14,)) if paper=='A3' else (
        (8,9) if memorial else (0,1) if photo else (4,5))
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    with fitz.open(ASSETS/'layout.pdf') as template, fitz.open() as doc:
        for i in indices:
            doc.insert_pdf(template,from_page=i,to_page=i)
        for pos,page in enumerate(doc):
            if memorial_rice and pos == 0:
                replace_memorial_heading(page, rice_kg, offset=59.5)
                draw_memorial_rice_notice(page)
            w,h=page.rect.width,page.rect.height
            is_sender = pos==len(doc)-1
            is_message = paper=='A3' or (pos==0 if memorial else pos==1)
            message_box = (350,550) if memorial else ((600,765) if paper=='A3' else (55,178))
            sender_box = ((635,1028) if memorial else (800,1150)) if paper=='A3' else (
                (45,h-45) if memorial else (211,561))
            if memorial and paper == 'A3':
                # General and rice memorials share the same sender area and
                # font limits, including longer or joint sender names.
                centered = memorial_sender_box(page)
                sender_box = (centered.y0,centered.y1)
            changes = []
            if message.strip() and is_message:
                changes.append((message_box,message,38 if paper=='A3' else 35,False))
            if is_sender:
                changes.append((sender_box,sender,195 if paper=='A3' or memorial else 170,True))
            for (top,bottom),_,_,_ in changes:
                page.add_redact_annot(fitz.Rect(0,top,w,bottom),fill=None)
            if changes:
                page.apply_redactions(images=0,graphics=0)
            if photo and pos==0:
                if paper=='A4':
                    # The photo-only page is replaced below with a clean page.
                    page.add_redact_annot(page.rect,fill=None)
                    page.apply_redactions(images=1,graphics=0)
                    rect=page.rect
                else:
                    rect=fitz.Rect(page.get_image_info()[0]['bbox'])
                    page.add_redact_annot(rect,fill=None)
                    page.apply_redactions(images=1,graphics=0)
                image=crop_photo(read_image(wedding_photo),(round(rect.width*2),round(rect.height*2)),subject,safe)
                buf=io.BytesIO()
                image.save(buf,format='JPEG',quality=94)
                page.insert_image(rect,stream=buf.getvalue())
            for (top,bottom),value,maximum,is_name in changes:
                if memorial_rice and not is_name:
                    top, bottom, maximum = 445.5, 551.5, 36
                light=memorial and (paper=='A3' or not is_name)
                color=(.99,.96,.89) if light else (.09,.16,.28) if is_name and not memorial else (.08,.08,.08)
                box = fitz.Rect(45, top, w - 45, bottom)
                if is_name:
                    write_text(page, value, box, maximum, color, sender=True, sender_lines=sender_lines, **scaling)
                else:
                    draw_message(page, value, box, maximum, color)
        doc.save(output/'scroll.pdf',garbage=4,deflate=True)
        images=list(render_pages(doc))
    stack(images).save(output/'scroll.png')
    if len(images)==2:
        for label,image in zip(('top','bottom'),images):
            image.save(output/f'scroll-{label}.png')

def make_sample(output, kind, scroll_files, artwork=None, frame_photo=None, bottom_only=False):
    if kind not in ('wedding','event','memorial') or (artwork is None)==(frame_photo is None):
        raise ValueError('행사 종류와 도록 작품 또는 직접 첨부 사진을 선택해 주세요.')
    if not 1<=len(scroll_files)<=2:
        raise ValueError('족자 이미지 한 장 또는 A4 위·아래 두 장을 넣어 주세요.')
    images=[]
    for file in map(Path,scroll_files):
        if file.suffix.lower()=='.pdf':
            with fitz.open(file) as doc:
                if len(doc) not in (1,2):
                    raise ValueError('족자 PDF는 한 장 또는 위·아래 두 장이어야 합니다.')
                images.extend(render_pages(doc))
        else:
            images.append(read_image(file))
    if len(images)>2:
        raise ValueError('족자 페이지가 두 장을 초과합니다.')
    scroll=stack(images)
    memorial=kind=='memorial'
    board=read_image(ASSETS/('memorial.png' if memorial else 'celebration.jpg'))
    x0,y0,x1,y1=(133,318,985,1510) if memorial else (178,278,1206,1732)
    slot=Image.new('RGB',(x1-x0,y1-y0),'white')
    fitted=ImageOps.contain(scroll,slot.size,Image.Resampling.LANCZOS)
    if bottom_only:
        board.paste(fitted, (x0+(slot.width-fitted.width)//2, y1-fitted.height))
    else:
        slot.paste(fitted, ((slot.width-fitted.width)//2,(slot.height-fitted.height)//2))
        board.paste(slot,(x0,y0))
    size=(1512,2016)
    sample=read_image(ASSETS/'easel.png').resize(size,Image.Resampling.LANCZOS)
    art=read_image(frame_photo if frame_photo is not None else catalog()[int(artwork)])
    portrait = art.height > art.width
    art_box = (600, 760) if portrait else (888, 588)
    # Clear the placeholder artwork before fitting the photo above the scroll.
    draw = ImageDraw.Draw(sample)
    draw.rectangle((378, 32 if portrait else 204, 1265, 791), fill='white')
    art=ImageOps.contain(art,art_box,Image.Resampling.LANCZOS)
    sample.paste(art,(822-art.width//2,792-art.height))
    sample.paste(board.resize((567,852),Image.Resampling.LANCZOS),(552,792))
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    sample.save(output/'sample.png')
    sample.save(output/'sample.pdf',resolution=150)

if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument('--order-json', type=Path)
    parser.add_argument('--sender')
    parser.add_argument('--message')
    parser.add_argument('--kind', choices=('wedding', 'memorial', 'event'), default='wedding')
    parser.add_argument('--paper', choices=('A3', 'A4', 'A4 1장'), default='A3')
    parser.add_argument('--wedding-photo', type=Path)
    parser.add_argument('--external-scroll', type=Path, nargs='+')
    parser.add_argument('--artwork', type=int, choices=range(1, 91))
    parser.add_argument('--frame-photo', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    order = json.loads(args.order_json.read_text()) if args.order_json else {}
    sender = args.sender if args.sender is not None else order.get('sender', '')
    message = args.message if args.message is not None else order.get('message', '')
    artwork = args.artwork
    if artwork is None and args.frame_photo is None:
        import re
        match = re.match(r'^([1-9][0-9]?)(?:\D|$)', str(order.get('artwork', '')).strip())
        if match and int(match[1]) <= 90:
            artwork = int(match[1])
    if args.kind == 'event' and not args.external_scroll:
        parser.error('행사 유형은 --external-scroll로 족자를 지정해 주세요.')
    if not args.external_scroll:
        make_scroll(args.output, sender, message, args.kind, args.paper, args.wedding_photo)
    if artwork is not None or args.frame_photo:
        make_sample(args.output, args.kind, args.external_scroll or [args.output/'scroll.pdf'], artwork, args.frame_photo, bottom_only=args.paper == "A4 1장")
    elif args.external_scroll:
        parser.error('이젤 시안에 사용할 --artwork 또는 --frame-photo를 지정해 주세요.')
