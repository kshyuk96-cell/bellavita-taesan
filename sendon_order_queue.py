"""Order-origin outbox. Provider acceptance is not handset delivery."""
import base64
import datetime as dt
import json
import logging
import os
import re
import threading
import time
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
        attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0, PRIMARY KEY(order_id,template)
    )""")

    columns = {row[1] for row in db.execute('PRAGMA table_info(sendon_order_queue)')}
    for name in ('retry_at', 'lease_until'):
        if name not in columns:
            try:
                db.execute(f'ALTER TABLE sendon_order_queue ADD COLUMN {name} REAL NOT NULL DEFAULT 0')
            except Exception as error:
                if 'duplicate column' not in str(error).lower():
                    raise


def scheduled_delivery_date(db, order_id, order):
    value = order.get('delivery_date')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_operations'").fetchone():
        row = db.execute(
            "SELECT details FROM order_operations WHERE replace(id,'-','')=? AND kind='payapp'",
            (order_id,),
        ).fetchone()
        if row:
            value = json.loads(row[0]).get('delivery_date', value)
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return ''

def enqueue(db_factory, order_id, order, *, customer=False):
    import order_sms
    import owner_alerts
    try:
        owner_alerts.enqueue_payapp_staff(db_factory, order_id, order)
    except Exception:
        logger.error('PayApp staff notification queue write failed')
    try:
        with closing(db_factory()) as db, db:
            setup(db)
            recipient = next(iter(order_sms.staff_phones(db)), '')
            when = scheduled_delivery_date(db, order_id, order)
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


def mode(env):
    value = env.get('SENDON_MODE')
    return value if value in ('off', 'dry_run', 'live') else 'off'


def live_enabled(env):
    return mode(env) == 'live' and all(
        env.get(key) for key in ('SENDON_ID', 'SENDON_API_KEY'))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def transmit(template, recipient, variables, *, env=None, open_request=None):
    env = os.environ if env is None else env
    if mode(env) == 'off':
        return 'held', '', 'MODE_OFF', '발송 모드 꺼짐'
    if mode(env) == 'live' and not live_enabled(env):
        return 'held', '', 'CONFIG_ERROR', '센드온 인증 설정 누락'
    if not re.fullmatch(r'01[016789][0-9]{7,8}', recipient):
        return 'failed', '', 'INVALID_PHONE', '휴대폰 번호 확인 필요'
    if mode(env) == 'dry_run':
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
            return 'accepted', group, '200', '센드온 접수 완료'
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
    env = os.environ if env is None else env
    if mode(env) == 'off':
        return False
    setup(db)
    if mode(env) == 'live':
        hold_pre_live(db, env)
    initial = 'failed' if retry else 'queued'
    with db:
        item = db.execute("""UPDATE sendon_order_queue
            SET state='sending', started_at=?, completed_at=NULL, attempts=attempts+1, lease_until=?
            WHERE order_id=? AND template=? AND state=? AND retry_at<=?
            RETURNING recipient,variables,attempts""", (now(), time.time()+60, order_id, template, initial, time.time())).fetchone()
        if item and template == STAFF and live_enabled(env):
            import owner_alerts
            owner_alerts.setup(db)
            if not owner_alerts.claim_payapp_staff(db, order_id, 'sendon', same_channel_retry=True):
                db.execute("""UPDATE sendon_order_queue SET state='held',result_code='STAFF_ALREADY_CLAIMED',
                    reason='직원 알림 중복 방지',completed_at=?,lease_until=0
                    WHERE order_id=? AND template=?""", (now(), order_id, template))
                return True
    if not item:
        return False
    recipient, variables, attempts = item
    state, request_id, code, reason = transmit(template, recipient, json.loads(variables),
                                               env=env, open_request=open_request)
    if state == 'queued' and attempts >= 3:
        state = 'failed'
    with db:
        db.execute("""UPDATE sendon_order_queue
            SET state=?,request_id=?,result_code=?,reason=?,completed_at=?,retry_at=?,lease_until=0
            WHERE order_id=? AND template=? AND state='sending' AND attempts=?""",
            (state, request_id or None, code, reason, now(),
             time.time()+min(300, 5*2**attempts) if state == 'queued' else 0, order_id, template, attempts))
    return True


def reconcile(db, order_id, template, group, recipient, env, *, open_request=None, supervised=False):
    """Look up the terminal result for the accepted message without resending."""
    if not live_enabled(env):
        return
    auth = base64.b64encode(f"{env['SENDON_ID']}:{env['SENDON_API_KEY']}".encode()).decode()
    endpoint = f'https://api.sendon.io/v2/statistics/messaging/groups/{group}/messages'
    request = Request(endpoint, headers={'Authorization':'Basic '+auth})
    try:
        with (open_request or build_opener(NoRedirect).open)(request, timeout=12) as response:
            response_data = json.loads(response.read(100_000))
        data = response_data.get('data') or {}
        if response_data.get('code') != 200:
            return
        rows = data.get('results') or []
        matches = [row for row in rows if row.get('groupId') == group
                   and re.sub(r'\D', '', str(row.get('to') or '')) == recipient]
        if len(matches) != 1:
            return
        item = matches[0]
        status = item.get('messageStatus')
        if status not in ('SUCCEEDED', 'FAILED', 'CANCELED'):
            return
        code = str(item.get('resultCode') or '')
        if not re.fullmatch(r'[A-Z0-9_-]{1,16}', code, re.I) or code == recipient:
            code = status
        with db:
            if supervised:
                db.execute("""UPDATE sendon_supervised_test SET final_state=?,final_code=?
                    WHERE order_id=? AND template=? AND api_state='accepted' AND request_id=?""",
                    (status, code, order_id, template, group))
                return
            db.execute("""UPDATE sendon_order_queue SET state=?,result_code=?,reason=?,completed_at=?
                WHERE order_id=? AND template=? AND state='accepted' AND request_id=?""",
                ('sent' if status == 'SUCCEEDED' else 'failed', code,
                 '수신 성공' if status == 'SUCCEEDED' else '센드온 최종 발송 실패', now(), order_id, template, group))
    except (OSError, ValueError, AttributeError, TypeError):
        return


def hold_pre_live(db, env):
    # Explicit activation boundary; missing or invalid boundary fails closed.
    try:
        cutoff = dt.datetime.fromisoformat(env.get('SENDON_LIVE_START_AT', ''))
        if cutoff.tzinfo is None or cutoff > dt.datetime.now(dt.timezone.utc):
            raise ValueError('invalid activation boundary')
        cutoff = cutoff.astimezone(dt.timezone.utc).isoformat()
    except (ValueError, TypeError):
        cutoff = None
    with db:
        conditions = "julianday(created_at) IS NULL OR julianday(created_at)<=julianday(?)"
        parameters = [now(), cutoff, cutoff]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='invitation_orders'").fetchone():
            conditions += """ OR EXISTS (
                SELECT 1 FROM invitation_orders AS orders
                WHERE orders.id=sendon_order_queue.order_id
                AND (julianday(orders.created) IS NULL OR julianday(orders.created)<=julianday(?))
            )"""
            parameters.append(cutoff)
        db.execute(f"""UPDATE sendon_order_queue SET state='held',
            result_code='PRE_LIVE_HOLD', reason='실발송 시작 전 주문 또는 시작시각 미설정',
            completed_at=?, lease_until=0
            WHERE state IN ('queued','failed') AND (? IS NULL OR {conditions})""", parameters)


def drain(db, env):
    if mode(env) == 'off':
        return
    setup(db)
    if mode(env) == 'live':
        hold_pre_live(db, env)
    timestamp = time.time()
    with db:
        db.execute("""UPDATE sendon_order_queue SET state='held', reason='중단된 발송의 접수 여부 확인 필요',
            completed_at=? WHERE state='sending' AND lease_until<=?""", (now(), timestamp))
        due = db.execute("""SELECT order_id,template FROM sendon_order_queue
            WHERE state='queued' AND retry_at<=? ORDER BY created_at LIMIT 3""", (timestamp,)).fetchall()
        accepted = db.execute("""SELECT order_id,template,request_id,recipient FROM sendon_order_queue
            WHERE state='accepted' AND retry_at<=? ORDER BY retry_at,created_at LIMIT 3""", (timestamp,)).fetchall()
        if live_enabled(env):
            db.executemany("UPDATE sendon_order_queue SET retry_at=? WHERE order_id=? AND template=? AND state='accepted'",
                           [(timestamp+60, item[0], item[1]) for item in accepted])
    for order_id, template in due:
        process(db, order_id, template, env=env)
    for order_id, template, group, recipient in accepted:
        reconcile(db, order_id, template, group, recipient, env)


def start(db_factory):
    def loop():
        while True:
            try:
                if mode(os.environ) != 'off':
                    with closing(db_factory()) as db:
                        drain(db, os.environ)
            except Exception:
                logger.error('Sendon outbox processing failed')
            time.sleep(5)
    threading.Thread(target=loop, daemon=True, name='sendon-order-outbox').start()
