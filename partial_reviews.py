"""Balance-specific fulfillment approvals; never changes financial payment state."""
import hashlib
import json
from datetime import datetime, timezone
from html import escape


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_partial_reviews (
        id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        refunded INTEGER NOT NULL, remaining INTEGER NOT NULL, decision TEXT NOT NULL,
        reviewer TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL, checked TEXT NOT NULL)''')


def balance(c, row):
    events = [tuple(r) for r in c.execute("SELECT event_key,amount FROM payment_events WHERE order_id=? AND kind='partial' ORDER BY event_key", (row['id'],))]
    refunded = sum(r[1] for r in events)
    original = json.loads(row['details'])['amount']
    fingerprint = hashlib.sha256(json.dumps([row['payment_number'], original, events], separators=(',', ':')).encode()).hexdigest()
    return fingerprint, refunded, original-refunded


def approval(c, row):
    if row['state'] != 'partial_cancel':
        return None
    check = c.execute('SELECT * FROM shop_partial_reviews WHERE order_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
    fingerprint, _, remaining = balance(c, row)
    return check if check and check['decision'] == 'resume' and check['fingerprint'] == fingerprint and remaining > 0 else None


def allowed(c, row):
    return row['state'] == 'paid' or approval(c, row) is not None


def review(c, fields, actor='admin-link'):
    row = c.execute('SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?', (fields.get('order_id', ''),)).fetchone()
    if not row or row['state'] != 'partial_cancel':
        raise ValueError('부분취소 주문만 잔액 대조할 수 있어요.')
    fingerprint, refunded, remaining = balance(c, row)
    decision = fields.get('partial_decision')
    reviewer, note = fields.get('partial_reviewer', '').strip(), fields.get('partial_note', '').strip()
    if (fields.get('partial_compared') != 'yes' or fields.get('partial_fingerprint') != fingerprint
            or fields.get('partial_remaining') != str(remaining) or decision not in ('hold', 'resume')
            or not 1 <= len(reviewer) <= 80 or not 1 <= len(note) <= 600 or remaining <= 0):
        raise ValueError('최신 결제번호·취소 누계·잔액을 대조하고 확인자와 근거를 입력해 주세요.')
    if decision == 'resume' and json.loads(row['details']).get('wedding_preview') and not c.execute(
            'SELECT 1 FROM invitation_photo_reviews WHERE order_id=?', (row['id'],)).fetchone():
        raise ValueError('신랑신부 사진 검토를 먼저 완료해 주세요.')
    previous = c.execute('SELECT * FROM shop_partial_reviews WHERE order_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
    if previous and previous['decision'] == decision and previous['fingerprint'] == fingerprint:
        return  # Form retries cannot enqueue another production request.
    if fields.get('partial_review_revision') != str(previous['id'] if previous else 0):
        raise ValueError('다른 관리자가 대조 결과를 변경했어요. 최신 화면에서 다시 확인해 주세요.')
    # A manually held approval cannot reuse its old resume event or notification.
    c.execute('INSERT INTO shop_partial_reviews(order_id,fingerprint,refunded,remaining,decision,reviewer,actor,note,checked) VALUES (?,?,?,?,?,?,?,?,?)',
              (row['id'], fingerprint, refunded, remaining, decision, reviewer, actor, note, datetime.now(timezone.utc).isoformat()))
    import order_handoff
    import order_ops
    if decision == 'resume':
        created = c.execute("INSERT OR IGNORE INTO shop_fulfillment VALUES (?,'confirmed',1,?)", (row['id'], order_ops.now())).rowcount
        if created:
            c.execute("INSERT INTO shop_stage_history VALUES (?,1,'confirmed','balance-review',?)", (row['id'], order_ops.now()))
        check = approval(c, row)
        order_handoff.enqueue(c, row, 'resume', row['id']+':resume:'+str(check['id']), 'pending')
        order_ops.queue(c, row['id'], 'resumed:'+str(check['id']))
    else:
        check = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("UPDATE shop_handoff SET state='held' WHERE order_id=? AND kind IN ('ready','resume') AND state!='acked'", (row['id'],))
        order_handoff.enqueue(c, row, 'hold', row['id']+':manual-hold:'+str(check), 'pending')
        c.execute("UPDATE shop_messages SET state='held',error='manual_hold' WHERE order_id=? AND state='queued' AND (event IN ('confirmed','shipping','delivered') OR event LIKE 'resumed:%')", (row['id'],))


def panel(c, row, path):
    content = '<h3>부분취소 잔액 대조</h3>'
    if row['state'] == 'partial_cancel':
        fingerprint, refunded, remaining = balance(c, row)
        latest = c.execute('SELECT id FROM shop_partial_reviews WHERE order_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
        review_revision = latest['id'] if latest else 0
        content += ('<p>결제번호 '+escape(str(row['payment_number']))+' · 취소 누계 '+str(refunded)+'원 · 잔액 '+str(remaining)+'원</p>'
            + f'<form method="post" action="{escape(path)}"><input type="hidden" name="action" value="review_partial">'
            + f'<input type="hidden" name="order_id" value="{row["id"]}"><input type="hidden" name="partial_fingerprint" value="{fingerprint}">'
            + f'<input type="hidden" name="partial_review_revision" value="{review_revision}">'
            + '<label>대조한 잔액<input name="partial_remaining" type="number" required></label>'
            + '<label>확인자 (직접 입력)<input name="partial_reviewer" maxlength="80" required></label>'
            + '<label>대조 근거<textarea name="partial_note" maxlength="600" required></textarea></label>'
            + '<select name="partial_decision"><option value="hold">보류 유지</option><option value="resume">현재 잔액으로 제작·배송 재개</option></select>'
            + '<label><input type="checkbox" name="partial_compared" value="yes" required>페이앱 결제번호·취소액·잔액을 대조했습니다</label><button>대조 결과 저장</button></form>')
    for check in c.execute('SELECT * FROM shop_partial_reviews WHERE order_id=? ORDER BY id', (row['id'],)):
        content += '<p>'+escape(f'{check["checked"][:16]} · {check["reviewer"]} ({check["actor"]}) · {check["decision"]} · 잔액 {check["remaining"]}원 · {check["note"]}')+'</p>'
    return content
