"""Durable order intake, paid-order work queue, and approved staff web access."""
import hashlib
import contextlib
import customer_sources
import order_origin
import json
import os
import re
import secrets
import time
import uuid
from order_lock import writer_lock
from datetime import datetime
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo('Asia/Seoul')
MAX_ATTEMPTS = 5

class RetryLater(ValueError):
    pass
EDIT = ('delivery_date', 'delivery_time', 'venue', 'address', 'sender', 'recipient', 'artwork', 'size', 'message')
LABELS = ('배송일', '배송시각', '설치 장소', '주소', '보내시는 분', '받는 분', '작품', '규격', '축하 문구')


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS order_operations (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL,
        raw TEXT NOT NULL, details TEXT NOT NULL, state TEXT NOT NULL, order_number INTEGER,
        retry_after REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0)''')
    c.execute('CREATE TABLE IF NOT EXISTS order_staff_web (digest TEXT PRIMARY KEY, owner TEXT, expires REAL, kind TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS order_staff_notify (owner TEXT PRIMARY KEY, center TEXT, phone TEXT UNIQUE)')
    c.execute('''CREATE TABLE IF NOT EXISTS order_sms (order_id TEXT, owner TEXT, state TEXT, receipt TEXT,
                 PRIMARY KEY(order_id,owner))''')
    c.execute('''CREATE TABLE IF NOT EXISTS order_changes (order_id TEXT, owner TEXT, changed TEXT,
                 before TEXT, after TEXT)''')
    c.execute('CREATE TABLE IF NOT EXISTS order_review (order_id TEXT PRIMARY KEY, owner TEXT, checked TEXT)')
    c.execute('CREATE INDEX IF NOT EXISTS order_owner_created ON order_operations(owner,kind,created DESC,id DESC)')
    import order_assistance
    order_assistance.setup(c)
    import order_sharing
    order_sharing.setup(c)
    customer_sources.setup(c)
    order_origin.setup(c)
    import order_cancellation
    order_cancellation.setup(c)


def now():
    return datetime.now(SEOUL).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def stage(c, key, owner, kind, raw, details, created):
    c.execute("INSERT OR IGNORE INTO order_operations(id,owner,kind,created,raw,details,state) VALUES (?,?,?,?,?,?,'pending')",
              (key, owner, kind, created, raw, dump(details)))
    return c.execute('SELECT * FROM order_operations WHERE id=?', (key,)).fetchone()


def intake_key(message,source_context=None):
    from order_intake import REQUEST
    canonical='\n'.join(line.strip() for line in REQUEST.sub('',message).splitlines() if line.strip())
    suffix='\0'+json.dumps(source_context,sort_keys=True,ensure_ascii=False) if source_context is not None else ''
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'bellavita:kakao:'+canonical+suffix))


def intake(db, owner, message, source_context=None, *, trusted_manual=False):
    key=intake_key(message,source_context)
    with db() as c:
        alias=c.execute('SELECT order_id FROM order_intake_aliases WHERE alias_key=?',(key,)).fetchone()
        if alias:key=alias[0]
        existing=c.execute('SELECT 1 FROM order_operations WHERE id=?',(key,)).fetchone()
        row=stage(c,key,owner,'kakao',message,{},now())
        if source_context is not None:c.execute('INSERT OR IGNORE INTO order_sources VALUES (?,?)',(key,dump(source_context)))
        elif trusted_manual and not existing:order_origin.record_manual(c,key,owner,'직원이 현재 요청으로 직접 접수')
    if row['owner']!=owner:return None,'같은 내용의 주문을 이미 접수했어요.'
    return key,None


def latest_intake(c, owner):
    return c.execute(
        """SELECT state,order_number FROM order_operations
           WHERE owner=? AND kind='kakao' ORDER BY created DESC LIMIT 1""",
        (owner,),
    ).fetchone()


def paid_details(row):
    data = json.loads(row['details'])
    recipient = next((value for label, value in data.get('options', []) if '받으시는 분' in label or '받는 분' in label), '')
    when = data.get('event_time', '')
    try:
        event = datetime.fromisoformat(when)
        event_date, event_time = event.date().isoformat(), event.strftime('%H:%M')
    except (ValueError, TypeError, AttributeError):
        event_date = event_time = ''
    size = next((f'쌀빛 {match[1]}kg' for _, value in data.get('options', [])
                 if (match := re.search(r'(10|20|30)\s*kg', value, re.I))), '')
    message = data.get('message') or next((v for k,v in data.get('options', []) if '축하 문구' in k), '')
    fields = {k: data.get(k, '') for k in ('venue', 'address', 'sender', 'product', 'title')}
    fields.update(event_type='결혼', recipient=recipient, event_date=event_date, event_time=event_time,
                  photo_phone=data.get('buyer_phone', ''), message=message, size=size, quantity=1,
                  note='초대장 페이앱 주문', source=data.get('source', ''))
    return fields


def stage_paid(c, row):
    return stage(c, str(uuid.UUID(row['id'])), '', 'payapp', '', paid_details(row), row['created'])


def ledger_key(payment_id):
    return str(uuid.UUID(payment_id))


def save(db, key, extract, write, after_save=None, *, source_guard=None):
    with writer_lock(db):
        with db() as c:row=c.execute('SELECT context FROM order_sources WHERE order_id=?',(key,)).fetchone()
        if row is None:
            with db() as c:
                operation=c.execute('SELECT * FROM order_operations WHERE id=?',(key,)).fetchone()
                if operation and operation['kind']=='kakao' and order_origin.unfinished(c,operation) and not order_origin.approved(c,key):
                    order_origin.hold(c,key)
                    unverified=True
                else:unverified=False
            if unverified:raise order_origin.OriginUnverified('출처가 기록되지 않은 기존 주문을 보류했어요. 원본 연결 또는 직원 직접입력을 명시적으로 확인해 주세요.')
        try:
            if row and source_guard is None:raise customer_sources.SourceRevoked('원본 고객방 연결을 검증하지 못해 자동 처리를 중지했어요.')
            guard=source_guard(json.loads(row['context'])) if row else contextlib.nullcontext()
            with guard:return _save_locked(db,key,extract,write,after_save)
        except customer_sources.SourceRevoked:
            with db() as c:customer_sources.hold(c,key)
            raise

def _save_locked(db, key, extract, write, after_save=None):
    with db() as c:
        row = c.execute('SELECT * FROM order_operations WHERE id=?', (key,)).fetchone()
    if row['state'] == 'saved':
        return row['order_number'], json.loads(row['details'])
    if row['attempts'] >= MAX_ATTEMPTS and row['state'] in ('pending','update'):
        with db() as c:c.execute("UPDATE order_operations SET state='review_hold' WHERE id=?",(key,))
        raise ValueError('재시도 한도에 도달해 검토 보류했어요.')
    if row['state'] not in ('pending', 'update'):
        raise ValueError('취소 요청 또는 보류 상태의 주문은 자동 등록하지 않아요.')
    if row['retry_after']>time.time():raise RetryLater('아직 자동 재시도 대기 중이에요. 대기 시각 이후 다시 확인해 주세요.')
    data = json.loads(row['details'])
    if not data and row['kind'] == 'kakao':
        try:data = extract(row['raw'])
        except Exception:
            import order_assistance
            with db() as c:order_assistance.record(c,key,order_assistance.explicit_details(row['raw']),error=True)
            raise
        with db() as c:
            c.execute('UPDATE order_operations SET details=? WHERE id=?', (dump(data), key))
    import order_assistance
    with db() as c:review=order_assistance.record(c,key,data)
    if review['hold']:
        raise ValueError(order_assistance.question(data))
    try:
        number, verified = write(key, data, row['created'], update=row['state'] == 'update')
    except Exception as error:
        with db() as c:order_assistance.record(c,key,data,error=getattr(error,'field',True))
        raise
    with db() as c:
        c.execute("UPDATE order_operations SET details=?,order_number=?,state='saved',attempts=0,retry_after=0 WHERE id=?",
                  (dump(verified), number, key))
        order_assistance.record(c,key,verified)
        import order_sharing
        order_sharing.enqueue(c,row,number,verified)
        if after_save:
            after_save(c, row, number, verified)
    return number, verified


def defer(c, key, error=None):
    if isinstance(error,RetryLater):return
    row=c.execute('SELECT attempts,state FROM order_operations WHERE id=?',(key,)).fetchone()
    if not row or row['state'] not in ('pending','update'):return
    attempts=row['attempts']+1
    delay=min(1800,60*2**min(attempts-1,5))
    c.execute('UPDATE order_operations SET attempts=?,retry_after=?,state=? WHERE id=?',
              (attempts,time.time()+delay,'review_hold' if attempts>=MAX_ATTEMPTS else row['state'],key))


def resume(c,row):
    if row['state']!='review_hold':return False
    c.execute('UPDATE order_operations SET state=?,attempts=0,retry_after=0 WHERE id=?',
              ('update' if row['order_number'] is not None else 'pending',row['id']))
    c.execute('INSERT INTO order_changes VALUES (?,?,?,?,?)',
              (row['id'],row['owner'],now(),dump({'state':'review_hold'}),dump({'state':'retry_requested'})))
    return True


def issue_web(c, owner):
    code = secrets.token_urlsafe(24)
    c.execute('DELETE FROM order_staff_web WHERE expires<=?', (time.time(),))
    c.execute("INSERT INTO order_staff_web VALUES (?,?,?,'link')", (digest(code), owner, time.time()+300))
    return '/staff-orders/login/' + code


def login(c, code):
    row = c.execute("DELETE FROM order_staff_web WHERE digest=? AND kind='link' AND expires>? RETURNING owner",
                    (digest(code), time.time())).fetchone()
    if not row:
        return None
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO order_staff_web VALUES (?,?,?,'session')", (digest(token), row['owner'], time.time()+14400))
    return token


def web_owner(c, token):
    if not token:
        return None
    row = c.execute("SELECT owner FROM order_staff_web WHERE digest=? AND kind='session' AND expires>?",
                    (digest(token), time.time())).fetchone()
    return row['owner'] if row else None


def list_orders(c, selected=''):
    return c.execute('''SELECT i.id,i.created,i.state AS payment,i.details AS payment_details,
                         o.state,o.order_number,o.details,r.checked
                         FROM invitation_orders i LEFT JOIN order_operations o ON o.id=
                         substr(i.id,1,8)||'-'||substr(i.id,9,4)||'-'||substr(i.id,13,4)||'-'||substr(i.id,17,4)||'-'||substr(i.id,21)
                         LEFT JOIN order_review r ON r.order_id=o.id
                         WHERE i.state IN ('paid','cancelled','partial_cancel') AND (?='' OR i.id=?)
                         ORDER BY i.created DESC LIMIT 100''', (selected, selected)).fetchall()


def edit(c, order_id, owner, fields):
    key = ledger_key(order_id)
    c.execute('BEGIN IMMEDIATE')
    row = c.execute('''SELECT o.*, i.state AS payment FROM order_operations o JOIN invitation_orders i ON i.id=?
                       WHERE o.id=? AND o.kind='payapp' ''', (order_id, key)).fetchone()
    if not row or row['payment'] != 'paid':
        return False
    if fields.get('operation') == 'review' and row['state'] == 'saved':
        c.execute('INSERT OR REPLACE INTO order_review VALUES (?,?,?)', (key, owner, now()))
        return True
    if fields.get('operation') == 'retry' and row['state'] in ('pending', 'update'):
        c.execute('UPDATE order_operations SET retry_after=0 WHERE id=?', (key,))
        return True
    if fields.get('operation') != 'save' or row['state'] not in ('saved', 'pending'):
        return False
    before = json.loads(row['details'])
    after = {**before, **{k: fields[k].strip()[:200] for k in EDIT if k in fields}}
    try:
        from datetime import date
        if after.get('delivery_date'):
            date.fromisoformat(after['delivery_date'])
    except ValueError:
        return False
    if after.get('delivery_time') and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', after['delivery_time']):
        return False
    c.execute('INSERT INTO order_changes VALUES (?,?,?,?,?)', (key, owner, now(), dump(before), dump(after)))
    c.execute('UPDATE order_operations SET details=?,state=?,retry_after=0 WHERE id=?',
              (dump(after), 'update' if row['state'] == 'saved' else 'pending', key))
    import order_sharing
    order_sharing.invalidate(c,key)
    c.execute('DELETE FROM order_review WHERE order_id=?', (key,))
    return True


def page(orders, selected):
    cards = []
    for row in orders:
        if selected and row['id'] != selected:
            continue
        paid = json.loads(row['payment_details'])
        data = json.loads(row['details']) if row['details'] else paid_details({'details': row['payment_details']})
        status = {'paid': '결제 완료', 'cancelled': '취소', 'partial_cancel': '일부 취소'}.get(row['payment'], '결제 확인 중')
        ledger = str(row['order_number'] or ('원장 등록 대기' if row['state'] else '기존 결제 내역'))
        href = '/staff-orders?' + urlencode({'order': row['id']})
        details = ''
        if selected:
            fields = ''.join(f'<label>{label}<input name="{key}" maxlength="200" value="{escape(str(data.get(key) or ""))}"></label>'
                             for key, label in zip(EDIT, LABELS))
            details = f'<p>행사 {escape(data.get("event_date", ""))} {escape(data.get("event_time", ""))} · 수량 {data.get("quantity", 1)}개</p>'
            details += f'<p>{escape(data.get("notes", "") or "확인이 필요한 항목은 원장 반영 후 표시됩니다.")}</p>'
            if row['payment'] == 'paid' and row['state']:
                if row['order_number']:
                    from order_intake import report
                    details += f'<details><summary>복사용 주문서</summary><pre style="white-space:pre-wrap">{escape(report(row["order_number"], data))}</pre></details>'
                details += (f'<form method="post" action="{escape(href)}">{fields}'
                            '<button name="operation" value="save">수정 저장</button></form>')
                if row['state'] == 'saved' and not row['checked']:
                    details += f'<form method="post" action="{escape(href)}"><button name="operation" value="review">확인 완료</button></form>'
                if row['state'] in ('pending', 'update'):
                    details += f'<form method="post" action="{escape(href)}"><button name="operation" value="retry">원장 반영 다시 시도</button></form>'
        cards.append(f'<article><a href="{escape(href)}">{escape(str(data.get("title") or "초대장 주문"))}</a>'
                     f'<p>{status} · {paid["amount"]:,}원 · {ledger}{" · 확인 완료" if row["checked"] else ""}</p>{details}</article>')
    body = ''.join(cards) or '<p>확인할 주문이 없어요.</p>'
    return ('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>초대장 주문관리</title><style>body{background:#f4f6f3;color:#20352f;font:18px system-ui;margin:0;padding:24px}'
            'main{max-width:720px;margin:auto}article{background:white;border-radius:14px;padding:18px;margin:16px 0}'
            'a{color:#145747}label{display:block;margin:10px 0}input{box-sizing:border-box;width:100%;padding:10px;font:inherit}'
            'button{font:inherit;margin:10px 8px 0 0;padding:8px 16px}</style><main><h1>초대장 주문관리</h1>'
            '<a href="/staff-orders">전체 주문</a>' + body + '</main></html>')
