"""Send an order alert once, retaining uncertain results for operator review."""
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from urllib.request import Request, build_opener

from invitation import NoRedirect, https_url


def configured():
    return all(os.getenv(key) for key in ('SOLAPI_API_KEY', 'SOLAPI_API_SECRET', 'SOLAPI_FROM'))


def send(phone, message, reference):
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
    if result.get('failedMessageList') or not result.get('groupInfo', {}).get('groupId'):
        raise ValueError('문자 접수 확인 필요')
    return result['groupInfo']['groupId']


def send_pending(db, base, sender=send, enabled=configured):
    if not enabled() or not https_url(base):
        return False
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('''SELECT o.id,o.order_number,o.details,n.owner,n.phone FROM order_operations o
            JOIN invitation_orders i ON i.id=replace(o.id,'-','') AND i.state='paid'
            JOIN order_staff_notify n ON n.center='구로' JOIN staff s ON s.owner=n.owner
            LEFT JOIN order_sms m ON m.order_id=o.id AND m.owner=n.owner
            WHERE o.kind='payapp' AND o.state='saved' AND m.owner IS NULL ORDER BY o.created LIMIT 1''').fetchone()
        if not row:
            return False
        c.execute("INSERT INTO order_sms VALUES (?,?,'확인 필요','')", (row['id'], row['owner']))
    event = json.loads(row['details'])
    date = ' '.join(filter(None, (event.get('event_date'), event.get('event_time')))) or '확인 필요'
    message = f'벨라비타 새 주문 {row["order_number"]}번\n예식 {date}\n{base}/staff-orders?order={row["id"].replace("-", "")}'
    try:
        receipt = sender(row['phone'], message, hashlib.sha256((row['id'] + ':' + row['owner']).encode()).hexdigest()[:24])
    except Exception:
        return True  # Check the provider before resending a possibly accepted request.
    with db() as c:
        c.execute("UPDATE order_sms SET state='접수 완료',receipt=? WHERE order_id=? AND owner=?",
                  (receipt, row['id'], row['owner']))
    return True
