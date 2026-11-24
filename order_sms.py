"""Paid-order notifications with an at-most-once Sendon/SOLAPI outbox.

Provider acceptance is recorded separately from delivery. An uncertain request is
never retried automatically, because SOLAPI may have accepted it before timeout.
Only payment transitions enqueue messages; there is no historical-order scan.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.request import Request, build_opener

from invitation import NoRedirect, https_url

PHONE = re.compile(r'01[016789][0-9]{7,8}')


def provider_name():
    selected = os.getenv('INVITATION_SMS_PROVIDER', '').strip().lower()
    if selected:
        return selected if selected in ('sendon', 'solapi') else ''
    # Prefer the user's Sendon account once any Sendon setting is supplied.
    return 'sendon' if any(os.getenv(k) for k in ('SENDON_ID','SENDON_API_KEY','SENDON_FROM')) else 'solapi'


def configured():
    keys = {'sendon':('SENDON_ID','SENDON_API_KEY','SENDON_FROM'),
            'solapi':('SOLAPI_API_KEY','SOLAPI_API_SECRET','SOLAPI_FROM')}.get(provider_name())
    return bool(keys and all(os.getenv(key) for key in keys))


def alimtalk_template(audience):
    key = 'SENDON_STAFF_ALIM_TEMPLATE_ID' if audience == 'staff' else 'SENDON_CUSTOMER_ALIM_TEMPLATE_ID'
    return os.getenv(key) or (os.getenv('SENDON_ALIM_TEMPLATE_ID') if audience == 'customer' else '')


def alimtalk_configured(audience='customer'):
    return (provider_name() == 'sendon' and configured() and
            bool(os.getenv('SENDON_ALIM_PROFILE_ID')) and bool(alimtalk_template(audience)))


def enabled():
    return os.getenv('INVITATION_ORDER_SMS_ENABLED') == '1'


def phone(value):
    digits = re.sub(r'[^0-9]', '', str(value or ''))
    return digits if PHONE.fullmatch(digits) else ''


def setup(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS invitation_notice_outbox (
        id TEXT PRIMARY KEY, order_id TEXT NOT NULL, audience TEXT NOT NULL,
        phone TEXT NOT NULL, details TEXT NOT NULL, state TEXT NOT NULL,
        receipt TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
        accepted REAL, error TEXT NOT NULL DEFAULT '',
        UNIQUE(order_id,audience,phone))''')


def staff_phones(connection=None):
    recipients = {phone(value) for value in os.getenv('INVITATION_STAFF_NOTIFY_PHONES', '').split(',')}
    if connection is not None:
        # Preserve the previously approved Guro staff subscription list.
        try:
            recipients.update(phone(row[0]) for row in connection.execute(
                "SELECT n.phone FROM order_staff_notify n JOIN staff s ON s.owner=n.owner WHERE n.center='구로'"))
        except __import__('sqlite3').OperationalError:
            pass
    return sorted(recipients - {''})


def enqueue_paid(connection, row):
    """Called in the authenticated PayApp paid-transition transaction only."""
    if not enabled():
        return 0
    setup(connection)
    details = json.loads(row['details'])
    targets = [('customer', phone(details.get('buyer_phone')))]
    targets.extend(('staff', value) for value in staff_phones(connection))
    count = 0
    for audience, recipient in targets:
        if not recipient:
            continue
        key = hashlib.sha256((row['id'] + ':' + audience + ':' + recipient).encode()).hexdigest()
        cursor = connection.execute('''INSERT OR IGNORE INTO invitation_notice_outbox
            (id,order_id,audience,phone,details,state,created) VALUES (?,?,?,?,?,'pending',?)''',
            (key, row['id'], audience, recipient, json.dumps(details, ensure_ascii=False), time.time()))
        count += cursor.rowcount
    return count


def send_solapi(phone, message, reference):
    date = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    salt = secrets.token_hex(20)
    signature = hmac.new(os.environ['SOLAPI_API_SECRET'].encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
    data = {'messages': [{'to': phone, 'from': os.environ['SOLAPI_FROM'], 'text': message,
                          'customFields': {'orderNotice': reference}}]}
    headers = {'Content-Type': 'application/json', 'Authorization':
        f'HMAC-SHA256 apiKey={os.environ["SOLAPI_API_KEY"]}, date={date}, salt={salt}, signature={signature}'}
    request = Request('https://api.solapi.com/messages/v4/send-many/detail', json.dumps(data).encode(), headers)
    with build_opener(NoRedirect()).open(request, timeout=12) as response:
        result = json.load(response)
    group = result.get('groupInfo', {})
    if result.get('failedMessageList') or not group.get('groupId'):
        raise ValueError('문자 접수 확인 필요')
    # A group ID only means provider acceptance. It is NOT proof of delivery.
    return group['groupId']



def send_sendon(phone, message, reference):
    """Documented Sendon v2 SMS API; HTTP 200 alone is not success.

    Official refs: sdk.sendon.io/reference/msgcontroller_send,
    /2-기본-설정하기 and /4-api-응답-코드-가이드. Sendon account ID
    and API key are HTTP Basic credentials. API-key IP allowlisting and
    approved sender registration are configured in the Sendon console.
    """
    sender = re.sub(r'[^0-9]', '', os.environ['SENDON_FROM'])
    if not re.fullmatch(r'0[0-9]{8,10}', sender):
        raise ValueError('센드온 발신번호 설정 확인 필요')
    # UTF-8 is conservative for the 90-byte SMS ceiling; long transactional
    # notices explicitly use LMS with the documented required title.
    is_short = len(message.encode('utf-8')) <= 90
    if len(message.encode('utf-8')) > 2000:
        raise ValueError('센드온 장문 알림 길이 확인 필요')
    data = {'type':'SMS' if is_short else 'LMS', 'from':sender,
            'to':[phone], 'message':message, 'isAd':False, 'useCredit':False}
    if not is_short:
        data['title'] = '벨라비타 주문 알림'
    token = base64.b64encode((os.environ['SENDON_ID'] + ':' + os.environ['SENDON_API_KEY']).encode()).decode('ascii')
    request = Request('https://api.sendon.io/v2/messages/sms',
                      json.dumps(data, ensure_ascii=False).encode('utf-8'),
                      {'Content-Type':'application/json', 'Authorization':'Basic ' + token})
    with build_opener(NoRedirect()).open(request, timeout=12) as response:
        result = json.load(response)
    receipt = result.get('data', {}).get('groupId') if isinstance(result.get('data'), dict) else None
    if str(result.get('code')) != '200' or not isinstance(receipt, str) or not receipt:
        # Keep the provider's arbitrary response text out of logs and exceptions:
        # it can contain recipient or credential details.
        raise ValueError('센드온 문자 접수 확인 필요')
    return receipt


def send_sendon_alimtalk(phone, variables, fallback_message, reference, audience='customer'):
    """Send an approved transactional AlimTalk, with LMS only as fallback."""
    if not alimtalk_configured(audience):
        raise ValueError('센드온 알림톡 채널·템플릿 설정 확인 필요')
    recipient = {'phone': phone, 'variables': variables}
    data = {
        'sendProfileId': os.environ['SENDON_ALIM_PROFILE_ID'],
        'templateId': alimtalk_template(audience),
        'to': [recipient],
        'useCredit': False,
        'fallback': {
            'fallbackType': 'CUSTOM',
            'custom': {
                'type': 'LMS',
                'senderNumber': re.sub(r'[^0-9]', '', os.environ['SENDON_FROM']),
                'isAd': False,
                'title': '벨라비타 주문 알림',
                'message': fallback_message,
            },
        },
    }
    token = base64.b64encode((os.environ['SENDON_ID'] + ':' + os.environ['SENDON_API_KEY']).encode()).decode('ascii')
    request = Request('https://api.sendon.io/v2/messages/kakao/alim-talk',
                      json.dumps(data, ensure_ascii=False).encode('utf-8'),
                      {'Content-Type':'application/json', 'Authorization':'Basic ' + token,
                       'X-Bellavita-Reference': reference})
    with build_opener(NoRedirect()).open(request, timeout=12) as response:
        result = json.load(response)
    receipt = result.get('data', {}).get('groupId') if isinstance(result.get('data'), dict) else None
    if str(result.get('code')) != '200' or not isinstance(receipt, str) or not receipt:
        raise ValueError('센드온 알림톡 접수 확인 필요')
    return receipt


def send(phone, message, reference):
    provider = provider_name()
    if provider == 'sendon':
        return send_sendon(phone, message, reference)
    if provider == 'solapi':
        return send_solapi(phone, message, reference)
    raise ValueError('문자 제공사 설정 확인 필요')


def message_for(row, base):
    details = json.loads(row['details'])
    order_id = row['order_id']
    title = str(details.get('title') or '쌀빛그림화환')[:180]
    when = str(details.get('event_time') or '')[:50].replace('T', ' ')
    try:
        when = datetime.fromisoformat(str(details.get('event_time'))).strftime('%Y.%m.%d %H:%M')
    except (ValueError, TypeError):
        pass
    venue = str(details.get('venue') or '')[:140]
    if row['audience'] == 'customer':
        header = '벨라비타 결제 확인 안내'
        tail = '감사합니다. 직원이 주문 내용을 확인하고 있습니다.'
        link = base + '/invitation-orders/' + order_id + '/receipt'
    else:
        header = '벨라비타 새 결제 주문 · 승인 대기'
        tail = 'ERP 초대장 주문관리에서 내용을 확인하고 주문입력해 주세요.'
        link = base + '/staff-orders?order=' + order_id
    try:
        amount = f"{int(details.get('amount', 0)):,}원"
    except (TypeError, ValueError):
        amount = '금액 확인 필요'
    return '\n'.join(filter(None, (header, '주문 ' + order_id[-8:], title, amount,
                                  when, venue, tail, link)))


def alimtalk_variables(row, base):
    details = json.loads(row['details'])
    try:
        amount = f"{int(details.get('amount', 0)):,}원"
    except (TypeError, ValueError):
        amount = '금액 확인 필요'
    try:
        when = datetime.fromisoformat(str(details.get('event_time'))).strftime('%Y.%m.%d %H:%M')
    except (ValueError, TypeError):
        when = str(details.get('event_time') or '일시 확인 필요')[:50].replace('T', ' ')
    staff = row['audience'] == 'staff'
    return {
        '#{주문번호}': str(row['order_id'])[-8:],
        '#{행사명}': str(details.get('title') or '쌀빛그림화환')[:180],
        '#{금액}': amount,
        '#{행사일시}': when,
        '#{장소}': str(details.get('venue') or '장소 확인 필요')[:140],
        '#{주문확인링크}': (base.rstrip('/') + '/staff-orders?order=' + row['order_id'] if staff else
                         base.rstrip('/') + '/invitation-orders/' + row['order_id'] + '/receipt'),
    }


def send_pending(db, base, sender=None, enabled=None):
    """Atomically claim one new paid-order notice; independent of Sheet writes.

    ``enabled`` is retained as a test seam from the old worker; it controls
    provider readiness only. The explicit order-notification feature flag still
    applies, and no old order_operations row is scanned or enqueued here.
    """
    provider_ready = enabled if enabled is not None else configured
    if not globals()['enabled']() or not provider_ready() or not https_url(base):
        return False
    with db() as connection:
        setup(connection)
        connection.execute('BEGIN IMMEDIATE')
        cutoff = time.time() - 72 * 3600
        connection.execute("UPDATE invitation_notice_outbox SET state='expired' WHERE state='pending' AND created<?", (cutoff,))
        row = connection.execute('''SELECT q.* FROM invitation_notice_outbox q
            JOIN invitation_orders i ON i.id=q.order_id AND i.state='paid'
            WHERE q.state='pending' ORDER BY q.created,q.id LIMIT 1''').fetchone()
        if row is None:
            return False
        # Commit before the network call: timeout/process death is uncertain,
        # not a reason to send a second message automatically.
        connection.execute("UPDATE invitation_notice_outbox SET state='uncertain' WHERE id=? AND state='pending'", (row['id'],))
    try:
        message = message_for(row, base.rstrip('/'))
        if sender is not None:
            receipt = sender(row['phone'], message, row['id'][:24])
        elif provider_name() == 'sendon':
            receipt = send_sendon_alimtalk(row['phone'], alimtalk_variables(row, base), message,
                                           row['id'][:24], row['audience'])
        else:
            receipt = send(row['phone'], message, row['id'][:24])
        if not isinstance(receipt, str) or not receipt:
            raise ValueError('접수 결과 확인 필요')
    except Exception as error:
        with db() as connection:
            connection.execute("UPDATE invitation_notice_outbox SET error=? WHERE id=?", (type(error).__name__, row['id']))
        return True
    with db() as connection:
        connection.execute("UPDATE invitation_notice_outbox SET state='accepted',receipt=?,accepted=? WHERE id=? AND state='uncertain'",
                           (receipt, time.time(), row['id']))
    return True


def start(db, base):
    def loop():
        while True:
            try:
                send_pending(db, base)
            except Exception as error:
                # Do not log customer details, phones, credentials or message text.
                print('주문 알림 대기열 확인 실패: ' + type(error).__name__, flush=True)
            time.sleep(3)
    threading.Thread(target=loop, daemon=True, name='invitation-order-notices').start()
