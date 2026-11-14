"""Authenticated staff records of confirmation, separate from cancellation requests."""
import json,re

TARGETS=('배송','원장','공유','청구')
PATTERN=re.compile(r'^주문 취소확인 ([1-9][0-9]*)\n대상: (배송|원장|공유|청구)\n확인ID: ([A-Za-z0-9_-]{4,100})\n근거: ([^\n]{4,300})$')

def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS order_cancel_confirmations (
        order_id TEXT,target TEXT,confirmation_id TEXT,evidence TEXT,actor TEXT,confirmed_at TEXT,
        PRIMARY KEY(order_id,target))''')

def command(db,owner,message):
    if not message.startswith('주문 취소확인'):return None
    match=PATTERN.fullmatch(message)
    if not match:return '주문 취소확인 주문번호 다음 줄에 대상: 배송/원장/공유/청구, 확인ID: 고유번호, 근거: 실제 확인 내용을 각각 적어주세요.'
    number,target,receipt,evidence=match.groups()
    if re.search(r'요청|미확인|아직|예정|대기|모름|미정',evidence):return '요청·예정은 실제 취소 확인으로 기록하지 않아요. 확인한 결과와 근거를 적어주세요.'
    import order_operations as ops
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        rows=c.execute("SELECT * FROM order_operations WHERE owner=? AND kind='kakao' AND order_number=?",(owner,int(number))).fetchall()
        if len(rows)!=1:return '이 직원 계정의 대상 주문을 하나로 확인하지 못했어요.'
        row=rows[0]
        if row['state'] not in ('cancel_requested','cancel_confirmed'):return '먼저 해당 주문의 취소 요청을 기록해 주세요.'
        previous=c.execute('SELECT * FROM order_cancel_confirmations WHERE order_id=? AND target=?',(row['id'],target)).fetchone()
        if previous:return '같은 취소 확인이 이미 기록되어 있어요.' if previous['confirmation_id']==receipt and previous['evidence']==evidence else '이 대상에 다른 확인 기록이 있어 덮어쓰지 않았어요. 기존 기록을 검토해 주세요.'
        c.execute('INSERT INTO order_cancel_confirmations VALUES (?,?,?,?,?,?)',(row['id'],target,receipt,evidence,owner,ops.now()))
        c.execute('INSERT INTO order_changes VALUES (?,?,?,?,?)',(row['id'],owner,ops.now(),ops.dump({'cancel_target':target,'confirmed':False}),ops.dump({'cancel_target':target,'confirmed':True,'confirmation_id':receipt})))
        if target=='배송':c.execute("UPDATE order_operations SET state='cancel_confirmed' WHERE id=?",(row['id'],))
        return target+' 취소 확인을 직원 수동 기록으로 남겼어요. 다른 대상의 취소 완료를 뜻하지는 않습니다.'

def status(c,key):
    confirmed={row[0] for row in c.execute('SELECT target FROM order_cancel_confirmations WHERE order_id=?',(key,))}
    return '취소 확인(직원 수동 기록): '+', '.join(target+(' 확인 기록 있음' if target in confirmed else ' 미확인') for target in TARGETS)
