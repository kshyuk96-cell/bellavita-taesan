"""Transactional, leased handoff to an idempotent ledger/production receiver."""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import partial_reviews
import production_files

SEOUL = ZoneInfo('Asia/Seoul')
STATES = {'review': '사진 검토 후 제작 접수', 'pending': '공장 전달 대기', 'leased': '공장 수신 처리 중',
          'acked': '원장·제작 대기열 접수 확인', 'held': '결제·검수 변경으로 제작 보류'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_handoff (
        event_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, kind TEXT NOT NULL,
        payload TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
        consumer TEXT, lease_digest TEXT, lease_until REAL, receipt TEXT, created TEXT NOT NULL,
        acked TEXT)''')
    c.execute('CREATE INDEX IF NOT EXISTS shop_handoff_order ON shop_handoff(order_id,created)')


def payload_for(c, row, kind):
    data = json.loads(row['details'])
    # Factory staff need order/contact data, not cash receipt identifiers or credentials.
    order = {key: data[key] for key in ('title','options','sender','message','buyer','buyer_phone',
                                       'venue','address','event_time','amount')}
    order['id'] = row['id']
    order['offer_id'] = row['job']
    options = dict(order['options'])
    weight = re.search(r'(?<!\d)(10|20|30)\s*(?:kg|㎏|키로|킬로)', options.get('상품 선택', ''), re.I)
    production = {'order_number': row['id'], 'quantity': 1, 'rice_kg': int(weight[1]) if weight else None,
                  'sender': data['sender'],
                  'message': data['message'] or re.sub(r'^\d+\.\s*', '', options.get('축하 문구', '')),
                  'photo': data.get('wedding_preview')}
    refunded = c.execute("SELECT COALESCE(SUM(amount),0) FROM payment_events WHERE order_id=? AND kind='partial'", (row['id'],)).fetchone()[0]
    if row['state'] == 'cancelled':
        refunded = data['amount']
    result = {'schema_version': 2 if kind == 'resume' else 1, 'kind': kind, 'order': order, 'production': production,
            'payment': {'state': row['state'], 'number': row['payment_number'], 'original_amount': data['amount'],
                        'refunded_amount': refunded, 'remaining_amount': data['amount']-refunded}}
    latest = c.execute('SELECT id FROM shop_partial_reviews WHERE order_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
    result['review_revision'] = latest['id'] if latest else 0
    if kind == 'resume':
        check = partial_reviews.approval(c, row)
        if not check:
            raise ValueError('partial_approval_required')
        result['approval'] = {'id': check['id'], 'fingerprint': check['fingerprint'], 'remaining': check['remaining'], 'refunded': check['refunded'], 'decision': 'resume'}
    artifact=production_files.manifest(c,row) if kind in ('ready','resume') else None
    if artifact:
        result['schema_version']=3
        result['artifact']=artifact
    latest=production_files.latest(c,row['id'])
    review=production_files.latest_review(c,row['id'])
    result['artifact_revision']=[latest['version'] if latest else 0,review['id'] if review else 0]
    return result


def enqueue(c, row, kind, event_id, state):
    payload = canonical(payload_for(c, row, kind))
    c.execute('''INSERT OR IGNORE INTO shop_handoff(event_id,order_id,kind,payload,digest,state,created)
                 VALUES (?,?,?,?,?,?,?)''',
              (event_id, row['id'], kind, payload, hashlib.sha256(payload.encode()).hexdigest(), state,
               datetime.now(SEOUL).isoformat()))


def ensure_ready(c, row):
    if row['state'] != 'paid':
        return
    data = json.loads(row['details'])
    reviewed = c.execute('SELECT 1 FROM invitation_photo_reviews WHERE order_id=?', (row['id'],)).fetchone()
    state = 'review' if (data.get('wedding_preview') and not reviewed) or not production_files.allowed(c,row) else 'pending'
    enqueue(c, row, 'ready', row['id'] + ':ready', state)


def on_payment(c, row, fields):
    ensure_ready(c, row)
    if row['state'] in ('partial_cancel', 'cancelled') and not partial_reviews.allowed(c, row):
        c.execute("UPDATE shop_handoff SET state='held' WHERE order_id=? AND kind IN ('ready','resume') AND state!='acked'", (row['id'],))
        # A hold is itself an idempotent event, including when cancellation follows an ACK.
        # A receiver must create a held tombstone even if it has not seen ready yet.
        if fields.get('pay_state') in ('9','64','70','71'):
            key = 'hold:' + fields.get('mul_no', '')
            enqueue(c, row, 'hold', row['id'] + ':' + key, 'pending')


def prepare(c, fields):
    row = c.execute('''SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?''',
                    (fields.get('order_id', ''),)).fetchone()
    if not row or row['state'] != 'paid':
        raise ValueError('결제가 확인된 쇼핑몰 주문만 후속 접수를 준비할 수 있어요.')
    ensure_ready(c, row)


def review_photo(c, fields):
    import checkout
    order_id = fields.get('order_id', '')
    row = c.execute('''SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?''', (order_id,)).fetchone()
    if row and row['state'] == 'partial_cancel' and fields.get('both_faces') == 'yes' and json.loads(row['details']).get('wedding_preview'):
        c.execute('INSERT OR IGNORE INTO invitation_photo_reviews VALUES (?,?)', (order_id, datetime.now(SEOUL).isoformat()))
        return
    if not row or not checkout.review_photo(c, fields):
        raise ValueError('결제 상태와 신랑신부 사진의 두 얼굴을 먼저 확인해 주세요.')
    ensure_ready(c, row)
    if production_files.allowed(c,row):
        c.execute("UPDATE shop_handoff SET state='pending' WHERE order_id=? AND kind='ready' AND state='review'", (order_id,))


def authorize(header):
    secret = os.getenv('SHOP_HANDOFF_SECRET', '')
    return (os.getenv('SHOP_HANDOFF_ENABLED') == '1' and len(secret) >= 32
            and hmac.compare_digest(header.encode(), ('Bearer '+secret).encode()))


def consumer_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', value):
        raise ValueError('invalid_consumer')
    return value


def claim(c, fields):
    consumer = consumer_name(fields.get('consumer'))
    now = time.time()
    c.execute('BEGIN IMMEDIATE')
    # Recheck approval at lease time, including stale refund versions.
    for candidate in c.execute("SELECT * FROM shop_handoff WHERE kind IN ('ready','resume') AND state IN ('pending','leased')").fetchall():
        if not fulfill_event(c, candidate):
            c.execute("UPDATE shop_handoff SET state='held' WHERE event_id=?", (candidate['event_id'],))
    capabilities=fields.get('capabilities',[])
    if not isinstance(capabilities,list) or len(capabilities)>10:raise ValueError('invalid_capabilities')
    supports_resume = 'partial-resume-v1' in capabilities
    supports_files = 'production-artifact-v1' in capabilities
    row = c.execute("""SELECT * FROM shop_handoff WHERE (state='pending' OR (state='leased' AND lease_until<=?))
        AND (kind!='resume' OR ?) AND (json_extract(payload,'$.schema_version')!=3 OR ?) ORDER BY (kind='hold') DESC,created,event_id LIMIT 1""", (now, supports_resume,supports_files)).fetchone()
    if not row:
        return {'job': None}
    token = secrets.token_hex(32)
    until = now + 120
    c.execute("UPDATE shop_handoff SET state='leased',consumer=?,lease_digest=?,lease_until=? WHERE event_id=?",
              (consumer, hashlib.sha256(token.encode()).hexdigest(), until, row['event_id']))
    return {'job': {'event_id': row['event_id'], 'order_id': row['order_id'], 'kind': row['kind'],
                    'payload': json.loads(row['payload']), 'sha256': row['digest'],
                    'lease_token': token, 'lease_until': until, 'consumer': consumer}}


def fulfill_event(c, event):
    order = c.execute('SELECT * FROM invitation_orders WHERE id=?', (event['order_id'],)).fetchone()
    if not production_files.allowed(c,order):return False
    payload=json.loads(event['payload'])
    if production_files.required(order) or production_files.latest(c,order['id']):
        current=production_files.manifest(c,order)
        if payload.get('artifact')!=current:return False
    if event['kind'] == 'ready':
        return order['state'] == 'paid'
    check = partial_reviews.approval(c, order)
    return check is not None and json.loads(event['payload']).get('approval', {}).get('id') == check['id']


def acknowledge(c, fields):
    consumer = consumer_name(fields.get('consumer'))
    event_id, token = fields.get('event_id'), fields.get('lease_token')
    if not isinstance(event_id, str) or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token):
        raise ValueError('invalid_ack')
    refs = fields.get('references')
    if (not isinstance(refs, dict) or set(refs) != {'ledger','production'}
            or any(not isinstance(value, str) or not 1 <= len(value) <= 120 for value in refs.values())):
        raise ValueError('invalid_references')
    c.execute('BEGIN IMMEDIATE')
    row = c.execute('SELECT * FROM shop_handoff WHERE event_id=?', (event_id,)).fetchone()
    if (not row or row['consumer'] != consumer or not row['lease_digest']
            or not hmac.compare_digest(row['lease_digest'], hashlib.sha256(token.encode()).hexdigest())):
        return {'error': 'lease_mismatch'}, 409
    receipt = canonical(refs)
    if row['state'] == 'acked':
        return ({'ok': True}, 200) if row['receipt'] == receipt else ({'error': 'receipt_mismatch'}, 409)
    if row['kind'] in ('ready','resume') and not fulfill_event(c, row):
        c.execute("UPDATE shop_handoff SET state='held' WHERE event_id=?", (event_id,))
        return {'error': 'payment_changed'}, 409
    if row['state'] != 'leased' or row['lease_until'] <= time.time():
        return {'error': 'lease_expired'}, 409
    c.execute("UPDATE shop_handoff SET state='acked',receipt=?,acked=? WHERE event_id=?",
              (receipt, datetime.now(SEOUL).isoformat(), event_id))
    return {'ok': True}, 200


def panel(c, row, path):
    records = c.execute('SELECT * FROM shop_handoff WHERE order_id=? ORDER BY created,event_id', (row['id'],)).fetchall()
    result = '<h3>제작·원장 연결</h3>'
    hidden = f'<input type="hidden" name="order_id" value="{row["id"]}">'
    if not records and row['state'] == 'paid':
        result += (f'<form method="post" action="{escape(path)}">{hidden}'
                   '<input type="hidden" name="action" value="prepare_handoff"><button>후속 접수 준비</button></form>')
    for record in records:
        kind = {'ready':'제작 접수','resume':'잔액 대조 후 재개','hold':'제작 보류 전달'}[record['kind']]
        state_label='제작 파일 검수 대기' if record['state']=='review' and production_files.required(row) else STATES[record['state']]
        result += '<p>' + kind + ' · ' + state_label + '</p>'
        if record['receipt']:
            refs = json.loads(record['receipt'])
            result += '<p>원장 접수번호: ' + escape(refs['ledger']) + '<br>제작 대기열 번호: ' + escape(refs['production']) + '</p>'
    if any(record['state'] == 'review' for record in records) or (row['state'] == 'partial_cancel' and not c.execute('SELECT 1 FROM invitation_photo_reviews WHERE order_id=?', (row['id'],)).fetchone()):
        data = json.loads(row['details'])
        photo = data.get('wedding_preview')
        if photo:
            result += (f'<details class="shop-photo-review"><summary>신랑신부 사진 검토</summary>'
                       f'<img src="{escape(photo["url"])}" alt="주문 제작용 신랑신부 사진" style="width:100%;max-width:320px;height:auto">'
                       f'<form method="post" action="{escape(path)}">{hidden}'
                       '<input type="hidden" name="action" value="review_shop_photo">'
                       '<label><input type="checkbox" name="both_faces" value="yes" required>신랑·신부가 맞고 두 얼굴이 온전히 보입니다</label>'
                       '<button disabled>사진 검토 완료·제작 접수</button></form></details>')
    result += '<p>접수 확인은 공장 대기열에 보관됐다는 뜻이며 출력·제작 완료와 구분합니다.</p>'
    return result
