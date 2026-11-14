"""Bellavita catalog and event offers for the existing checkout."""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime
from html import escape
from urllib.parse import urlsplit

from checkout import SEOUL
import order_ops
import shop_dashboard
import order_messages
import cash_receipts

ADMIN_PATH = re.compile(r'/shop/admin/([0-9]{10})/([a-f0-9]{64})')
ORDER_PATH = re.compile(r'/shop/order/([a-f0-9]{32})')
PRICE_LIMIT = 100_000_000
STATES = {'requesting': '결제 요청 확인 필요', 'waiting': '결제 대기',
          'failed': '결제 요청 실패', 'paid': '결제 완료',
          'cancelled': '취소', 'partial_cancel': '일부 취소'}


def admin_signature(expiry, secret):
    return hmac.new(secret.encode(), ('shop/admin/' + expiry).encode(), hashlib.sha256).hexdigest()


def admin_authorized(expiry, signature, secret):
    return int(expiry) >= time.time() and hmac.compare_digest(signature, admin_signature(expiry, secret))


def setup(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS shop_products (
        id TEXT PRIMARY KEY, details TEXT NOT NULL, revision INTEGER NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS shop_offers (
        id TEXT PRIMARY KEY, product_id TEXT NOT NULL, product_revision INTEGER NOT NULL,
        price INTEGER NOT NULL, details TEXT NOT NULL, active INTEGER NOT NULL)""")
    connection.execute('CREATE INDEX IF NOT EXISTS shop_order_job ON invitation_orders(job, created)')


def required(fields, key, limit):
    value = fields.get(key, '').strip()
    if not value or len(value) > limit:
        raise ValueError('필수 항목과 글자 수를 확인해 주세요.')
    return value


def money(value):
    if not re.fullmatch(r'[0-9]{1,9}', value) or not 0 <= int(value) <= PRICE_LIMIT:
        raise ValueError('가격은 원 단위 숫자로 입력해 주세요.')
    return int(value)


def image_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or len(value) > 600 or any(char.isspace() for char in value)):
        raise ValueError('사진은 HTTPS 주소로 입력해 주세요.')
    return value


def choices(text, *, priced=False):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if priced:
            label, separator, extra = line.rpartition('|')
            if not separator:
                raise ValueError('상품 옵션은 한 줄에 옵션명 | 추가금 형식으로 입력해 주세요.')
            amount = money(extra.strip())
            label = label.strip()
        else:
            label, amount = line, 0
        if not label or len(label) > 120 or len(result) >= 20:
            raise ValueError('옵션은 최대 20개, 이름은 120자까지 입력할 수 있어요.')
        result[str(len(result) + 1)] = {'label': label, 'amount': amount, 'custom': False}
    if not result:
        raise ValueError('옵션을 한 개 이상 입력해 주세요.')
    return result


def save_product(connection, fields):
    details = {
        'title': required(fields, 'title', 100),
        'price': money(required(fields, 'price', 9)),
        'images': [image_url(url.strip()) for url in required(fields, 'images', 3000).splitlines() if url.strip()],
        'options': required(fields, 'options', 2400),
        'greetings': required(fields, 'greetings', 2400),
        'cash_tax_type': fields.get('cash_tax_type', ''),
    }
    if details['cash_tax_type'] not in ('', 'TG01', 'TG02'):
        raise ValueError('현금영수증 과세 구분을 확인해 주세요.')
    if not details['price'] or len(details['images']) > 5:
        raise ValueError('기본 가격과 상품 사진(최대 5장)을 확인해 주세요.')
    greetings = choices(details['greetings'])
    greetings['custom'] = {'label': '직접 입력', 'amount': 0, 'custom': True}
    details['groups'] = [
        {'label': '상품 선택', 'choices': choices(details['options'], priced=True)},
        {'label': '축하 문구', 'choices': greetings},
        {'label': '받으시는 분', 'choices': choices('신랑측\n신부측\n행사 주최측')},
    ]
    data = json.dumps(details, ensure_ascii=False)
    product_id = fields.get('product_id', '')
    if not product_id:
        connection.execute('INSERT INTO shop_products VALUES (?, ?, 1)', (secrets.token_hex(16), data))
    elif not connection.execute('''UPDATE shop_products SET details=?, revision=revision+1
                                   WHERE id=? AND revision=?''',
                                 (data, product_id, fields.get('revision'))).rowcount:
        raise ValueError('상품이 변경됐어요. 최신 화면에서 다시 수정해 주세요.')


def issue_offers(connection, fields):
    product_id = required(fields, 'product_id', 32)
    row = connection.execute('SELECT * FROM shop_products WHERE id=?', (product_id,)).fetchone()
    if not row or str(row['revision']) != fields.get('revision'):
        raise ValueError('상품이 변경됐어요. 최신 화면에서 다시 발급해 주세요.')
    product = json.loads(row['details'])
    event = required(fields, 'event', 80)
    raw_time = required(fields, 'when', 16)
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}', raw_time):
        raise ValueError('행사 일시를 확인해 주세요.')
    try:
        when = datetime.fromisoformat(raw_time).replace(tzinfo=SEOUL)
    except ValueError:
        raise ValueError('행사 일시를 확인해 주세요.') from None
    if when <= datetime.now(SEOUL):
        raise ValueError('앞으로 열릴 행사만 등록할 수 있어요.')
    raw_prices = fields.get('prices', '').strip()
    prices = list(dict.fromkeys(money(price.strip()) for price in raw_prices.split(','))) if raw_prices else [product['price']]
    max_extra = max(choice['amount'] for choice in product['groups'][0]['choices'].values())
    if len(prices) > 5 or any(not price or price + max_extra > PRICE_LIMIT for price in prices):
        raise ValueError('판매가는 최대 5개까지 지정할 수 있어요. 가격과 추가금을 확인해 주세요.')
    recipient = {'wedding': '신랑측\n신부측', 'event': '행사 주최측'}.get(fields.get('kind'))
    if not recipient:
        raise ValueError('행사 구분을 선택해 주세요.')
    product['groups'][2]['choices'] = choices(recipient)
    snapshot = {
        'number': product_id, 'title': event + ' · ' + product['title'],
        'images': product['images'], 'groups': product['groups'],
        'venue': required(fields, 'venue', 120), 'address': required(fields, 'address', 200),
        'date': when.date().isoformat(), 'clock': when.strftime('%H:%M'),
        'event_time': when.isoformat(),
        'cash_tax_type': product.get('cash_tax_type', ''),
    }
    photo = fields.get('wedding_photo', '').strip()
    if photo and fields.get('kind') == 'wedding':
        snapshot['wedding_photo'] = image_url(photo)
    for price in prices:
        data = json.dumps(dict(snapshot, price=price), ensure_ascii=False)
        existing = connection.execute(
            'SELECT id FROM shop_offers WHERE product_id=? AND product_revision=? AND price=? AND details=?',
            (product_id, row['revision'], price, data),
        ).fetchone()
        if existing:
            continue
        connection.execute('INSERT INTO shop_offers VALUES (?, ?, ?, ?, ?, 1)',
                           (secrets.token_hex(16), product_id, row['revision'], price, data))


def change_offer(connection, fields):
    offer_id = required(fields, 'offer_id', 32)
    active = fields.get('active')
    if active not in ('0', '1') or not connection.execute(
        'UPDATE shop_offers SET active=? WHERE id=?', (int(active), offer_id)
    ).rowcount:
        raise ValueError('판매 링크를 찾지 못했어요.')


def read_offer(connection, offer_id):
    row = connection.execute('SELECT * FROM shop_offers WHERE id=?', (offer_id,)).fetchone()
    if not row:
        return None, False
    product=json.loads(row['details'])
    product['production_review_required']=os.getenv('SHOP_PRODUCTION_REVIEW_REQUIRED','1')!='0'
    return product, bool(row['active'])


def record_payment_check(connection, fields):
    """Record a merchant-side comparison without inventing a payment notification."""
    order_id = required(fields, 'order_id', 32)
    row = connection.execute('''SELECT o.* FROM invitation_orders o
        JOIN shop_offers s ON s.id=o.job WHERE o.id=?''', (order_id,)).fetchone()
    if not row:
        raise ValueError('쇼핑몰 주문을 찾지 못했어요.')
    order = json.loads(row['details'])
    number = fields.get('payment_number', '').strip()
    amount = money(required(fields, 'amount', 9))
    phone = re.sub(r'[\s-]', '', required(fields, 'buyer_phone', 20))
    note = required(fields, 'note', 600)
    if (not re.fullmatch(r'[0-9]{1,20}', number) or amount != order['amount']
            or phone != order['buyer_phone'] or fields.get('compared') != 'yes'
            or row['payment_number'] not in (None, number)):
        raise ValueError('페이앱의 원결제번호·원금액·주문자 전화번호를 주문과 대조해 주세요.')
    if connection.execute('SELECT 1 FROM invitation_orders WHERE payment_number=? AND id!=?',
                          (number, order_id)).fetchone():
        raise ValueError('다른 주문에 연결된 결제번호예요.')
    connection.execute('UPDATE invitation_orders SET payment_number=? WHERE id=?', (number, order_id))
    connection.execute('''INSERT INTO payment_checks(order_id,payment_number,amount,note,checked)
                          VALUES (?,?,?,?,?)''',
                       (order_id, number, amount, note, datetime.now(SEOUL).isoformat()))


def admin_page(connection, path, origin):
    def form(action, fields, button):
        return (f'<form method="post" action="{escape(path)}">'
                f'<input type="hidden" name="action" value="{action}">'
                f'{fields}<button>{button}</button></form>')

    def hidden(key, value):
        return f'<input type="hidden" name="{key}" value="{escape(str(value))}">'

    def field(label, name, value='', *, multiline=False, input_type='text', optional=False):
        required_attr = '' if optional else ' required'
        control = (f'<textarea name="{name}"{required_attr}>{escape(str(value))}</textarea>' if multiline
                   else f'<input type="{input_type}" name="{name}" value="{escape(str(value))}"{required_attr}>')
        return f'<label>{label}{control}</label>'

    def product_fields(product):
        tax_options = ''.join(f'<option value="{code}"' + (' selected' if product.get('cash_tax_type', '') == code else '')
                              + f'>{label}</option>' for code, label in
                              [('', '미설정 · 자동 발급 보류'), ('TG01', '과세'), ('TG02', '면세')])
        return (field('상품명', 'title', product.get('title', ''))
                + field('기본 판매가(원)', 'price', product.get('price', ''), input_type='number')
                + field('상품 사진 HTTPS 주소 · 한 줄에 하나', 'images',
                        '\n'.join(product.get('images', [])), multiline=True)
                + field('상품 옵션 · 옵션명 | 추가금', 'options', product.get('options', '쌀 10kg | 0'), multiline=True)
                + field('기본 축하 문구 · 한 줄에 하나', 'greetings',
                        product.get('greetings', '결혼을 진심으로 축하합니다'), multiline=True)
                + '<label>현금영수증 과세 구분<select name="cash_tax_type">' + tax_options + '</select></label>')

    products = []
    for row in connection.execute('SELECT * FROM shop_products ORDER BY rowid DESC'):
        product = json.loads(row['details'])
        identity = hidden('product_id', row['id']) + hidden('revision', row['revision'])
        event_fields = (
            field('행사명', 'event') + field('행사 일시(한국 시간)', 'when', input_type='datetime-local')
            + '<label>행사 구분<select name="kind"><option value="wedding">결혼</option>'
              '<option value="event">축하·행사</option></select></label>'
            + field('행사장·홀', 'venue') + field('설치 주소', 'address')
            + field('판매가(원) · 예: 93500,99000 · 공란이면 기본가', 'prices', optional=True)
            + field('신랑신부 사진 HTTPS 주소', 'wedding_photo', optional=True)
        )
        products.append(f'<section><h2>{escape(product["title"])}</h2>'
                        '<details><summary>상품 수정</summary>'
                        + form('save_product', identity + product_fields(product), '상품 수정 저장')
                        + '</details><details><summary>행사별 주문 링크 발급</summary>'
                        + form('issue_offers', identity + event_fields, '링크 발급') + '</details></section>')

    offers = []
    for row in connection.execute('SELECT * FROM shop_offers ORDER BY rowid DESC LIMIT 100'):
        offer = json.loads(row['details'])
        url = f'{origin}/shop/order/{row["id"]}'
        status = '판매 중' if row['active'] else '판매 중지'
        if row['active'] and datetime.fromisoformat(offer['event_time']) <= datetime.now(SEOUL):
            status = '행사 마감'
        offers.append(f'<li><a href="{escape(url)}">{escape(offer["title"])}</a> · '
                      f'{row["price"]:,}원 · {status}<br><small>{escape(url)}</small>'
                      + form('change_offer', hidden('offer_id', row['id']) + hidden('active', 0 if row['active'] else 1),
                             '판매 중지' if row['active'] else '판매 재개') + '</li>')

    orders = []
    for row in connection.execute('''SELECT o.* FROM invitation_orders o
            JOIN shop_offers s ON s.id=o.job
            ORDER BY (o.state='requesting') DESC, o.created DESC LIMIT 200'''):
        order = json.loads(row['details'])
        details = [
            ('주문번호', row['id']), ('접수 시각', row['created'][:16].replace('T', ' ')),
            ('판매 링크', f'{origin}/shop/order/{row["job"]}'),
            ('원결제금액', f'{order["amount"]:,}원'),
            ('페이앱 결제번호', row['payment_number'] or '미확인'),
            ('주문자', order['buyer'] + ' · ' + order['buyer_phone']),
            ('보내시는 분', order['sender']),
            ('선택 사항', '\n'.join(f'{key} · {value}' for key, value in order['options'])),
            ('직접 입력 문구', order['message'] or '없음'),
            ('설치 장소', order['venue'] + ' · ' + order['address']),
            ('행사 시간', order['event_time'][:16].replace('T', ' ')),
        ]
        events = connection.execute('SELECT * FROM payment_events WHERE order_id=? ORDER BY received,event_key',
                                    (row['id'],)).fetchall()
        partial_total = sum(event['amount'] for event in events if event['kind'] == 'partial')
        if any(event['kind'] == 'cancel' for event in events):
            details.append(('승인취소 금액', f'{order["amount"]:,}원 (전체)'))
        elif partial_total:
            details.extend([('부분취소 합계', f'{partial_total:,}원'),
                            ('남은 결제금액', f'{order["amount"] - partial_total:,}원')])
        elif row['state'] in ('partial_cancel', 'cancelled'):
            details.append(('취소 금액', '이전 기록은 금액 확인 필요 · 페이앱에서 대조'))
        for event in events:
            label = {'partial': '부분취소', 'cancel': '승인취소', 'request_cancel': '요청취소',
                     'paid': '결제완료', 'waiting': '결제대기'}[event['kind']]
            details.append(('페이앱 통보', f'{label} · {event["payment_number"]} · {event["amount"]:,}원 · {event["provider_time"]}'))
        for check in connection.execute('SELECT * FROM payment_checks WHERE order_id=? ORDER BY id', (row['id'],)):
            details.append(('관리자 대조 기록', f'{check["checked"][:16]} · {check["payment_number"]} · {check["note"]}'))
        content = ''.join(f'<dt>{key}</dt><dd>{escape(value)}</dd>' for key, value in details)
        check_form = ''
        if row['state'] in ('requesting', 'waiting', 'failed'):
            check_form = ('<p>페이앱 판매자 결제현황에서 주문번호(연동 변수), 접수 시각, 전화번호와 원금액을 대조하세요. '
                          '결제번호를 기록한 후 페이앱에 통보 재전송을 요청하세요. '
                          '이 기록만으로 결제 완료 처리하지 않습니다. 결과가 불명확하면 새 결제를 요청하지 마세요.</p>'
                          + form('record_payment_check', hidden('order_id', row['id'])
                                 + field('페이앱 원결제번호', 'payment_number', row['payment_number'] or '')
                                 + field('페이앱 원결제금액(원)', 'amount', input_type='number')
                                 + field('페이앱 주문자 전화번호', 'buyer_phone')
                                 + field('페이앱 확인 상태·대조 근거·재전송 요청 결과', 'note', multiline=True)
                                 + '<label><input type="checkbox" name="compared" value="yes" required>'
                                   '페이앱 원거래와 주문을 직접 대조했습니다</label>', '대조 기록 저장'))
        orders.append(f'<li id="order-{row["id"]}" data-shop-order="{row["id"]}" data-queues="{",".join(shop_dashboard.categories(connection,row))}"><details><summary>{escape(order["title"])} · {order["amount"]:,}원 · '
                      f'{STATES.get(row["state"], "확인 필요")} · 주문 {row["id"]}</summary>'
                      f'<dl>{content}</dl>{check_form}' + order_ops.controls(connection, row, path) + '</details></li>')

    notification_settings = ('<section><h2>직원 주문 알림</h2>'
        + form('save_notifications', field('알림 받을 직원 휴대폰 · 쉼표 또는 줄바꿈으로 구분', 'staff_phones',
                                           '\n'.join(order_ops.phones(connection)), multiline=True, optional=True), '직원 알림 번호 저장')
        + '<p>결제 확인 후 직원·주문자에게 문구 샘플을 첨부합니다. 배송 상태 변경도 알립니다. '
          '실제 MMS 발송은 발신번호 등록과 발송 설정이 필요합니다. 결과 불명인 알림은 자동 재발송하지 않습니다.</p>'
        + ('<p>실제 알림 발송 활성화</p>' if order_messages.configured() else '<p>실제 알림 발송 꺼짐 또는 설정 미완료 · 대기열만 보관</p>')
        + ('<p>직원 알림 번호가 없습니다.</p>' if not order_ops.phones(connection) else '') + '</section>')

    return ('''<!doctype html><html lang="ko"><meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1"><title>벨라비타 쇼핑몰 관리</title>
      <style>body{background:#f5f5ef;color:#243329;font:16px/1.5 system-ui;margin:0}
      main{max-width:850px;margin:auto;padding:24px}section{background:white;border:1px solid #dadfd6;
      padding:18px;margin:14px 0;border-radius:14px}h1,h2{letter-spacing:-.04em}h2{font-size:1.2em}
      label,input,textarea,select{display:block;box-sizing:border-box;width:100%}
      label{margin:10px 0;font-weight:600}input,textarea,select{font:inherit;padding:8px;border:1px solid #aab5aa;border-radius:6px}
      input[type=hidden]{display:none}button{font:inherit;background:#284d39;color:white;border:0;border-radius:7px;padding:8px 12px}
      input[type=checkbox]{display:inline;width:auto}
      a{color:#204c36}summary{cursor:pointer;padding:6px 0}small{overflow-wrap:anywhere}li{margin-bottom:12px}
      dd{white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 8px}dt{font-weight:bold}</style>
      <main><h1>벨라비타 쇼핑몰 관리</h1>''' + shop_dashboard.panel(connection) + '''<section><h2>새 상품 등록</h2>'''
      + form('save_product', product_fields({}), '상품 등록') + '</section>' + notification_settings
      + ''.join(products) + '<section><h2>행사별 주문 링크</h2>'
      + ('<ul>' + ''.join(offers) + '</ul>' if offers else '<p>발급된 링크가 없어요.</p>')
      + '</section><section><h2>쇼핑몰 주문·결제 내역</h2>'
      + '<p>결제 요청 확인 필요 주문은 페이앱 대조 후 통보 재전송으로 확인합니다. 취소는 페이앱 판매자 화면에서 실행합니다.</p>'
      + ('<ul>' + ''.join(orders) + '</ul>' if orders else '<p>접수된 주문이 없어요.</p>')
      + '''</section></main><script>
      for (const review of document.querySelectorAll('.shop-photo-review')) {
        const image = review.querySelector('img');
        const button = review.querySelector('button');
        const loaded = () => { button.disabled = !image.naturalWidth; };
        image.addEventListener('load', loaded);
        image.addEventListener('error', loaded);
        loaded();
      }
      </script>''' + shop_dashboard.SCRIPT + '</html>')
