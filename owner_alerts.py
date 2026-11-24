"""Send approval notices to the representative's Kakao self chat."""
import hashlib
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ORIGIN = 'https://bellavita-taesan-test.onrender.com'
REDIRECT = ORIGIN + '/owner-alerts/connected'
TOKEN_URL = 'https://kauth.kakao.com/oauth/token'


def configured():
    return bool(os.getenv('KAKAO_CLIENT_ID'))


def setup(db):
    db.execute('CREATE TABLE IF NOT EXISTS alert_link (digest TEXT PRIMARY KEY, expires REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS alert_account (singleton INTEGER PRIMARY KEY, refresh TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS alert_links (id TEXT PRIMARY KEY, url TEXT NOT NULL)')
    db.execute("""CREATE TABLE IF NOT EXISTS alert_outbox (
        id TEXT PRIMARY KEY, message TEXT NOT NULL, created REAL NOT NULL,
        sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, retry REAL NOT NULL DEFAULT 0)""")


def enqueue(db, event, message, link=None):
    db.execute('INSERT OR IGNORE INTO alert_outbox(id,message,created) VALUES (?,?,?)',
               (event, message[:185], time.time()))
    if link:
        db.execute('INSERT OR IGNORE INTO alert_links VALUES (?,?)', (event, link))


def claim_payapp_staff(db, order_id, channel, *, same_channel_retry=False):
    event = 'payapp:' + order_id.replace('-', '')
    db.execute("""CREATE TABLE IF NOT EXISTS payapp_staff_dispatch (
        event_id TEXT PRIMARY KEY, channel TEXT NOT NULL, claimed_at REAL NOT NULL
    )""")
    # Delivered or previously attempted legacy notices may predate this guard.
    old = db.execute("""SELECT 1 FROM alert_outbox WHERE id LIKE 'payapp:%'
        AND replace(id,'-','')=? AND (sent=1 OR attempts>0)""", (event,)).fetchone()
    if old:
        return False
    if same_channel_retry:
        previous = db.execute('SELECT channel FROM payapp_staff_dispatch WHERE event_id=?',(event,)).fetchone()
        if previous:
            return previous[0] == channel
    return db.execute('INSERT OR IGNORE INTO payapp_staff_dispatch VALUES (?,?,?)',
                      (event, channel, time.time())).rowcount == 1


def enqueue_payapp_staff(db_factory, order_id, order):
    import sendon_order_queue
    if sendon_order_queue.mode(os.environ) == 'live':
        return
    from contextlib import closing
    with closing(db_factory()) as db, db:
        setup(db)
        link = ORIGIN + '/staff-orders?order=' + order_id
        message = '새 초대장 주문\n' + str(order.get('title') or '벨라비타 주문')
        enqueue(db, 'payapp-staff:' + order_id, message, link)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def link(db):
    if not configured():
        return None
    state = secrets.token_urlsafe(32)
    db.execute('DELETE FROM alert_link')
    db.execute('INSERT INTO alert_link VALUES (?,?)', (digest(state), time.time()+600))
    return 'https://kauth.kakao.com/oauth/authorize?' + urlencode({
        'client_id': os.environ['KAKAO_CLIENT_ID'], 'redirect_uri': REDIRECT,
        'response_type': 'code', 'state': state, 'scope': 'talk_message', 'prompt': 'select_account'})


def post(url, form, access=None):
    headers = {'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'}
    if access:
        headers['Authorization'] = 'Bearer '+access
    with urlopen(Request(url, data=urlencode(form).encode(), headers=headers), timeout=10) as response:
        return json.load(response)


def token(grant, **fields):
    form = {'grant_type': grant, 'client_id': os.environ['KAKAO_CLIENT_ID'], **fields}
    secret = os.getenv('KAKAO_CLIENT_SECRET')
    if secret:
        form['client_secret'] = secret
    return post(TOKEN_URL, form)


def connect(db_factory, code, state):
    with db_factory() as c:
        row = c.execute('DELETE FROM alert_link WHERE digest=? AND expires>? RETURNING digest',
                        (digest(state),time.time())).fetchone()
    if not row:
        return False
    issued = token('authorization_code', code=code, redirect_uri=REDIRECT)
    if not issued.get('refresh_token') or 'talk_message' not in issued.get('scope','').split():
        return False
    identity = post('https://kapi.kakao.com/v2/user/me', {}, issued['access_token'])
    if not identity.get('id'):
        return False
    with db_factory() as c:
        c.execute('INSERT OR REPLACE INTO alert_account VALUES (1,?)', (issued['refresh_token'],))
        enqueue(c, 'connection-'+digest(state), '태산이 알림을 연결했어요. 직원 신청과 업무 개선 요청이 오면 이 채팅으로 알려드릴게요.')
    return True


def send_pending(db_factory, *, env=None):
    import sendon_order_queue
    legacy_payapp = sendon_order_queue.mode(os.environ if env is None else env) != 'live'
    with db_factory() as c:
        account = c.execute('SELECT refresh FROM alert_account WHERE singleton=1').fetchone()
        row = c.execute('''SELECT o.*,l.url FROM alert_outbox o LEFT JOIN alert_links l USING(id)
                        WHERE sent=0 AND id NOT LIKE 'payapp:%' AND (? OR id NOT LIKE 'payapp-staff:%') AND retry<=? ORDER BY created LIMIT 1''',
                        (legacy_payapp, time.time(),)).fetchone()
    if not account or not row:
        return
    claimed = False
    try:
        issued = token('refresh_token', refresh_token=account['refresh'])
        if issued.get('refresh_token'):
            with db_factory() as c:
                c.execute('UPDATE alert_account SET refresh=? WHERE singleton=1 AND refresh=?',
                          (issued['refresh_token'], account['refresh']))
        link = row['url'] or ORIGIN
        template = {'object_type':'text', 'text':row['message'], 'link':{'web_url':link,'mobile_web_url':link},
                    'button_title':'주문 내역' if row['url'] else '태산이 안내'}
        if row['id'].startswith('payapp-staff:'):
            with db_factory() as c:
                c.execute('BEGIN IMMEDIATE')
                if not claim_payapp_staff(c, row['id'].split(':',1)[1], 'kakao'):
                    c.execute('UPDATE alert_outbox SET retry=? WHERE id=?', (253402300799, row['id']))
                    return
                claimed = True
        response = post('https://kapi.kakao.com/v2/api/talk/memo/default/send',
                        {'template_object':json.dumps(template, ensure_ascii=False)}, issued['access_token'])
        if response.get('result_code') != 0:
            raise RuntimeError('Kakao delivery failed')
    except Exception:
        with db_factory() as c:
            c.execute('UPDATE alert_outbox SET attempts=attempts+1,retry=? WHERE id=?',
                      (253402300799 if claimed else time.time()+min(3600, 30*2**min(row['attempts'],7)),row['id']))
        return
    with db_factory() as c:
        c.execute('UPDATE alert_outbox SET sent=1 WHERE id=?',(row['id'],))


def start(db_factory):
    def loop():
        while True:
            try:
                if configured():
                    send_pending(db_factory)
            except Exception:
                print('대표 알림 대기열 확인 실패', flush=True)
            time.sleep(3)
    threading.Thread(target=loop,daemon=True).start()
