"""Order-origin outbox. Provider acceptance is not handset delivery."""
import base64
import datetime as dt
import json
import logging
import os
import re
from contextlib import closing
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

STAFF = 'STAFF_NEW_ORDER'
CUSTOMER = 'CUSTOMER_ORDER_RECEIPT'
API = 'https://api.sendon.io/v2/messages/kakao/alim-talk'
logger = logging.getLogger(__name__)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS sendon_order_queue (
        order_id TEXT NOT NULL, template TEXT NOT NULL, recipient TEXT NOT NULL,
        variables TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
        request_id TEXT, result_code TEXT, reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(order_id,template)
    )""")


def enqueue(db_factory, order_id, order, *, customer=False):
    import order_sms
    try:
        with closing(db_factory()) as db, db:
            setup(db)
            recipient = next(iter(order_sms.staff_phones(db)), '')
            when = str(order.get('event_time') or '')[:10]
            title = str(order.get('title') or '벨라비타 상품')
            kind = '근조' if re.search('부고|근조', title) else '결혼' if re.search('예식|결혼', title) else '일반 행사'
            jobs = [(STAFF, recipient, {
                '주문번호':order_id, '행사구분':kind,
                '받는분':order.get('recipient') or '확인 필요', '배송일':when or '확인 필요',
                '배송센터':order.get('delivery_center') or '확인 필요',
                '결제수단':'무통장 입금' if order.get('payment_method') == 'bank_transfer' else '카드·간편결제',
            })]
            if customer:
                jobs.append((CUSTOMER, order.get('buyer_phone') or '', {
                    '고객명':order.get('buyer') or '고객', '주문번호':order_id,
                    '상품명':title, '배송일':when or '확인 필요', '결제상태':'결제 완료',
                }))
            db.executemany("""INSERT INTO sendon_order_queue
                (order_id,template,recipient,variables,created_at) VALUES(?,?,?,?,?)
                ON CONFLICT(order_id,template) DO NOTHING""",
                [(order_id, code, number, json.dumps(values, ensure_ascii=False), now())
                 for code, number, values in jobs])
    except Exception:
        logger.error('Sendon order notification queue write failed')


def live_enabled(env):
    return env.get('SENDON_LIVE_ENABLED') == 'true' and all(
        env.get(key) for key in ('SENDON_ID', 'SENDON_API_KEY'))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def transmit(template, recipient, variables, *, env=None, open_request=None):
    env = os.environ if env is None else env
    if not re.fullmatch(r'01[016789][0-9]{7,8}', recipient):
        return 'failed', '', 'INVALID_PHONE', '휴대폰 번호 확인 필요'
    if not live_enabled(env):
        return 'sent', '', 'SIMULATED', '모의 발송'
    auth = base64.b64encode(f"{env['SENDON_ID']}:{env['SENDON_API_KEY']}".encode()).decode()
    body = {
        'sendProfileId':'@bellavita', 'templateId':template, 'useCredit':True,
        'to':[{'phone':recipient, 'variables':{f'#{{{key}}}':str(value) for key, value in variables.items()}}],
    }
    request = Request(API, json.dumps(body, ensure_ascii=False).encode(), {
        'Authorization':'Basic '+auth, 'Content-Type':'application/json',
    })
    try:
        with (open_request or build_opener(NoRedirect).open)(request, timeout=12) as response:
            result = json.loads(response.read(100_000))
    except HTTPError as error:
        if error.code == 429:
            return 'queued', '', '429', '요청 한도 초과'
        return ('held' if error.code >= 500 else 'failed'), '', str(error.code), '센드온 HTTP 오류'
    except (OSError, ValueError):
        return 'held', '', 'UNKNOWN', '발송 접수 여부 확인 필요'
    code = result.get('code') if isinstance(result, dict) else None
    if code == 200:
        data = result.get('data')
        group = data.get('groupId', '') if isinstance(data, dict) else ''
        if isinstance(group, str) and re.fullmatch(r'[a-fA-F0-9-]{36}', group):
            return 'sent', group, '200', '센드온 접수 완료'
        return 'held', '', 'NO_GROUP_ID', '발송 접수 여부 확인 필요'
    if code == 429:
        return 'queued', '', '429', '요청 한도 초과'
    if isinstance(code, int) and 400 <= code < 500:
        return 'failed', '', str(code), '센드온이 요청을 거절했습니다. 템플릿·인증·수신번호 확인 필요'
    return 'held', '', str(code) if isinstance(code, int) else 'UNKNOWN', '발송 접수 여부 확인 필요'


def list_jobs(db):
    setup(db)
    rows = db.execute("""SELECT order_id,template,substr(recipient,-4) AS recipient_last4,
        state,request_id,result_code,reason,created_at,started_at,completed_at,attempts
        FROM sendon_order_queue ORDER BY created_at DESC,order_id,template LIMIT 100""")
    keys = [field[0] for field in rows.description]
    return [dict(zip(keys, row)) for row in rows]


def process(db, order_id, template, *, retry=False, env=None, open_request=None):
    setup(db)
    initial = 'failed' if retry else 'queued'
    with db:
        item = db.execute("""UPDATE sendon_order_queue
            SET state='sending', started_at=?, completed_at=NULL, attempts=attempts+1
            WHERE order_id=? AND template=? AND state=?
            RETURNING recipient,variables,attempts""", (now(), order_id, template, initial)).fetchone()
    if not item:
        return False
    recipient, variables, attempts = item
    state, request_id, code, reason = transmit(template, recipient, json.loads(variables),
                                               env=env, open_request=open_request)
    if state == 'queued' and attempts >= 3:
        state = 'failed'
    with db:
        db.execute("""UPDATE sendon_order_queue
            SET state=?,request_id=?,result_code=?,reason=?,completed_at=?
            WHERE order_id=? AND template=? AND state='sending' AND attempts=?""",
            (state, request_id or None, code, reason, now(), order_id, template, attempts))
    return True
