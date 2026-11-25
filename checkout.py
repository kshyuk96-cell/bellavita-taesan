"""Invitation-only order forms backed by copies of existing BlogPay products."""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
from html import escape
from pathlib import Path
from string import Template
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, build_opener
from zoneinfo import ZoneInfo

from lxml import html

from blogpay import NoRedirect
from scroll_sender import split_sender
from order_operations import stage_paid

SEOUL = ZoneInfo('Asia/Seoul')
ORDER_PATH = re.compile(r'/i/([a-f0-9]{32})/(93500|99000)/order')
RECEIPT_PATH = re.compile(r'/invitation-orders/([a-f0-9]{32})/receipt')
ADMIN_PATH = re.compile(r'/invitation-orders/admin/([0-9]{10})/([a-f0-9]{64})')
TEMPLATE = Path(__file__).with_name('checkout.html')
ARTWORK = Path(__file__).with_name('assets') / 'checkout'
MEMORIAL_ARTWORK = Path(__file__).with_name('erp_invitation_assets')
SCROLL_ARTWORK = Path(__file__).with_name('wedding_scroll') / 'assets'
ARTWORK_PATHS = {
    'sunflower-easel.webp': ARTWORK / 'sunflower-easel.webp',
    'scroll-paper.webp': ARTWORK / 'scroll-paper.webp',
    'rice-10kg.webp': ARTWORK / 'rice-10kg.webp',
    'rice-20kg.webp': ARTWORK / 'rice-20kg.webp',
    'rice-30kg.webp': ARTWORK / 'rice-30kg.webp',
    'memorial-easel-base.webp': MEMORIAL_ARTWORK / 'memorial-easel-base.webp',
    'memorial-frame-right.jpg': MEMORIAL_ARTWORK / 'memorial-frame-right.jpg',
    'memorial-rice-10kg-base.webp': MEMORIAL_ARTWORK / 'memorial-rice-10kg-base.webp',
    'hy-gungseo.ttf': SCROLL_ARTWORK / 'hy-gungseo.ttf',
}
ARTWORK_FILES = frozenset(ARTWORK_PATHS)
MEMORIAL_MESSAGES = (
    '하늘의 별이 되신 고인을 추모하며 삼가 고인의 영전 앞에 깊은 애도의 마음을 전합니다',
    '하늘의 별이 되신 고인을 추모하며 유가족분들께 깊은 위로의 마음을 전합니다',
    '주님 품 안에서 고인께 영원한 안식과 평안 있으시길 마음 모아 기도 드립니다',
    '고인께서 주님 안에 평안하시고 주님의 위로가 유가족분들께 함께하시길 기도 드립니다',
    '삼가 고인의 명복을 빕니다.',
    '삼가 故人의 冥福을 빕니다.',
)
PRODUCT_DETAIL_SECTION = '''<section>
<button class="detail-open" type="button" id="open-detail">상세설명 보기</button>
</section>'''
PRODUCT_DETAIL_DIALOG = '''<dialog class="product-detail" id="product-detail" aria-labelledby="detail-title">
<div class="detail-head"><h2 id="detail-title">상품 설명</h2><form method="dialog"><button>닫기</button></form></div>
<div class="detail-content">
<p><strong>쌀(쌀빛그림)화환 관련</strong></p>
<p>신청해주신 쌀빛그림화환은 친환경 웨딩갤러리를 만들기 위해 액자를 설치하고<br>예식 후 쌀로 교환하여 혼주님께 드리거나 필요한 시설에 기부합니다.</p>
<p>필요한 웨딩액자나 작품액자는 준비된 액자를 사용하니<br>전화나 문자를 이용하시거나 주문시스템에서 시청하시면 됩니다.</p>
<p>문의 전화 031-8067-8580</p>
<img src="https://bellavita1004.blogpay.io/img/editor/bellavita1004/temp/0524251001759278768.jpg" alt="쌀빛그림화환 상세 안내" loading="lazy">
</div>
</dialog>'''
PAYAPP_FIELDS = ('USER_ID', 'LINK_KEY', 'LINK_VALUE', 'REQUEST_KEY')


def artwork_path(filename):
    return ARTWORK_PATHS.get(filename)


def artwork_mime(filename):
    return {'jpg': 'image/jpeg', 'svg': 'image/svg+xml', 'ttf': 'font/ttf'}.get(
        filename.rsplit('.', 1)[-1], 'image/webp')


def memorial_choices():
    """Approved ERP condolence texts and the shared custom option."""
    return {'0': {'label': '직접 입력', 'amount': 0, 'custom': True}, **{
        str(index): {'label': message, 'amount': 0, 'custom': False}
        for index, message in enumerate(MEMORIAL_MESSAGES, 1)
    }}


def apply_memorial_rules(product):
    """Apply current memorial choices on read, including already-published links."""
    if product.get('kind') != 'memorial':
        return product
    from erp_memorial_invitation import canonical_product_title
    product['title'] = canonical_product_title(product.get('title'))
    # The deceased/mourner is fixed by the published obituary product.  It is
    # not a customer choice, so old snapshots must lose the legacy required
    # select at read time as well as newly published products.
    product['groups'] = [
        group for group in product.get('groups', [])
        if group.get('label') != '받으시는 분'
    ]
    for group in product['groups']:
        if group.get('label') in ('축하 문구', '근조 문구'):
            group['label'] = '근조 문구'
            group['choices'] = memorial_choices()
    return product


def installation_venue(product):
    """Return the visible venue, adding a memorial room when snapshots omit it."""
    venue = str(product.get('venue') or '').strip()
    if product.get('kind') != 'memorial':
        return venue
    room = str(product.get('funeralRoom') or '').strip()
    if not room:
        # Older immutable checkout snapshots kept the room only in the title:
        # "... · 장례식장 특1호실".  Recover only the suffix after the exact
        # venue, so unrelated words in a product name are never shown as a room.
        suffix = str(product.get('title') or '').rpartition('·')[2].strip()
        if venue and suffix.startswith(venue):
            room = suffix[len(venue):].strip()
    if room and room not in venue:
        return ' '.join(filter(None, (venue, room)))
    return venue


def enabled():
    return os.getenv('INVITATION_CHECKOUT_ENABLED') == '1'


def payment_enabled(job):
    if os.getenv('INVITATION_LOCAL_CHECKOUT_ENABLED')=='1':
        root=Path(os.getenv('TAESAN_DATA','taesan-data')).resolve()
        product=read(root,job,'93500')
        if product and product.get('localCheckout') and read(root,job,'99000'):
            return True
    pilot_jobs = os.getenv('INVITATION_CHECKOUT_PILOT_JOBS', '')
    if pilot_jobs:
        return job in (item.strip() for item in pilot_jobs.split(','))
    return enabled()


def payment_config():
    return {key: os.getenv('INVITATION_PAYAPP_' + key, '') for key in PAYAPP_FIELDS}


def bank_config():
    return {'bank': os.getenv('INVITATION_BANK_NAME', '국민은행').strip(),
            'account': os.getenv('INVITATION_BANK_ACCOUNT', '037601-04-151244').strip(),
            'holder': os.getenv('INVITATION_BANK_HOLDER', '브이에스엔씨').strip()}


def product_snapshot(client, row):
    """Read photos, required choices and prices from the copied product's order form."""
    url = client.order_link(row)
    shop = urlsplit(client.origin).hostname
    allowed = {'bp2.kr', shop, 'blogpay.co.kr'}
    opener = build_opener(NoRedirect)
    for _ in range(5):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc not in allowed:
            raise ValueError('복사한 상품의 주문 주소를 확인해 주세요.')
        try:
            with opener.open(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=8) as reply:
                raw = reply.read(4_000_001)
            break
        except HTTPError as error:
            location = error.headers.get('Location')
            if error.code not in (301, 302, 303, 307, 308) or not location:
                raise ValueError('복사한 상품 주문서를 열지 못했어요. 판매 상태를 확인해 주세요.') from None
            url = urljoin(url, location)
        except OSError:
            raise ValueError('복사한 상품 주문서를 열지 못했어요. 잠시 뒤 다시 시도해 주세요.') from None
    else:
        raise ValueError('복사한 상품 주문서의 연결 주소를 확인해 주세요.')
    if len(raw) > 4_000_000:
        raise ValueError('상품 주문서가 너무 커서 읽지 못했어요.')

    try:
        tree = html.fromstring(raw.decode('utf-8'))
        return parse_product(tree, row, url, shop)
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError('복사한 상품의 사진과 주문 옵션을 확인해 주세요.') from error


def parse_product(tree, row, url, shop):
    images = []
    media_host = shop.split('.', 1)[0] + '.blogpay.io'
    for image in tree.xpath('//section[contains(concat(" ",normalize-space(@class)," ")," product-info ")]//img[starts-with(@id,"slide")]/@src'):
        resolved = urljoin(url, image)
        parsed = urlsplit(resolved)
        if parsed.scheme == 'https' and parsed.netloc in (media_host, shop) and resolved not in images:
            images.append(resolved)
    groups = []
    for select in tree.xpath('//select[starts-with(@name,"noOption[")]'):
        placeholder = next(iter(select.xpath('./option[not(@value) or @value=""]')), None)
        if placeholder is None or '필수' not in placeholder.text_content():
            raise ValueError('Unsupported optional choice')
        label = ' '.join(placeholder.text_content().split()).removesuffix(' (필수)')
        choices = {}
        for option in select.xpath('./option[@value!="" and not(@disabled) and not(@inven="0")]'):
            value = option.get('value')
            amount = int(option.get('price'))
            name = ' '.join((option.get('title') or option.text_content()).split())
            custom = '축하 문구' in label and name.startswith('0.') and '직접 입력' in name
            if not value.isdigit() or amount < 0 or not name:
                raise ValueError('Invalid choice')
            choices[value] = {'label': '직접 입력' if custom else name, 'amount': amount, 'custom': custom}
        if not choices:
            raise ValueError('No available choices')
        groups.append({'label': label, 'choices': choices})
    # Confirm the copy is the same kind of single-product form supported here.
    if (not images or len(groups) != 3 or
            {g['label'] for g in groups} != {'축하 문구', '받으시는 분', '상품 선택'} or
            len(tree.xpath('//input[starts-with(@name,"ioOption[")]')) != 1):
        raise ValueError('Unsupported product form')
    return {'number': str(row['goodNum']), 'title': row['goodName'], 'price': int(row['goodPrice']),
            'images': images, 'groups': groups}


def read(root, job, price):
    try:
        product=json.loads((root / 'invitations' / job / f'{price}.order.json').read_text(encoding='utf-8'))
        if product.get('localCheckout'):
            publication=product.get('publicationId','')
            if not re.fullmatch(r'[a-f0-9]{32}',publication):return None
            manifest=Path(os.getenv('ERP_INVITATION_DATA') or root)/'erp-invitations'/'public'/publication/'result.json'
            if not manifest.is_file() or json.loads(manifest.read_text()).get('fingerprint')!=product.get('releaseFingerprint'):return None
        product['_libraryKey']=job
        product['_libraryRoot']=str(root)
        import erp_invitation_product_edits
        # Apply current public memorial rules after the editable text overlay,
        # so an old stored title or legacy recipient choice cannot reappear.
        return apply_memorial_rules(erp_invitation_product_edits.apply(root,job,price,product))
    except FileNotFoundError:
        return None


def available(product):
    import erp_invitation_library as library
    gate=library.product_status(Path(product.get('_libraryRoot') or os.getenv('TAESAN_DATA','taesan-data')),product)
    if gate is not None:return gate['operating'] and not gate['salesStopped']
    return dt.datetime.now(SEOUL) < dt.datetime.fromisoformat(product['event_time'])


def admin_signature(expiry, secret):
    return hmac.new(secret.encode(), ('invitation-orders/' + expiry).encode(), 'sha256').hexdigest()


def admin_authorized(expiry, signature, secret):
    return int(expiry) >= dt.datetime.now(dt.timezone.utc).timestamp() and hmac.compare_digest(
        signature, admin_signature(expiry, secret))


def wedding_preview(product):
    photo = product.get('wedding_photo')
    if not isinstance(photo, str) or not photo.startswith('https://'):
        return None
    parsed = urlsplit(photo)
    native = re.fullmatch(r'/erp/i/([a-f0-9]{32})/(?:hero|checkout-photo)\.webp', parsed.path)
    if (product.get('localCheckout') is True and native
            and native[1] == product.get('publicationId')
            and not parsed.query and not parsed.fragment and not parsed.username and not parsed.password):
        # The public asset route applies the same two-face bounds used by the
        # automatic scroll renderer. Map on read so existing immutable product
        # snapshots also receive the fix without rewriting prices or payments.
        mapped = urlunsplit(parsed._replace(path='/erp/i/' + native[1] + '/checkout-photo.webp'))
        return {'url': mapped, 'position': None}
    crop = product.get('wedding_crop')
    if type(crop) in (int, float) and 0 <= crop <= 100:
        return {'url': photo, 'position': float(crop)}
    return {'url': photo, 'position': None}


def memorial_preview(product):
    if product.get('kind') != 'memorial':
        return None
    publication = product.get('publicationId', '')
    if product.get('localCheckout') is not True or not re.fullmatch(r'[a-f0-9]{32}', publication):
        return None
    return {
        'sample': product.get('images', [''])[0] if product.get('images') else '',
        'base': '/checkout-art/memorial-easel-base.webp',
        'frame': '/checkout-art/memorial-frame-right.jpg',
    }


def wedding_image(preview, *, review=False):
    position = preview['position']
    safe_header = bool(re.fullmatch(r'/erp/i/[a-f0-9]{32}/checkout-photo\.webp', urlsplit(preview['url']).path))
    # Its ratio already matches the CSS opening. Filling that opening resizes
    # the pre-cropped image only; neither a second crop nor letterboxing occurs.
    style = ('object-fit:fill' if safe_header else
             f'object-fit:cover;object-position:50% {position:g}%' if position is not None else 'object-fit:contain')
    lazy = ' loading="lazy"' if review else ''
    return f'<img class="scroll-couple" src="{escape(preview["url"])}" alt="신랑신부 사진" style="{style}"{lazy}>'


def sender_preview(sender):
    parsed = split_sender(sender)
    name = parsed.get('name') or sender
    # The live memorial preview uses the same semantic rows and ratios as the
    # production PDF renderer.  Keep the legacy fields for the unchanged
    # wedding preview and older clients.
    from wedding_scroll import render as scroll_render
    rows = [
        [{'text': text, 'ratio': ratio} for text, ratio in row]
        for row in scroll_render.canonical_sender_plan(
            sender, scroll_render.SENDER_REFERENCE_FONT)
    ]
    if parsed.get('group'):
        return {'group': name.splitlines(), 'rows': rows}
    structured = parsed.get('companyAffiliationTitle')
    label = list(structured) if structured else parsed.get('label', '').splitlines()
    extra = [part for key in ('note', 'footer') for part in parsed.get(key, '').splitlines() if part]
    return {'label': label, 'name': name + parsed.get('nameSuffix', ''),
            'note': extra, 'rows': rows}


def admin_page(orders, action):
    rows = []
    for order in orders:
        data = json.loads(order['details'])
        recipient = next((value for label, value in data['options'] if label == '받으시는 분'), '')
        options = ', '.join(value for label, value in data['options'] if label != '받으시는 분')
        saved_contact = ' '.join(filter(None, (data.get('recipient'), data.get('recipient_phone'))))
        if saved_contact:
            recipient = ' · '.join(filter(None, (recipient, saved_contact)))
        values = (order['id'], order['created'][:16].replace('T', ' '), data['title'], options,
                  data['sender'], data['message'], data['buyer'] + ' ' + data['buyer_phone'],
                  recipient,
                  data['venue'] + ' ' + data['address'], f"{data['amount']:,}원",
                  {'paid': '결제 완료', 'cancelled': '취소', 'partial_cancel': '일부 취소'}[order['state']])
        preview = data.get('wedding_preview')
        review = '사진 확인 필요 · 제작 보류'
        if order['photo_reviewed'] and order['state'] == 'paid':
            review = '사진 검토 완료'
        elif preview and order['state'] == 'paid':
            review = (f'<details class="photo-review"><summary>사진 검토 · 제작 보류</summary>{wedding_image(preview, review=True)}'
                      f'<form method="post" action="{escape(action)}">'
                      f'<input type="hidden" name="order_id" value="{escape(order["id"])}">'
                      '<label><input type="checkbox" name="both_faces" value="yes" required> '
                      '신랑·신부가 맞고 두 얼굴이 모두 온전히 보입니다.</label>'
                      '<button type="submit" disabled>사진 검토 완료</button></form></details>')
        elif order['state'] != 'paid':
            review = '제작 보류'
        rows.append('<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in values) + f'<td>{review}</td></tr>')
    headings = ('주문번호', '주문 시각', '상품', '옵션', '보내시는 분', '직접 쓴 문구', '주문자',
                '받으시는 분', '설치 장소', '주문금액', '상태', '제작 사진')
    table = ('<table><thead><tr>' + ''.join(f'<th scope="col">{name}</th>' for name in headings) +
             '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>') if rows else '<p>확정된 주문이 없어요.</p>'
    return ('<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>초대장 주문 내역</title><style>body{font:16px system-ui;margin:20px}'
            'table{border-collapse:collapse}td,th{padding:8px;border:1px solid #aaa;text-align:left}'
            '.scroll-couple{display:block;width:min(320px,80vw);aspect-ratio:1.5;margin:8px 0}'
            'button{display:block;margin-top:8px}</style>'
            '<h1>초대장 주문 내역</h1>' + table + '''<script>
            for (const review of document.querySelectorAll('.photo-review')) {
                const image = review.querySelector('img');
                const button = review.querySelector('button');
                const checkImage = () => { button.disabled = !image.naturalWidth; };
                image.addEventListener('load', checkImage);
                image.addEventListener('error', checkImage);
                checkImage();
            }
            </script></html>''')


def render(product, action, can_pay):
    escaped = {key: escape(str(product[key])) for key in ('title', 'date', 'clock', 'venue', 'address')}
    escaped['venue'] = escape(installation_venue(product))
    groups = []
    for index, group in enumerate(product['groups']):
        options = ['<option value="">선택해 주세요</option>']
        for code, choice in group['choices'].items():
            amount = choice['amount']
            extra = f' (+{amount:,}원)' if amount else ''
            custom = ' data-custom="1"' if choice['custom'] else ''
            greeting = (f' data-greeting="{escape(re.sub(r"^\d+\.\s*", "", choice["label"]))}"'
                        if group['label'] in ('축하 문구','근조 문구') and not choice['custom'] else '')
            kg = re.search(r'(?<!\d)(10|20|30)\s*(?:kg|㎏|키로|킬로)', choice['label'], re.I) if group['label'] == '상품 선택' else None
            weight = f' data-kg="{kg[1]}"' if kg else ''
            options.append(f'<option value="{escape(code)}" data-amount="{amount}"{custom}{greeting}{weight}>'
                           f'{escape(choice["label"])}{extra}</option>')
        greeting = ' id="greeting-choice"' if group['label'] in ('축하 문구','근조 문구') else ''
        groups.append(f'<label>{escape(group["label"])}<select name="choice{index}"{greeting} required>'
                      + ''.join(options) + '</select></label>')
    pictures = ''.join(f'<img src="{escape(src)}" alt="쌀빛그림화환 상품 사진"'
                       + (' loading="lazy"' if index else '') + '>'
                       for index, src in enumerate(product['images']))
    preview = wedding_preview(product)
    memorial = memorial_preview(product)
    if memorial:
        fallback = pictures
        pictures = f'''<figure class="wreath-preview memorial-preview" id="wreath-preview" data-preview-kind="memorial">
          <div class="wreath-viewport">
          <div class="wreath-stage">
            <img class="wreath-base" src="{escape(memorial['base'])}" alt="근조 쌀빛그림화환">
            <img class="memorial-frame-photo" src="{escape(memorial['frame'])}" alt="44번 설악산 울산바위 은하수 작품 액자">
            <div class="scroll-paper memorial-scroll-paper">
              <img class="memorial-scroll-base" id="scroll-weight" src="/checkout-art/memorial-rice-10kg-base.webp" alt="원본 은하수 쌀 10kg 근조 족자">
              <p class="memorial-rice-notice sr-only">설치 액자 또는 쌀을 받으시거나, 상주님 명의로 쌀을 기부하실 수 있습니다. 우측 QR로 정보 전달 부탁드리며 발인 후 5일까지 회신 없으시면 쌀을 기부처리합니다.</p>
              <a class="memorial-rice-qr" href="https://form.jotform.com/251220921797458" target="_blank" rel="noopener noreferrer" aria-label="쌀빛 수령 안내 정보 입력"></a>
              <p class="scroll-greeting" id="scroll-greeting"></p>
              <div class="scroll-sender" id="scroll-sender"></div>
            </div>
          </div>
          </div>
          <figcaption>근조 쌀빛그림화환 설치 예시입니다.<br>선택 내용이 바로 반영됩니다.</figcaption>
        </figure><div class="gallery" id="wreath-fallback" role="group" aria-label="근조 쌀빛그림화환 상품 사진" tabindex="0" hidden>{fallback}</div>'''
    elif preview:
        pictures = f'''<figure class="wreath-preview" id="wreath-preview">
          <div class="wreath-viewport">
          <div class="wreath-stage">
            <img class="wreath-base" src="/checkout-art/sunflower-easel.webp" alt="해바라기 작품 액자화환">
            <div class="scroll-paper">
              {wedding_image(preview)}
              <img class="scroll-weight" id="scroll-weight" src="/checkout-art/rice-10kg.webp" alt="쌀 10kg">
              <p class="scroll-greeting" id="scroll-greeting"></p>
              <div class="scroll-sender" id="scroll-sender"></div>
            </div>
          </div>
          </div>
          <figcaption>예식 당일 신랑신부 사진 및<br>풍경사진으로 설치해드립니다.</figcaption>
        </figure><div class="gallery" id="wreath-fallback" role="group" aria-label="상품 사진" tabindex="0" hidden>{pictures}</div>'''
    else:
        pictures = f'<div class="gallery" role="group" aria-label="상품 사진" tabindex="0">{pictures}</div>'
    memorial_checkout = product.get('kind') == 'memorial'
    page=Template(TEMPLATE.read_text(encoding='utf-8')).substitute(
        escaped, action=escape(action), groups=''.join(groups), pictures=pictures,
        base=str(product['price']), base_display=f'{product["price"]:,}',
        attempt=secrets.token_hex(16),
        button_state='' if can_pay else 'disabled', can_submit='true' if can_pay else 'false',
        detail_section='' if memorial_checkout else PRODUCT_DETAIL_SECTION,
        detail_dialog='' if memorial_checkout else PRODUCT_DETAIL_DIALOG,
        notice=('<p class="hint order-notice" style="white-space:pre-line">'+escape(str(product['orderNotice']))+'</p>' if product.get('orderNotice') else '')+('' if can_pay else '<p class="hint">현재 결제를 이용할 수 없습니다. 문의 031-8067-8580</p>'))
    if memorial_checkout:
        page=page.replace('웨딩액자','액자').replace('축하 문구','근조 문구').replace('신청해주신 쌀빛그림화환은 친환경 웨딩갤러리를 만들기 위해 액자를 설치하고<br>예식 후 쌀로 교환하여 혼주님께 드리거나 필요한 시설에 기부합니다.','조문의 마음을 전하는 쌀빛그림화환입니다.<br>선택한 상품과 빈소 정보를 확인해 주세요.')
    return page


def normalize_order(product, fields):
    def value(key, limit):
        text = fields.get(key, '').strip()
        if not text or len(text) > limit:
            raise ValueError('입력하신 주문 정보를 다시 확인해 주세요.')
        return text

    if fields.get('consent') != 'yes':
        raise ValueError('주문 정보 수집에 동의해 주세요.')
    selected, amount = [], product['price']
    custom = False
    for index, group in enumerate(product['groups']):
        choice = group['choices'].get(fields.get(f'choice{index}'))
        if not choice:
            raise ValueError('상품 옵션을 모두 선택해 주세요.')
        amount += choice['amount']
        selected.append([group['label'], choice['label']])
        custom |= choice['custom']
    buyer_phone = re.sub(r'[\s-]', '', value('buyer_phone', 20))
    if not re.fullmatch(r'01[016789]\d{7,8}', buyer_phone):
        raise ValueError('주문자 휴대폰 번호를 확인해 주세요.')
    sender = value('sender', 140)
    buyer = sender if fields.get('same_buyer') == 'yes' else value('buyer', 60)
    payment_method = fields.get('payment_method', '')
    if payment_method not in ('bank_transfer', 'payapp'):
        raise ValueError('결제 방법을 선택해 주세요.')
    order = {'product': product['number'], 'title': product['title'], 'options': selected,
            'sender': sender, 'message': value('message', 200) if custom else '',
            'buyer': buyer, 'buyer_phone': buyer_phone,
            'venue': installation_venue(product), 'address': product['address'],
            'event_time': product['event_time'], 'amount': amount, 'source': product.get('source', ''),
            'payment_method': payment_method}
    if product.get('delivery_date'):
        order['delivery_date'] = dt.date.fromisoformat(product['delivery_date']).isoformat()
    return order


def setup(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS invitation_orders (
        id TEXT PRIMARY KEY, job TEXT NOT NULL, price INTEGER NOT NULL, created TEXT NOT NULL,
        details TEXT NOT NULL, state TEXT NOT NULL, payment_number TEXT UNIQUE, payment_url TEXT)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS invitation_photo_reviews (
        order_id TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL)''')


def review_photo(connection, fields):
    order_id = fields.get('order_id', '')
    if fields.get('both_faces') != 'yes' or not re.fullmatch(r'[a-f0-9]{32}', order_id):
        return False
    cursor = connection.execute('''INSERT INTO invitation_photo_reviews(order_id, reviewed_at)
        SELECT id, ? FROM invitation_orders
        WHERE id=? AND state='paid' AND json_type(details, '$.wedding_preview')='object'
        ON CONFLICT(order_id) DO UPDATE SET reviewed_at=invitation_photo_reviews.reviewed_at''',
        (dt.datetime.now(SEOUL).isoformat(), order_id))
    return cursor.rowcount == 1


def payapp_request(config, order_id, order, origin):
    request_date = dt.datetime.now(SEOUL).strftime('%Y%m%d%H%M%S')
    fields = {'cmd': 'payrequest', 'userid': config['USER_ID'], 'goodname': order['title'],
              'price': order['amount'], 'recvphone': order['buyer_phone'], 'reqaddr': '0',
              'feedbackurl': origin + '/invitation-orders/payapp', 'var1': order_id,
              'smsuse': 'n', 'checkretry': 'y', 'requestdate': request_date,
              'hash': signature(config, request_date),
              'returnurl': f'{origin}/invitation-orders/{order_id}/receipt'}
    request = Request('https://api.payapp.kr/oapi/v2', urlencode(fields).encode('ascii'))
    with build_opener(NoRedirect).open(request, timeout=12) as reply:
        result = parse_qs(reply.read(32768).decode('utf-8'))
    if result.get('state') != ['1']:
        raise ValueError('결제창을 열지 못했어요. 문의 031-8067-8580')
    number, url = result['mul_no'][0], result['payurl'][0]
    parsed = urlsplit(url)
    if not re.fullmatch(r'[0-9]{1,20}', number) or parsed.scheme not in ('http', 'https') or (
        parsed.netloc not in ('payapp.kr', 'www.payapp.kr', 'api.payapp.kr')
    ):
        raise OSError('Unexpected payment response')
    return number, urlunsplit(parsed._replace(scheme='https'))


def start_order(db, job, price, product, fields, origin, config):
    if product.get('_libraryRoot') and not available(product):raise ValueError('이 행사의 판매가 중지되거나 운영 기간이 종료됐어요.')
    attempt = fields.get('attempt', '')
    if not re.fullmatch(r'[a-f0-9]{32}', attempt):
        raise ValueError('주문서를 다시 열어 주세요.')
    order = normalize_order(product, fields)
    details = json.dumps({**order, 'wedding_preview': wedding_preview(product)}, ensure_ascii=False)
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM invitation_orders WHERE id=?', (attempt,)).fetchone()
        if row:
            saved = json.loads(row['details'])
            saved.pop('recipient', None)
            saved.pop('recipient_phone', None)
            saved.pop('wedding_preview', None)
            saved.setdefault('source', '')
            # Delivery scheduling may change after a checkout attempt has been stored.
            saved.pop('delivery_date', None)
            if row['job'] != job or row['price'] != int(price) or saved != {
                key: value for key, value in order.items() if key != 'delivery_date'
            }:
                raise ValueError('주문서를 다시 열어 주세요.')
            if row['state'] == 'bank_pending':
                return f'/invitation-orders/{attempt}/receipt'
            if row['state'] == 'waiting' and row['payment_url']:
                return row['payment_url']
            if row['state'] in ('paid', 'cancelled', 'partial_cancel'):
                return f'/invitation-orders/{attempt}/receipt'
            if row['state'] == 'failed':
                raise ValueError('결제창을 열지 못했어요. 문의 031-8067-8580')
            raise ValueError('결제 요청을 확인하고 있어요. 문의 031-8067-8580')
        initial = 'bank_pending' if order['payment_method'] == 'bank_transfer' else 'requesting'
        connection.execute('INSERT INTO invitation_orders VALUES (?,?,?,?,?,?,NULL,NULL)',
                           (attempt, job, int(price), dt.datetime.now(SEOUL).isoformat(), details, initial))
    if order['payment_method'] == 'bank_transfer':
        import sendon_order_queue
        sendon_order_queue.enqueue(db, attempt, order)
        return f'/invitation-orders/{attempt}/receipt'
    if not all(config.values()):
        with db() as connection:
            connection.execute("UPDATE invitation_orders SET state='failed' WHERE id=? AND state='requesting'", (attempt,))
        raise ValueError('카드·간편결제를 이용할 수 없어요. 무통장 입금을 선택해 주세요.')
    try:
        number, url = payapp_request(config, attempt, order, origin)
    except ValueError:
        with db() as connection:
            connection.execute("UPDATE invitation_orders SET state='failed' WHERE id=? AND state='requesting'", (attempt,))
        raise
    with db() as connection:
        saved = connection.execute('''UPDATE invitation_orders SET payment_number=?, payment_url=?,
                state=CASE WHEN state='requesting' THEN 'waiting' ELSE state END
                WHERE id=? AND (payment_number IS NULL OR payment_number=?)''', (number, url, attempt, number))
        if not saved.rowcount:
            raise OSError('Payment reference mismatch')
    return url


def signature(config, date):
    return hashlib.sha256((config['USER_ID'] + date + config['REQUEST_KEY']).encode()).hexdigest()


def receive_payment(db, fields, config):
    when = fields.get('responsedate', '')
    if not re.fullmatch(r'[0-9]{14}', when):
        return False
    expected = {'userid': config['USER_ID'], 'linkkey': config['LINK_KEY'],
                'linkval': config['LINK_VALUE'], 'hash': signature(config, when)}
    if any(not hmac.compare_digest(value.encode(), fields.get(key, '').encode()) for key, value in expected.items()):
        return False
    incoming = fields.get('pay_state')
    partial = incoming in ('70', '71')
    attempt = fields.get('var1', '')
    number = fields.get('orig_mul_no' if partial else 'mul_no', '')
    amount = fields.get('orig_price' if partial else 'price')
    if not re.fullmatch(r'[a-f0-9]{32}', attempt) or not re.fullmatch(r'[0-9]{1,20}', number):
        return False
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM invitation_orders WHERE id=?', (attempt,)).fetchone()
        if (not row or row['payment_number'] not in (None, number) or
                amount != str(json.loads(row['details'])['amount'])):
            return False
        state = row['state']
        if incoming in ('8', '9', '32', '64'):
            state = 'cancelled'
        elif partial and state != 'cancelled':
            state = 'partial_cancel'
        elif incoming == '4' and state not in ('cancelled', 'partial_cancel'):
            state = 'paid'
        connection.execute('UPDATE invitation_orders SET state=?, payment_number=? WHERE id=?', (state, number, attempt))
        if state == 'paid':
            stage_paid(connection, row)
    if state == 'paid':
        import sendon_order_queue
        sendon_order_queue.enqueue(db, attempt, json.loads(row['details']), customer=True)
    return True
