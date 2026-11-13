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

SEOUL = ZoneInfo('Asia/Seoul')
ORDER_PATH = re.compile(r'/i/([a-f0-9]{32})/(93500|99000)/order')
RECEIPT_PATH = re.compile(r'/invitation-orders/([a-f0-9]{32})/receipt')
TEMPLATE = Path(__file__).with_name('checkout.html')
PAYAPP_FIELDS = ('USER_ID', 'LINK_KEY', 'LINK_VALUE', 'REQUEST_KEY')


def enabled():
    return os.getenv('INVITATION_CHECKOUT_ENABLED') == '1'


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


def admin_page(orders):
    rows = []
    for order in orders:
        data = json.loads(order['details'])
        options = ', '.join(value for _, value in data['options'])
        values = (order['id'], order['created'][:16].replace('T', ' '), data['title'], options,
                  data['sender'], data['message'], data['buyer'] + ' ' + data['buyer_phone'],
                  data['recipient'] + ' ' + data['recipient_phone'],
                  data['venue'] + ' ' + data['address'], f"{data['amount']:,}원",
                  {'paid': '결제 완료', 'cancelled': '취소', 'partial_cancel': '일부 취소'}[order['state']])
        rows.append('<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in values) + '</tr>')
    headings = ('주문번호', '주문 시각', '상품', '옵션', '보내시는 분', '직접 쓴 문구', '주문자',
                '받으시는 분', '설치 장소', '주문금액', '상태')
    table = ('<table><thead><tr>' + ''.join(f'<th scope="col">{name}</th>' for name in headings) +
             '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>') if rows else '<p>확정된 주문이 없어요.</p>'
    return ('<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>초대장 주문 내역</title><style>body{font:16px system-ui;margin:20px}'
            'table{border-collapse:collapse}td,th{padding:8px;border:1px solid #aaa;text-align:left}</style>'
            '<h1>초대장 주문 내역</h1>' + table + '</html>')


def render(product, action, can_pay):
    escaped = {key: escape(str(product[key])) for key in ('title', 'date', 'clock', 'venue', 'address')}
    groups = []
    for index, group in enumerate(product['groups']):
        options = ['<option value="">선택해 주세요</option>']
        for code, choice in group['choices'].items():
            amount = choice['amount']
            extra = f' (+{amount:,}원)' if amount else ''
            custom = ' data-custom="1"' if choice['custom'] else ''
            options.append(f'<option value="{escape(code)}" data-amount="{amount}"{custom}>'
                           f'{escape(choice["label"])}{extra}</option>')
        groups.append(f'<label>{escape(group["label"])}<select name="choice{index}" required>'
                      + ''.join(options) + '</select></label>')
    pictures = ''.join(f'<img src="{escape(src)}" alt="쌀빛그림화환 상품 사진"'
                       + (' loading="lazy"' if index else '') + '>'
                       for index, src in enumerate(product['images']))
    return Template(TEMPLATE.read_text(encoding='utf-8')).substitute(
        escaped, action=escape(action), groups=''.join(groups), pictures=pictures,
        base=str(product['price']), base_display=f'{product["price"]:,}',
        attempt=secrets.token_hex(16),
        button_state='' if can_pay else 'disabled', can_submit='true' if can_pay else 'false',
        notice='' if can_pay else '<p class="hint">현재 결제를 이용할 수 없습니다. 문의 031-8067-8580</p>')


def normalize_order(product, fields):
    def value(key, limit, required=True):
        text = fields.get(key, '').strip()
        if (required and not text) or len(text) > limit:
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
        selected.append((group['label'], choice['label']))
        custom |= choice['custom']
    buyer_phone = re.sub(r'[\s-]', '', value('buyer_phone', 20))
    if not re.fullmatch(r'01[016789]\d{7,8}', buyer_phone):
        raise ValueError('주문자 휴대폰 번호를 확인해 주세요.')
    buyer = value('buyer', 60)
    if fields.get('same_recipient') == 'yes':
        recipient, recipient_phone = buyer, buyer_phone
    else:
        recipient = value('recipient', 60)
        recipient_phone = re.sub(r'[\s-]', '', value('recipient_phone', 24))
        if not re.fullmatch(r'0\d{7,11}', recipient_phone):
            raise ValueError('받으시는 분 연락처를 확인해 주세요.')
    return {'product': product['number'], 'title': product['title'], 'options': selected,
            'sender': value('sender', 140), 'message': value('message', 200) if custom else '',
            'buyer': buyer, 'buyer_phone': buyer_phone, 'recipient': recipient,
            'recipient_phone': recipient_phone, 'venue': product['venue'], 'address': product['address'],
            'event_time': product['event_time'], 'amount': amount}


def setup(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS invitation_orders (
        id TEXT PRIMARY KEY, job TEXT NOT NULL, price INTEGER NOT NULL, created TEXT NOT NULL,
        details TEXT NOT NULL, state TEXT NOT NULL, payment_number TEXT UNIQUE, payment_url TEXT)''')


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
    attempt = fields.get('attempt', '')
    if not re.fullmatch(r'[a-f0-9]{32}', attempt):
        raise ValueError('주문서를 다시 열어 주세요.')
    order = normalize_order(product, fields)
    details = json.dumps(order, ensure_ascii=False)
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM invitation_orders WHERE id=?', (attempt,)).fetchone()
        if row:
            if row['job'] != job or row['price'] != int(price) or row['details'] != details:
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
    return True
