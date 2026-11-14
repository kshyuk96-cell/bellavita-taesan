"""PayApp cash receipt issuance and cancel/reissue with durable ambiguity guards."""
import datetime as dt
import json
import os
import re
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, build_opener
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo('Asia/Seoul')
LABELS = {'pending': '자동 발급 대기', 'issuing': '발급 요청 중', 'issued': '발급 완료',
          'cancelling': '취소 요청 중', 'cancelled': '영수증 취소 완료', 'uncertain': '발급·취소 결과 대조 필요',
          'failed': '처리 실패 · 대조 필요', 'held': '설정·거래일 확인 필요'}


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_receipt_payments (
        order_id TEXT PRIMARY KEY, pay_type TEXT NOT NULL, paid_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_cash_receipts (
        order_id TEXT NOT NULL, generation INTEGER NOT NULL, amount INTEGER NOT NULL,
        state TEXT NOT NULL, cashstno TEXT, receipt_no TEXT, url TEXT,
        error TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL,
        PRIMARY KEY(order_id,generation))''')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS shop_cashstno ON shop_cash_receipts(cashstno) WHERE cashstno IS NOT NULL')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_receipt_checks (
        id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, generation INTEGER NOT NULL,
        decision TEXT NOT NULL, note TEXT NOT NULL, checked TEXT NOT NULL)''')


def on_payment(c, row, fields):
    if fields.get('pay_state') != '4':
        return
    paid_at = fields.get('pay_date', '')
    # Preserve the provider's approval time; notification time is not trade time.
    if re.fullmatch(r'\d{14}', paid_at):
        try:
            paid_at = dt.datetime.strptime(paid_at, '%Y%m%d%H%M%S').replace(tzinfo=SEOUL).isoformat()
        except ValueError:
            paid_at = ''
    else:
        try:
            value = dt.datetime.fromisoformat(paid_at)
            paid_at = (value.replace(tzinfo=SEOUL) if value.tzinfo is None else value.astimezone(SEOUL)).isoformat()
        except ValueError:
            paid_at = ''
    c.execute('INSERT OR IGNORE INTO shop_receipt_payments VALUES (?,?,?)',
              (row['id'], fields.get('pay_type', ''), paid_at))
    c.execute('''UPDATE shop_receipt_payments SET pay_type=CASE WHEN pay_type='' THEN ? ELSE pay_type END,
                 paid_at=CASE WHEN paid_at='' THEN ? ELSE paid_at END WHERE order_id=? AND (pay_type='' OR pay_type=?)''',
              (fields.get('pay_type', ''), paid_at, row['id'], fields.get('pay_type', '')))


def target_amount(c, row):
    if row['state'] == 'cancelled':
        return 0
    amount = json.loads(row['details'])['amount']
    refunds = c.execute("SELECT COALESCE(SUM(amount),0) FROM payment_events WHERE order_id=? AND kind='partial'",
                        (row['id'],)).fetchone()[0]
    return max(0, amount - refunds)


def request(fields):
    import checkout
    from blogpay import NoRedirect
    config = checkout.payment_config()
    when = dt.datetime.now(SEOUL).strftime('%Y%m%d%H%M%S')
    payload = dict(fields, userid=config['USER_ID'], linkkey=config['LINK_KEY'],
                   requestdate=when, hash=checkout.signature(config, when))
    with build_opener(NoRedirect).open(Request('https://api.payapp.kr/oapi/v2',
                                               urlencode(payload).encode('ascii')), timeout=12) as reply:
        result = parse_qs(reply.read(32768).decode('utf-8'), keep_blank_values=True)
    if result.get('state') == ['0'] and result.get('errno') != ['9040'] and result.get('errorCode') != ['9040']:
        raise Rejected('provider_rejected')
    if result.get('state') != ['1']:
        raise OSError('uncertain_receipt_result')
    return {key: values[0] for key, values in result.items() if len(values) == 1}


class Rejected(Exception):
    pass


def status(c, row):
    latest = c.execute('SELECT * FROM shop_cash_receipts WHERE order_id=? ORDER BY generation DESC LIMIT 1',
                       (row['id'],)).fetchone()
    if latest:
        text = LABELS[latest['state']] + f' · {latest["amount"]:,}원'
        if latest['receipt_no']:
            text += ' · 승인번호 ' + latest['receipt_no']
        return text
    data = json.loads(row['details'])
    method = c.execute('SELECT * FROM shop_receipt_payments WHERE order_id=?', (row['id'],)).fetchone()
    if not data.get('cash_receipt'):
        return '현금영수증 신청 없음'
    if not method:
        return '결제수단 확인 대기'
    if method['pay_type'] not in ('6', '7'):
        return '자동 발급 제외 · 카드 또는 결제사 발급 여부 확인'
    if not ready() or data.get('product_snapshot', {}).get('cash_tax_type') not in ('TG01', 'TG02'):
        return '자동 발급 설정 확인 필요'
    return '자동 발급 대기'


def ready():
    import checkout
    return (os.getenv('SHOP_CASH_RECEIPT_ENABLED') == '1'
            and os.getenv('SHOP_CASH_RECEIPT_OWNER') == 'shop' and all(checkout.payment_config().values()))


def process_one(db):
    if not ready():
        return
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        candidates = c.execute('''SELECT o.*, p.pay_type, p.paid_at FROM invitation_orders o
            JOIN shop_receipt_payments p ON p.order_id=o.id WHERE p.pay_type IN ('6','7')
            AND o.state IN ('paid','partial_cancel','cancelled') ORDER BY o.created''').fetchall()
        task = None
        for order in candidates:
            data = json.loads(order['details'])
            tax_type = data.get('product_snapshot', {}).get('cash_tax_type')
            if not data.get('cash_receipt') or tax_type not in ('TG01', 'TG02'):
                continue
            latest = c.execute('SELECT * FROM shop_cash_receipts WHERE order_id=? ORDER BY generation DESC LIMIT 1',
                               (order['id'],)).fetchone()
            target = target_amount(c, order)
            if latest and latest['state'] in ('issuing', 'cancelling', 'uncertain', 'failed', 'held'):
                continue
            when = dt.datetime.now(SEOUL).isoformat()
            if latest and latest['state'] == 'issued':
                if latest['amount'] == target:
                    continue
                if target:
                    try:
                        age = dt.datetime.now(SEOUL) - dt.datetime.fromisoformat(order['paid_at'])
                        if not dt.timedelta(0) <= age < dt.timedelta(days=6):
                            raise ValueError('adjustment_expired')
                    except ValueError:
                        c.execute("""UPDATE shop_cash_receipts SET state='held',error='partial_adjustment_requires_merchant',
                                     updated=? WHERE order_id=? AND generation=?""", (when, order['id'], latest['generation']))
                        continue
                c.execute("UPDATE shop_cash_receipts SET state='cancelling',updated=? WHERE order_id=? AND generation=?",
                          (when, order['id'], latest['generation']))
                task = (order, latest['generation'], latest['amount'], dict(cmd='cashStCancel', cashstno=latest['cashstno']))
                break
            if not target:
                continue
            generation = latest['generation'] + 1 if latest else 1
            try:
                trade_time = dt.datetime.fromisoformat(order['paid_at'])
                age = dt.datetime.now(SEOUL) - trade_time
                if not dt.timedelta(0) <= age < dt.timedelta(days=6):
                    raise ValueError('trade_time_out_of_range')
            except ValueError:
                c.execute('''INSERT INTO shop_cash_receipts(order_id,generation,amount,state,error,updated)
                             VALUES (?,?,?,'held','trade_time_missing_or_expired',?)''', (order['id'], generation, target, when))
                continue
            receipt = data['cash_receipt']
            # Integer arithmetic: total is inclusive of VAT. No tax assumption for unconfigured goods.
            tax = (target + 5) // 11 if tax_type == 'TG01' else 0
            payload = dict(cmd='cashStRegist', good_name=data['title'], buyr_name=data['buyer'],
                           buyr_tel1=data['buyer_phone'], id_info=receipt['identifier'],
                           trad_time=trade_time.strftime('%Y%m%d%H%M%S'),
                           tr_code='1' if receipt['kind'] == 'business' else '0',
                           amt_tot=target, amt_sup=target-tax, amt_svc=0, amt_tax=tax,
                           corp_tax_type=tax_type, mul_no=order['payment_number'])
            c.execute('''INSERT INTO shop_cash_receipts(order_id,generation,amount,state,updated)
                         VALUES (?,?,?,'issuing',?)''', (order['id'], generation, target, when))
            task = (order, generation, target, payload)
            break
    if task is None:
        return
    order, generation, amount, payload = task
    try:
        result = request(payload)
        if payload['cmd'] == 'cashStRegist':
            if not re.fullmatch(r'\d{1,30}', result.get('cashstno', '')) or not result.get('receipt_no'):
                raise OSError('uncertain_receipt_result')
            parsed = urlsplit(result.get('cashsturl', ''))
            if parsed.scheme != 'https' or parsed.netloc not in ('payapp.kr', 'www.payapp.kr', 'api.payapp.kr'):
                raise OSError('unexpected_receipt_url')
            state = 'issued'
        else:
            state = 'cancelled'
    except Rejected:
        state, result = 'failed', {}
    except Exception:
        state, result = 'uncertain', {}
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if result.get('cashstno') and c.execute('''SELECT 1 FROM shop_cash_receipts WHERE cashstno=?
            AND NOT (order_id=? AND generation=?)''', (result['cashstno'], order['id'], generation)).fetchone():
            state, result = 'uncertain', {}
        c.execute('''UPDATE shop_cash_receipts SET state=?,cashstno=COALESCE(?,cashstno),
            receipt_no=COALESCE(?,receipt_no),url=COALESCE(?,url),error=?,updated=?
            WHERE order_id=? AND generation=?''',
                  (state, result.get('cashstno'), result.get('receipt_no'), result.get('cashsturl'),
                   '' if state in ('issued','cancelled') else 'provider_result_check_required',
                   dt.datetime.now(SEOUL).isoformat(), order['id'], generation))


def reconcile(c, fields):
    order_id, generation = fields.get('order_id', ''), fields.get('generation', '')
    row = c.execute('SELECT * FROM shop_cash_receipts WHERE order_id=? AND generation=?', (order_id, generation)).fetchone()
    decision, note = fields.get('receipt_decision'), fields.get('receipt_note', '').strip()
    if (not row or row['state'] not in ('uncertain', 'failed', 'held') or not 1 <= len(note) <= 600
            or fields.get('receipt_compared') != 'yes' or decision not in ('issued', 'cancelled')):
        raise ValueError('페이앱 발급·취소 내역을 먼저 대조하고 확인 근거를 입력해 주세요.')
    number, approval = fields.get('cashstno', ''), fields.get('receipt_no', '')
    if (not re.fullmatch(r'\d{1,30}', number) or not approval
            or fields.get('receipt_amount') != str(row['amount'])):
        raise ValueError('페이앱 영수증 발행번호·승인번호·금액을 확인해 주세요.')
    if c.execute('''SELECT 1 FROM shop_cash_receipts WHERE cashstno=?
        AND NOT (order_id=? AND generation=?)''', (number, order_id, generation)).fetchone():
        raise ValueError('다른 영수증에 연결된 발행번호예요.')
    c.execute('UPDATE shop_cash_receipts SET state=?,cashstno=?,receipt_no=?,error=? WHERE order_id=? AND generation=?',
              (decision, number, approval[:100], '', order_id, generation))
    c.execute('INSERT INTO shop_receipt_checks(order_id,generation,decision,note,checked) VALUES (?,?,?,?,?)',
              (order_id, generation, decision, note, dt.datetime.now(SEOUL).isoformat()))


def recover(c):
    c.execute("UPDATE shop_cash_receipts SET state='uncertain',error='worker_restarted' WHERE state IN ('issuing','cancelling')")
