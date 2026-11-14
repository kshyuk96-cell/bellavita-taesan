"""Shop fulfillment and durable notification outbox, committed with payment state."""
import hashlib
import hmac
import json
import os
import re
import threading
import time
from html import escape
from datetime import datetime
from zoneinfo import ZoneInfo

import cash_receipts
import order_handoff
import partial_reviews

SEOUL = ZoneInfo('Asia/Seoul')
STAGES = {'confirmed': '주문 확인', 'shipping': '배송 중', 'delivered': '배송 완료'}
MESSAGE_STATES = {'queued': '발송 대기', 'preparing': '이미지 준비 중', 'sending': '발송 요청 중',
                  'accepted': '발송사 접수', 'delivered': '수신 완료', 'failed': '발송 실패',
                  'uncertain': '발송 결과 대조 필요', 'held': '결제 취소로 발송 보류'}
SAMPLE_PATH = re.compile(r'/shop/sample/([a-f0-9]{32})/([0-9]{10})/([a-f0-9]{64})\.jpg')
DELIVERY_PATH = re.compile(r'/shop/delivery/([a-f0-9]{32})/([0-9]{10})/([a-f0-9]{64})')


def now():
    return datetime.now(SEOUL).isoformat()


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_fulfillment (
        order_id TEXT PRIMARY KEY, stage TEXT NOT NULL, version INTEGER NOT NULL, updated TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_stage_history (
        order_id TEXT NOT NULL, version INTEGER NOT NULL, stage TEXT NOT NULL,
        actor TEXT NOT NULL, changed TEXT NOT NULL, PRIMARY KEY(order_id,version))''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_notification_settings (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), phones TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_messages (
        id TEXT PRIMARY KEY, order_id TEXT NOT NULL, event TEXT NOT NULL, audience TEXT NOT NULL,
        recipient TEXT NOT NULL, state TEXT NOT NULL, image_id TEXT, group_id TEXT, message_id TEXT,
        error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, checked REAL NOT NULL DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_message_checks (
        id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, decision TEXT NOT NULL,
        note TEXT NOT NULL, checked TEXT NOT NULL)''')
    partial_reviews.setup(c)
    cash_receipts.setup(c)
    order_handoff.setup(c)


def phones(c):
    row = c.execute('SELECT phones FROM shop_notification_settings WHERE singleton=1').fetchone()
    text = row['phones'] if row else os.getenv('SHOP_STAFF_PHONES', '')
    return list(dict.fromkeys(re.sub(r'[\s-]', '', part) for part in re.split(r'[,\n]', text) if part.strip()))


def save_settings(c, fields):
    text = fields.get('staff_phones', '')
    numbers = list(dict.fromkeys(re.sub(r'[\s-]', '', part) for part in re.split(r'[,\n]', text) if part.strip()))
    if len(numbers) > 20 or any(not re.fullmatch(r'01[016789]\d{7,8}', number) for number in numbers):
        raise ValueError('직원 휴대폰은 최대 20개, 쉼표 또는 줄바꿈으로 입력해 주세요.')
    c.execute('INSERT INTO shop_notification_settings VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET phones=excluded.phones',
              (','.join(numbers),))


def queue(c, order_id, event):
    order = json.loads(c.execute('SELECT details FROM invitation_orders WHERE id=?', (order_id,)).fetchone()[0])
    recipients = [('customer', order['buyer_phone'])] + [('staff', phone) for phone in phones(c)]
    for audience, phone in recipients:
        key = hashlib.sha256(f'{order_id}:{event}:{phone}'.encode()).hexdigest()
        # One handset receives one message even when buyer and staff are the same person.
        c.execute('''INSERT OR IGNORE INTO shop_messages
            (id,order_id,event,audience,recipient,state,created) VALUES (?,?,?,?,?,'queued',?)''',
                  (key, order_id, event, audience, phone, now()))


def on_payment(c, order_id, state, fields):
    row = c.execute('''SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?''',
                    (order_id,)).fetchone()
    if not row:
        return
    if state == 'paid':
        created = c.execute("INSERT OR IGNORE INTO shop_fulfillment VALUES (?,'confirmed',1,?)", (order_id, now())).rowcount
        if created:
            c.execute("INSERT INTO shop_stage_history VALUES (?,1,'confirmed','payment',?)", (order_id, now()))
            queue(c, order_id, 'confirmed')
    if state in ('cancelled', 'partial_cancel') and not partial_reviews.allowed(c, row):
        c.execute("""UPDATE shop_messages SET state='held',error='payment_changed' WHERE order_id=? AND state='queued'
                     AND (event IN ('confirmed','shipping','delivered') OR event LIKE 'resumed:%')""",
                  (order_id,))
        if fields.get('pay_state') in ('9', '64', '70', '71', '8', '32'):
            event = ('refund:' if fields.get('orig_mul_no') else 'cancel:') + fields.get('mul_no', '')
            queue(c, order_id, event)
    cash_receipts.on_payment(c, row, fields)
    order_handoff.on_payment(c, row, fields)


def change_stage(c, fields, actor='admin-link'):
    order_id, stage = fields.get('order_id', ''), fields.get('stage', '')
    row = c.execute('''SELECT f.*, o.state,o.details,o.payment_number,o.id FROM shop_fulfillment f
                       JOIN invitation_orders o ON o.id=f.order_id WHERE order_id=?''', (order_id,)).fetchone()
    if not row or not partial_reviews.allowed(c, row) or stage not in STAGES:
        raise ValueError('결제가 확인된 쇼핑몰 주문만 배송 처리할 수 있어요. 취소·부분취소는 먼저 대조해 주세요.')
    if str(row['version']) != fields.get('version'):
        raise ValueError('주문 상태가 변경됐어요. 최신 화면에서 다시 확인해 주세요.')
    if stage == row['stage']:
        return
    if {'confirmed': 'shipping', 'shipping': 'delivered'}.get(row['stage']) != stage:
        raise ValueError('주문 확인 → 배송 중 → 배송 완료 순서로 처리해 주세요.')
    c.execute('UPDATE shop_fulfillment SET stage=?,version=version+1,updated=? WHERE order_id=?', (stage, now(), order_id))
    c.execute('INSERT INTO shop_stage_history VALUES (?,?,?,?,?)',
              (order_id, row['version'] + 1, stage, actor, now()))
    queue(c, order_id, stage)


def signature(kind, order_id, expiry, secret):
    return hmac.new(secret.encode(), f'shop/{kind}/{order_id}/{expiry}'.encode(), hashlib.sha256).hexdigest()


def authorized(kind, order_id, expiry, signed, secret):
    return int(expiry) >= time.time() and hmac.compare_digest(signed, signature(kind, order_id, expiry, secret))


def signed_path(kind, order_id, secret, lifetime=600):
    expiry = str(int(time.time()) + lifetime)
    extension = '.jpg' if kind == 'sample' else ''
    return f'/shop/{kind}/{order_id}/{expiry}/{signature(kind, order_id, expiry, secret)}{extension}'


def retry_message(c, fields):
    # Uncertain requests must first be compared with the provider, never blindly resent.
    if fields.get('checked_failure') != 'yes' or not c.execute(
        "UPDATE shop_messages SET state='queued',error='' WHERE id=? AND state='failed'", (fields.get('message_id', ''),)
    ).rowcount:
        raise ValueError('실패가 확인된 알림만 다시 요청할 수 있어요. 결과 불명은 발송사에서 먼저 대조해 주세요.')


def reconcile_message(c, fields):
    decision, note = fields.get('message_decision'), fields.get('message_note', '').strip()
    if (fields.get('message_compared') != 'yes' or decision not in ('delivered', 'failed')
            or not 1 <= len(note) <= 600):
        raise ValueError('발송사에서 동일 주문번호·알림번호·수신번호의 최종 결과를 대조해 주세요.')
    if not c.execute("UPDATE shop_messages SET state=?,error='' WHERE id=? AND state='uncertain'",
                     (decision, fields.get('message_id', ''))).rowcount:
        raise ValueError('결과 대조가 필요한 알림이 아니에요.')
    c.execute('INSERT INTO shop_message_checks(message_id,decision,note,checked) VALUES (?,?,?,?)',
              (fields['message_id'], decision, note, now()))


def recover(c):
    c.execute("UPDATE shop_messages SET state='queued' WHERE state='preparing'")
    c.execute("UPDATE shop_messages SET state='uncertain',error='worker_restarted' WHERE state='sending'")
    cash_receipts.recover(c)


def controls(c, row, path, *, staff=False):
    fulfillment = c.execute('SELECT * FROM shop_fulfillment WHERE order_id=?', (row['id'],)).fetchone()
    if not fulfillment:
        return '<p>결제 확인 후 주문·배송 관리가 시작됩니다.</p>' + order_handoff.panel(c, row, path)
    stage, version = fulfillment['stage'], fulfillment['version']
    image = signed_path('sample', row['id'], os.environ['TAESAN_WEBHOOK_SECRET'])
    content = f'<h3>주문·배송: {STAGES[stage]}</h3><img src="{escape(image)}" alt="고객 입력 문구 샘플" style="width:100%;max-width:320px;height:auto">'
    next_stage = {'confirmed': 'shipping', 'shipping': 'delivered'}.get(stage)
    if next_stage and partial_reviews.allowed(c, row):
        button_label = '배송 중으로 변경' if next_stage == 'shipping' else '배송 완료로 변경'
        content += (f'<form method="post" action="{escape(path)}">'
                    '<input type="hidden" name="action" value="change_stage">'
                    f'<input type="hidden" name="order_id" value="{row["id"]}">'
                    f'<input type="hidden" name="version" value="{version}">'
                    f'<input type="hidden" name="stage" value="{next_stage}">'
                    f'<button>{button_label}</button></form>')
    if not partial_reviews.allowed(c, row):
        content += '<p>결제 취소·부분취소로 배송 처리를 보류합니다.</p>'
    for history in c.execute('SELECT * FROM shop_stage_history WHERE order_id=? ORDER BY version', (row['id'],)):
        content += f'<p>{escape(history["changed"][:16])} · {STAGES[history["stage"]]} · {escape(history["actor"])}</p>'
    if staff:
        return content
    delivery = signed_path('delivery', row['id'], os.environ['TAESAN_WEBHOOK_SECRET'], 86400)
    content += f'<p><a href="{escape(delivery)}">직원용 주문·배송 링크 (24시간)</a></p>'
    content += partial_reviews.panel(c, row, path)
    content += '<h3>알림 이력</h3>'
    for message in c.execute('SELECT * FROM shop_messages WHERE order_id=? ORDER BY created,id', (row['id'],)):
        audience = '직원' if message['audience'] == 'staff' else '주문자'
        content += f'<p>{audience} · {escape(message["recipient"])} · {escape(message["event"])} · {MESSAGE_STATES[message["state"]]}</p>'
        if message['state'] == 'failed':
            content += (f'<form method="post" action="{escape(path)}">'
                        '<input type="hidden" name="action" value="retry_message">'
                        f'<input type="hidden" name="message_id" value="{message["id"]}">'
                        '<label><input type="checkbox" name="checked_failure" value="yes" required>발송사에서 실패를 확인했습니다</label>'
                        '<button>실패 알림 재요청</button></form>')
        if message['state'] == 'uncertain':
            content += (f'<form method="post" action="{escape(path)}">'
                        '<input type="hidden" name="action" value="reconcile_message">'
                        f'<input type="hidden" name="message_id" value="{message["id"]}">'
                        f'<p>발송사 대조용 알림번호: {message["id"]}</p>'
                        '<label>발송사 확인 결과<select name="message_decision"><option value="delivered">수신 완료 확인</option><option value="failed">발송 실패 확인</option></select></label>'
                        '<label>발송사 대조 근거<textarea name="message_note" required maxlength="600"></textarea></label>'
                        '<label><input type="checkbox" name="message_compared" value="yes" required>주문·알림번호·수신번호와 최종 결과를 대조했습니다</label>'
                        '<button>알림 대조 결과 저장</button></form>')
    for check in c.execute('''SELECT a.* FROM shop_message_checks a JOIN shop_messages m ON m.id=a.message_id
                             WHERE m.order_id=? ORDER BY a.id''', (row['id'],)):
        content += '<p>알림 대조 기록: ' + escape(check['checked'][:16] + ' · ' + check['decision'] + ' · ' + check['note']) + '</p>'
    content += '<h3>현금영수증</h3><p>' + escape(cash_receipts.status(c, row)) + '</p>'
    latest = c.execute('SELECT * FROM shop_cash_receipts WHERE order_id=? ORDER BY generation DESC LIMIT 1', (row['id'],)).fetchone()
    if latest and latest['state'] in ('uncertain', 'failed', 'held'):
        content += (f'<form method="post" action="{escape(path)}"><input type="hidden" name="action" value="reconcile_receipt">'
                    f'<input type="hidden" name="order_id" value="{row["id"]}">'
                    f'<input type="hidden" name="generation" value="{latest["generation"]}">'
                    '<label>페이앱 대조 결과<select name="receipt_decision"><option value="issued">발급 확인</option><option value="cancelled">취소 확인</option></select></label>'
                    '<label>발행번호<input name="cashstno" required></label><label>승인번호<input name="receipt_no" required></label>'
                    '<label>페이앱 영수증 금액<input name="receipt_amount" type="number" required></label>'
                    '<label>대조 근거<textarea name="receipt_note" required maxlength="600"></textarea></label>'
                    '<label><input type="checkbox" name="receipt_compared" value="yes" required>페이앱 원본 발급·취소 내역과 대조했습니다</label>'
                    '<button>영수증 대조 기록</button></form>')
    for check in c.execute('SELECT * FROM shop_receipt_checks WHERE order_id=? ORDER BY id', (row['id'],)):
        content += '<p>영수증 대조 기록: ' + escape(check['checked'][:16] + ' · ' + check['decision'] + ' · ' + check['note']) + '</p>'
    content += order_handoff.panel(c, row, path)
    return content


def delivery_page(c, row, path):
    data = json.loads(row['details'])
    return ('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>벨라비타 주문·배송</title><style>body{font:16px/1.6 system-ui;margin:20px;overflow-wrap:anywhere}button{font:inherit;padding:12px}</style>'
            '<main><h1>주문·배송 관리</h1><p>' + escape(data['title']) + '</p><p>'
            + escape(data['venue'] + ' · ' + data['address']) + '</p><p>주문번호 ' + row['id'] + '</p>'
            + controls(c, row, path, staff=True) + '</main></html>')


def customer_receipt(c, row):
    fulfillment = c.execute('SELECT stage FROM shop_fulfillment WHERE order_id=?', (row['id'],)).fetchone()
    if not fulfillment:
        return None
    payment = {'paid': '결제가 완료됐어요.', 'partial_cancel': '결제 금액의 일부가 취소됐어요.', 'cancelled': '결제가 취소됐어요.'}.get(row['state'], '결제를 확인하고 있어요.')
    image = signed_path('sample', row['id'], os.environ['TAESAN_WEBHOOK_SECRET'])
    return ('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>벨라비타 주문 상태</title><style>body{font:16px/1.6 system-ui;margin:20px;overflow-wrap:anywhere}</style>'
            '<main><h1>벨라비타 주문 상태</h1><p>' + payment + '</p><p>주문번호 ' + row['id']
            + '</p><p>주문 확인 → 배송 중 → 배송 완료</p><h2>' + STAGES[fulfillment['stage']] + '</h2>'
            + '<p>현금영수증: ' + escape(cash_receipts.status(c, row)) + '</p>'
            + f'<img src="{escape(image)}" alt="입력하신 주문 문구 샘플" style="width:100%;max-width:450px;height:auto"></main></html>')


def start(db, root):
    import order_messages
    with db() as c:
        recover(c)
    def loop():
        while True:
            for operation in (lambda: order_messages.process_one(db, root), lambda: cash_receipts.process_one(db)):
                try:
                    operation()
                except Exception as error:
                    print('쇼핑몰 알림·영수증 처리 확인 필요:', type(error).__name__, flush=True)
            time.sleep(3)
    threading.Thread(target=loop, daemon=True).start()
