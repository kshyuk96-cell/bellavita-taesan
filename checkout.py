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
import order_ops
from order_operations import stage_paid

SEOUL = ZoneInfo('Asia/Seoul')
ORDER_PATH = re.compile(r'/i/([a-f0-9]{32})/(93500|99000)/order')
RECEIPT_PATH = re.compile(r'/invitation-orders/([a-f0-9]{32})/receipt')
ADMIN_PATH = re.compile(r'/invitation-orders/admin/([0-9]{10})/([a-f0-9]{64})')
TEMPLATE = Path(__file__).with_name('checkout.html')
ARTWORK = Path(__file__).with_name('assets') / 'checkout'
ARTWORK_FILES = frozenset({'sunflower-easel.webp', 'scroll-paper.webp',
                           'rice-10kg.webp', 'rice-20kg.webp', 'rice-30kg.webp'})
PAYAPP_FIELDS = ('USER_ID', 'LINK_KEY', 'LINK_VALUE', 'REQUEST_KEY')


def enabled():
    return os.getenv('INVITATION_CHECKOUT_ENABLED') == '1'


def payment_enabled(job):
    pilot_jobs = os.getenv('INVITATION_CHECKOUT_PILOT_JOBS', '')
    if pilot_jobs:
        return job in (item.strip() for item in pilot_jobs.split(','))
    return enabled()


def payment_config():
    return {key: os.getenv('INVITATION_PAYAPP_' + key, '') for key in PAYAPP_FIELDS}


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
        return json.loads((root / 'invitations' / job / f'{price}.order.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


def available(product):
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
    crop = product.get('wedding_crop')
    if type(crop) in (int, float) and 0 <= crop <= 100:
        return {'url': photo, 'position': float(crop)}
    return {'url': photo, 'position': None}


def wedding_image(preview, *, review=False):
    position = preview['position']
    style = f'object-fit:cover;object-position:50% {position:g}%' if position is not None else 'object-fit:contain'
    lazy = ' loading="lazy"' if review else ''
    return f'<img class="scroll-couple" src="{escape(preview["url"])}" alt="신랑신부 사진" style="{style}"{lazy}>'


def sender_preview(sender):
    parsed = split_sender(sender)
    name = parsed.get('name') or sender
    if parsed.get('group'):
        return {'group': name.splitlines()}
    structured = parsed.get('companyAffiliationTitle')
    label = list(structured) if structured else parsed.get('label', '').splitlines()
    extra = [part for key in ('note', 'footer') for part in parsed.get(key, '').splitlines() if part]
    return {'label': label, 'name': name + parsed.get('nameSuffix', ''), 'note': extra}


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
    groups = []
    for index, group in enumerate(product['groups']):
        options = ['<option value="">선택해 주세요</option>']
        for code, choice in group['choices'].items():
            amount = choice['amount']
            extra = f' (+{amount:,}원)' if amount else ''
            custom = ' data-custom="1"' if choice['custom'] else ''
            greeting = (f' data-greeting="{escape(re.sub(r"^\d+\.\s*", "", choice["label"]))}"'
                        if group['label'] == '축하 문구' and not choice['custom'] else '')
            kg = re.search(r'(?<!\d)(10|20|30)\s*(?:kg|㎏|키로|킬로)', choice['label'], re.I) if group['label'] == '상품 선택' else None
            weight = f' data-kg="{kg[1]}"' if kg else ''
            options.append(f'<option value="{escape(code)}" data-amount="{amount}"{custom}{greeting}{weight}>'
                           f'{escape(choice["label"])}{extra}</option>')
        greeting = ' id="greeting-choice"' if group['label'] == '축하 문구' else ''
        groups.append(f'<label>{escape(group["label"])}<select name="choice{index}"{greeting} required>'
                      + ''.join(options) + '</select></label>')
    pictures = ''.join(f'<img src="{escape(src)}" alt="쌀빛그림화환 상품 사진"'
                       + (' loading="lazy"' if index else '') + '>'
                       for index, src in enumerate(product['images']))
    preview = wedding_preview(product)
    if preview:
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
    return Template(TEMPLATE.read_text(encoding='utf-8')).substitute(
        escaped, action=escape(action), groups=''.join(groups), pictures=pictures,
        base=str(product['price']), base_display=f'{product["price"]:,}',
        receipt_section_state='' if 'cash_tax_type' in product else 'hidden',
        receipt_controls_state='' if 'cash_tax_type' in product else 'disabled',
        attempt=secrets.token_hex(16),
        button_state='' if can_pay else 'disabled', can_submit='true' if can_pay else 'false',
        notice='' if can_pay else '<p class="hint">현재 결제를 이용할 수 없습니다. 문의 031-8067-8580</p>')


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
    order = {'product': product['number'], 'title': product['title'], 'options': selected,
            'sender': sender, 'message': value('message', 200) if custom else '',
            'buyer': buyer, 'buyer_phone': buyer_phone,
            'venue': product['venue'], 'address': product['address'],
            'event_time': product['event_time'], 'amount': amount, 'source': product.get('source', '')}
    kind = fields.get('cash_receipt_type', '')
    if kind:
        if kind not in ('personal', 'business', 'self'):
            raise ValueError('현금영수증 발급 구분을 확인해 주세요.')
        identifier = ('0100001234' if kind == 'self' else buyer_phone if kind == 'personal' and
                      not fields.get('cash_receipt_identifier', '').strip() else
                      re.sub(r'[\s-]', '', fields.get('cash_receipt_identifier', '')))
        pattern = r'01[016789]\d{8}' if kind == 'personal' else r'\d{10}'
        if not re.fullmatch(pattern, identifier):
            raise ValueError('현금영수증 휴대폰 번호(11자리) 또는 사업자번호(10자리)를 확인해 주세요.')
        order['cash_receipt'] = {'kind': kind, 'identifier': identifier}
    return order


def setup(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS invitation_orders (
        id TEXT PRIMARY KEY, job TEXT NOT NULL, price INTEGER NOT NULL, created TEXT NOT NULL,
        details TEXT NOT NULL, state TEXT NOT NULL, payment_number TEXT UNIQUE, payment_url TEXT)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS invitation_photo_reviews (
        order_id TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS payment_events (
        order_id TEXT NOT NULL, event_key TEXT NOT NULL, kind TEXT NOT NULL,
        payment_number TEXT NOT NULL, amount INTEGER NOT NULL,
        provider_time TEXT NOT NULL, received TEXT NOT NULL, previous_state TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(order_id, event_key))''')
    if 'previous_state' not in {row[1] for row in connection.execute('PRAGMA table_info(payment_events)')}:
        connection.execute("ALTER TABLE payment_events ADD COLUMN previous_state TEXT NOT NULL DEFAULT ''")
    connection.execute('''CREATE TABLE IF NOT EXISTS payment_checks (
        id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, payment_number TEXT NOT NULL,
        amount INTEGER NOT NULL, note TEXT NOT NULL, checked TEXT NOT NULL)''')
    order_ops.setup(connection)


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
        try:
            result = parse_qs(reply.read(32768).decode('utf-8'))
        except ValueError as error:
            raise OSError('Uncertain payment response') from error
    if result.get('errno') == ['9040'] or result.get('errorCode') == ['9040']:
        raise OSError('Payment request timed out')
    if result.get('state') == ['0']:
        raise ValueError('결제창을 열지 못했어요. 문의 031-8067-8580')
    if result.get('state') != ['1'] or not result.get('mul_no') or not result.get('payurl'):
        raise OSError('Uncertain payment response')
    number, url = result['mul_no'][0], result['payurl'][0]
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise OSError('Unexpected payment URL') from error
    if not re.fullmatch(r'[0-9]{1,20}', number) or parsed.scheme not in ('http', 'https') or (
        parsed.netloc not in ('payapp.kr', 'www.payapp.kr', 'api.payapp.kr')
    ):
        raise OSError('Unexpected payment response')
    return number, urlunsplit(parsed._replace(scheme='https'))


def start_order(db, job, price, product, fields, origin, config):
    attempt = fields.get('attempt', '')
    if not re.fullmatch(r'[a-f0-9]{32}', attempt):
        raise ValueError('주문서를 다시 열어 주세요.')
    order = normalize_order(product, fields)
    details = json.dumps({**order, 'wedding_preview': wedding_preview(product),
                          'product_snapshot': product}, ensure_ascii=False)
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM invitation_orders WHERE id=?', (attempt,)).fetchone()
        if row:
            saved = json.loads(row['details'])
            saved.pop('recipient', None)
            saved.pop('recipient_phone', None)
            saved.pop('wedding_preview', None)
            saved.pop('product_snapshot', None)
            saved.setdefault('source', '')
            if row['job'] != job or row['price'] != int(price) or saved != order:
                raise ValueError('주문서를 다시 열어 주세요.')
            if row['state'] == 'waiting' and row['payment_url']:
                return row['payment_url']
            if row['state'] in ('paid', 'cancelled', 'partial_cancel'):
                return f'/invitation-orders/{attempt}/receipt'
            if row['state'] == 'failed':
                raise ValueError('결제창을 열지 못했어요. 문의 031-8067-8580')
            raise ValueError('결제 요청을 확인하고 있어요. 문의 031-8067-8580')
        connection.execute('INSERT INTO invitation_orders VALUES (?,?,?,?,?,?,NULL,NULL)',
                           (attempt, job, int(price), dt.datetime.now(SEOUL).isoformat(), details, 'requesting'))
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
    try:
        dt.datetime.strptime(when, '%Y%m%d%H%M%S')
    except ValueError:
        return False
    expected = {'userid': config['USER_ID'], 'linkkey': config['LINK_KEY'],
                'linkval': config['LINK_VALUE']}
    if any(not hmac.compare_digest(value.encode(), fields.get(key, '').encode()) for key, value in expected.items()):
        return False
    if not hmac.compare_digest(signature(config, when), fields.get('hash', '').lower()):
        return False
    incoming = fields.get('pay_state')
    if incoming not in ('1', '4', '8', '9', '10', '32', '64', '70', '71'):
        return False
    partial = incoming in ('70', '71')
    attempt = fields.get('var1', '')
    number = fields.get('orig_mul_no' if partial else 'mul_no', '')
    amount = fields.get('orig_price' if partial else 'price')
    if not re.fullmatch(r'[a-f0-9]{32}', attempt) or not re.fullmatch(r'[0-9]{1,20}', number):
        return False
    event_number = fields.get('mul_no', '')
    if not re.fullmatch(r'[0-9]{1,20}', event_number):
        return False
    kind = ('partial' if partial else 'cancel' if incoming in ('9', '64') else
            'request_cancel' if incoming in ('8', '32') else 'paid' if incoming == '4' else 'waiting')
    event_amount = fields.get('price', '')
    if not re.fullmatch(r'[0-9]{1,9}', event_amount):
        return False
    event_amount = int(event_amount)
    # A partial cancellation has its own mul_no; 70/71 and repeated delivery
    # describe the same cancellation, never a second refund.
    key = kind + ':' + event_number
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM invitation_orders WHERE id=?', (attempt,)).fetchone()
        if (not row or row['payment_number'] not in (None, number) or
                amount != str(json.loads(row['details'])['amount'])):
            return False
        total = json.loads(row['details'])['amount']
        if partial and (number == event_number or not 0 < event_amount <= total):
            return False
        existing = connection.execute('SELECT amount FROM payment_events WHERE order_id=? AND event_key=?',
                                      (attempt, key)).fetchone()
        if existing and existing['amount'] != event_amount:
            return False
        refunded = connection.execute("SELECT COALESCE(SUM(amount),0) FROM payment_events WHERE order_id=? AND kind='partial'",
                                      (attempt,)).fetchone()[0]
        if partial and not existing and refunded + event_amount > total:
            return False
        # The UNIQUE payment number also protects orders whose first notification
        # arrives before payrequest returns. Reject a cross-order reference cleanly.
        duplicate = connection.execute('SELECT id FROM invitation_orders WHERE payment_number=? AND id!=?',
                                       (number, attempt)).fetchone()
        if duplicate:
            return False
        had_request_cancel = connection.execute("""SELECT 1 FROM payment_events WHERE order_id=? AND kind='request_cancel'
                                                AND previous_state IN ('requesting','waiting','failed')""",
                                                (attempt,)).fetchone()
        had_full_cancel = connection.execute("SELECT 1 FROM payment_events WHERE order_id=? AND kind='cancel'",
                                             (attempt,)).fetchone()
        connection.execute('INSERT OR IGNORE INTO payment_events VALUES (?,?,?,?,?,?,?,?)',
                           (attempt, key, kind, event_number, event_amount, when,
                            dt.datetime.now(SEOUL).isoformat(), row['state']))
        state = row['state']
        if kind == 'cancel':
            state = 'cancelled'
        elif partial and (state != 'cancelled' or (had_request_cancel and not had_full_cancel)):
            state = 'partial_cancel'
        elif kind == 'paid' and (state not in ('cancelled', 'partial_cancel') or
                                (state == 'cancelled' and had_request_cancel and not had_full_cancel)):
            state = 'paid'
        elif kind == 'request_cancel' and state in ('requesting', 'waiting', 'failed'):
            state = 'cancelled'
        elif kind == 'waiting' and state == 'requesting':
            state = 'waiting'
        connection.execute('UPDATE invitation_orders SET state=?, payment_number=? WHERE id=?', (state, number, attempt))
        is_shop = connection.execute('SELECT 1 FROM shop_offers WHERE id=?', (row['job'],)).fetchone()
        if state == 'paid' and not is_shop:
            stage_paid(connection, row)
        order_ops.on_payment(connection, attempt, state, fields)
    return True
