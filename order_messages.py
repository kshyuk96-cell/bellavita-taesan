"""SOLAPI MMS adapter. A provider acceptance is distinct from handset delivery."""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

import order_ops
import order_sample
import alimtalk
from blogpay import NoRedirect


def configured(channel=None):
    base=os.getenv('SHOP_MESSAGES_ENABLED')=='1' and all(os.getenv(k) for k in ('SOLAPI_API_KEY','SOLAPI_API_SECRET'))
    if not base:return False
    mms=bool(os.getenv('SOLAPI_SENDER'))
    ata=alimtalk.configured()
    return (mms or ata) if channel is None else ata if channel=='alimtalk' else mms


def api(path, body=None):
    date = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    salt = secrets.token_hex(16)
    signature = hmac.new(os.environ['SOLAPI_API_SECRET'].encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
    auth = f'HMAC-SHA256 apiKey={os.environ["SOLAPI_API_KEY"]}, date={date}, salt={salt}, signature={signature}'
    request = Request('https://api.solapi.com' + path,
                      json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                      {'Authorization': auth, 'Content-Type': 'application/json; charset=utf-8'})
    with build_opener(NoRedirect).open(request, timeout=12) as response:
        return json.loads(response.read(200_000).decode())


def text_for(job, order,delivery_link=None):
    data = json.loads(order['details'])
    status = order_ops.STAGES.get(job['event'], '부분취소 잔액 대조 후 재개' if job['event'].startswith('resumed:') else '결제 일부 취소' if job['event'].startswith('refund:') else '결제 취소')
    text = f'[벨라비타] {status}\n주문번호 {order["id"]}\n{data["title"]}\n{data["venue"]}\n{data["event_time"][:16].replace("T", " ")}\n문구 확인용 샘플 이미지를 첨부합니다.'
    if job['audience'] == 'staff':
        origin = os.getenv('TAESAN_PUBLIC_URL', '').rstrip('/')
        text += '\n주문·배송 관리: ' + origin + (delivery_link or order_ops.signed_path('delivery', order['id'], os.environ['TAESAN_WEBHOOK_SECRET'], 86400))
    else:
        text += '\n주문 상태: ' + os.getenv('TAESAN_PUBLIC_URL', '').rstrip('/') + f'/invitation-orders/{order["id"]}/receipt'
    return text


def poll(db):
    with db() as c:
        job = c.execute("SELECT * FROM shop_messages WHERE state='accepted' AND checked<? ORDER BY checked LIMIT 1",
                        (time.time()-60,)).fetchone()
        if not job:
            return
        c.execute('UPDATE shop_messages SET checked=? WHERE id=?', (time.time(), job['id']))
    result = api('/messages/v4/list?' + urlencode({'criteria': 'messageId', 'cond': 'eq', 'value': job['message_id']}))
    message = result.get('messageList', {}).get(job['message_id'])
    if not message or message.get('groupId') != job['group_id'] or message.get('to') != job['recipient']:
        return
    code = str(message.get('statusCode', ''))
    if not code.isdigit():
        return
    state = 'delivered' if code == '4000' else 'accepted' if code in ('2000', '3000') else 'failed'
    with db() as c:
        c.execute("UPDATE shop_messages SET state=?,error=? WHERE id=? AND state='accepted'",
                  (state, '' if state != 'failed' else 'provider_' + code[:12], job['id']))


def process_one(db, root):
    if not configured():
        return
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        channels=[channel for channel in ('mms','alimtalk') if configured(channel)]
        job=c.execute("SELECT * FROM shop_messages WHERE state='queued' AND channel IN ("+','.join('?' for _ in channels)+") ORDER BY created,id LIMIT 1",channels).fetchone()
        if job:
            if job['audience'] == 'staff' and job['recipient'] not in order_ops.phones(c):
                c.execute("UPDATE shop_messages SET state='held',error='staff_recipient_removed' WHERE id=?", (job['id'],))
                return
            order = c.execute('SELECT * FROM invitation_orders WHERE id=?', (job['order_id'],)).fetchone()
            if not order_ops.partial_reviews.allowed(c, order) and (job['event'] in order_ops.STAGES or job['event'].startswith('resumed:')):
                c.execute("UPDATE shop_messages SET state='held',error='payment_changed' WHERE id=?", (job['id'],))
                return
            c.execute("UPDATE shop_messages SET state='preparing' WHERE id=?", (job['id'],))
    if not job:
        return poll(db)
    try:
        image_id = job['image_id']
        if job['channel']=='mms' and not image_id:
            image = order_sample.sample(root, order).read_bytes()
            uploaded = api('/storage/v1/files', {'file': base64.b64encode(image).decode(), 'type': 'MMS',
                                                'name': 'bellavita-order.jpg'})
            image_id = uploaded.get('fileId')
            if not image_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', image_id):
                raise ValueError('invalid_image_id')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            current = c.execute('SELECT * FROM invitation_orders WHERE id=?', (job['order_id'],)).fetchone()
            if not order_ops.partial_reviews.allowed(c, current) and (job['event'] in order_ops.STAGES or job['event'].startswith('resumed:')):
                c.execute("UPDATE shop_messages SET state='held',error='payment_changed' WHERE id=?", (job['id'],))
                return
            import shop_dispatch
            tracking=shop_dispatch.link(c,current['id'],os.environ['TAESAN_WEBHOOK_SECRET'])
            payload=json.loads(job['request_json']) if job['request_json'] else (alimtalk.message(job,current,tracking=(os.getenv('TAESAN_PUBLIC_URL','').rstrip('/')+tracking) if job['audience']=='staff' else None) if job['channel']=='alimtalk' else {
                'to':job['recipient'],'from':os.environ['SOLAPI_SENDER'],'type':'MMS',
                'text':text_for(job,current,tracking),'subject':'벨라비타 주문 안내','imageId':image_id,
                'customFields':{'orderId':job['order_id'],'notificationId':job['id']}})
            c.execute("UPDATE shop_messages SET state='sending',image_id=?,request_json=? WHERE id=?", (image_id,json.dumps(payload,ensure_ascii=False),job['id']))
    except Exception:
        with db() as c:
            c.execute("UPDATE shop_messages SET state='failed',error='image_preparation_failed' WHERE id=? AND state='preparing'", (job['id'],))
        return
    try:
        result=api('/messages/v4/send-many/detail', {'messages':[payload], 'allowDuplicates':False,'showMessageList':True})
        if result.get('failedMessageList'):
            if result.get('messageList'):
                raise OSError('inconsistent_acceptance')
            state, group, message = 'failed', None, None
        else:
            group = result.get('groupInfo', {}).get('groupId')
            entries = result.get('messageList', [])
            message = entries[0].get('messageId') if len(entries) == 1 else None
            if not group or not message:
                raise OSError('uncertain_acceptance')
            state = 'accepted'
    except HTTPError as error:
        state, group, message = ('failed' if error.code in (400,401,403,404,422) else 'uncertain'), None, None
    except Exception:
        state, group, message = 'uncertain', None, None
    with db() as c:
        c.execute('''UPDATE shop_messages SET state=?,group_id=?,message_id=?,error=?,checked=? WHERE id=?''',
                  (state, group, message, '' if state == 'accepted' else 'provider_check_required', time.time(), job['id']))
