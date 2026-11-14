"""Append-only content amendments; financial order snapshots stay immutable."""
import json
import secrets
from datetime import datetime,timezone
from html import escape
import partial_reviews


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_amendments (
        id TEXT PRIMARY KEY,order_id TEXT NOT NULL,base_revision INTEGER NOT NULL,
        changes TEXT NOT NULL,state TEXT NOT NULL,requester TEXT NOT NULL,note TEXT NOT NULL,created TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_amendment_events (
        id INTEGER PRIMARY KEY,order_id TEXT NOT NULL,amendment_id TEXT NOT NULL,decision TEXT NOT NULL,
        actor TEXT NOT NULL,reviewer TEXT NOT NULL,note TEXT NOT NULL,changed TEXT NOT NULL)''')


def revision(c,order_id):
    return c.execute('SELECT COALESCE(MAX(id),0) FROM shop_amendment_events WHERE order_id=?',(order_id,)).fetchone()[0]


def pending(c,order_id):
    return c.execute("SELECT * FROM shop_amendments WHERE order_id=? AND state='requested'",(order_id,)).fetchone()


def effective(c,row):
    change=c.execute("SELECT a.*,e.id AS revision FROM shop_amendments a JOIN shop_amendment_events e ON e.amendment_id=a.id AND e.decision='approved' WHERE a.order_id=? ORDER BY e.id DESC LIMIT 1",(row['id'],)).fetchone()
    result=dict(row)
    if change:
        data=json.loads(row['details']);data.update(json.loads(change['changes']));data['_amendment_revision']=change['revision']
        result['details']=json.dumps(data,ensure_ascii=False)
    return result


def order(c,order_id):
    row=c.execute('SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?',(order_id,)).fetchone()
    fulfillment=c.execute('SELECT stage FROM shop_fulfillment WHERE order_id=?',(order_id,)).fetchone()
    if not row or not partial_reviews.allowed(c,row) or not fulfillment or fulfillment['stage']!='confirmed':
        raise ValueError('결제·잔액 대조 후 배송 시작 전 주문만 정정할 수 있습니다. 배송 이후에는 AS 요청을 이용해 주세요.')
    return row


def request(c,fields,actor='admin-link'):
    row=order(c,fields.get('order_id',''));current=revision(c,row['id'])
    key=fields.get('amendment_attempt','')
    import re
    if not re.fullmatch(r'[a-f0-9]{32}',key):raise ValueError('정정 요청 화면을 다시 열어 주세요.')
    note=fields.get('amendment_note','').strip()
    changes={key:fields.get('new_'+key,'').strip() for key in ('sender','message')}
    if not 1<=len(changes['sender'])<=140 or not 1<=len(changes['message'])<=200 or not 1<=len(note)<=600:
        raise ValueError('보내는 분·문구·정정 근거를 확인해 주세요.')
    old=c.execute('SELECT * FROM shop_amendments WHERE id=?',(key,)).fetchone()
    if old:
        if old['order_id']!=row['id'] or json.loads(old['changes'])!=changes or old['note']!=note:raise ValueError('같은 정정 요청번호의 내용이 달라졌습니다.')
        return
    if fields.get('content_revision')!=str(current) or pending(c,row['id']):raise ValueError('정정 상태가 변경됐거나 검토 중인 요청이 있습니다.')
    data=json.loads(effective(c,row)['details'])
    if all(data.get(k)==v for k,v in changes.items()):raise ValueError('변경할 문구나 보내는 분을 입력해 주세요.')
    now=datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO shop_amendments VALUES (?,?,?,?,'requested',?,?,?)",(key,row['id'],current,json.dumps(changes,ensure_ascii=False),actor,note,now))
    c.execute("INSERT INTO shop_amendment_events(order_id,amendment_id,decision,actor,reviewer,note,changed) VALUES (?,?,'requested',?,'',?,?)",(row['id'],key,actor,note,now))
    hold(c,row,'amendment-request:'+key)


def hold(c,row,event):
    import order_handoff
    c.execute("UPDATE shop_handoff SET state='held' WHERE order_id=? AND kind IN ('ready','resume') AND state!='acked'",(row['id'],))
    order_handoff.enqueue(c,row,'hold',row['id']+':'+event,'pending')
    c.execute("UPDATE shop_messages SET state='held',error='content_review' WHERE order_id=? AND state IN ('queued','preparing','failed') AND (event IN ('confirmed','shipping','delivered') OR event LIKE 'resumed:%' OR event LIKE 'amended:%')",(row['id'],))


def review(c,fields,actor='admin-link'):
    decision=fields.get('amendment_decision')
    row=c.execute('SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?',(fields.get('order_id',''),)).fetchone() if decision=='rejected' else order(c,fields.get('order_id',''))
    if not row:raise ValueError('원주문을 찾지 못했습니다.')
    change=c.execute('SELECT * FROM shop_amendments WHERE id=? AND order_id=?',(fields.get('amendment_id',''),row['id'])).fetchone()
    decision=fields.get('amendment_decision');reviewer=fields.get('amendment_reviewer','').strip();note=fields.get('amendment_review_note','').strip()
    if not change or decision not in ('approved','rejected') or fields.get('amendment_checked')!='yes' or not 1<=len(reviewer)<=80 or not 1<=len(note)<=600:
        raise ValueError('정정 내용·승인자·근거를 확인해 주세요.')
    if change['state']==decision:return
    if change['state']!='requested' or fields.get('content_revision')!=str(revision(c,row['id'])):raise ValueError('정정 검토 상태가 변경됐습니다.')
    c.execute('UPDATE shop_amendments SET state=? WHERE id=?',(decision,change['id']))
    c.execute('INSERT INTO shop_amendment_events(order_id,amendment_id,decision,actor,reviewer,note,changed) VALUES (?,?,?,?,?,?,?)',
              (row['id'],change['id'],decision,actor,reviewer,note,datetime.now(timezone.utc).isoformat()))
    import production_files
    import order_handoff
    import order_ops
    if decision=='approved':
        hold(c,row,'amendment-approved:'+change['id'])
        order_ops.queue(c,row['id'],'amended:'+str(revision(c,row['id'])))
    elif partial_reviews.allowed(c,row) and production_files.allowed(c,row):
        order_handoff.enqueue(c,row,'ready' if row['state']=='paid' else 'resume',row['id']+':amendment-rejected:'+change['id'],'pending')


def panel(c,row,path):
    data=json.loads(effective(c,row)['details']);rev=revision(c,row['id']);current=pending(c,row['id'])
    stage=c.execute('SELECT stage FROM shop_fulfillment WHERE order_id=?',(row['id'],)).fetchone()
    eligible=partial_reviews.allowed(c,row) and stage and stage['stage']=='confirmed'
    result='<h3>주문 내용 정정</h3><p>원주문·금액을 보존합니다. 검토 중에는 제작을 보류하며 승인 후 새 제작 파일 검수가 필요합니다.</p>'
    hidden=f'<input type="hidden" name="order_id" value="{row["id"]}"><input type="hidden" name="content_revision" value="{rev}">'
    if not current and eligible:
        result+=f'<form method="post" action="{escape(path)}">{hidden}<input type="hidden" name="action" value="request_amendment"><input type="hidden" name="amendment_attempt" value="{secrets.token_hex(16)}"><label>정정할 보내는 분<input name="new_sender" value="{escape(data["sender"])}" maxlength="140" required></label><label>정정할 문구<textarea name="new_message" maxlength="200" required>{escape(data["message"] or dict(data["options"]).get("축하 문구",""))}</textarea></label><label>정정 근거<textarea name="amendment_note" maxlength="600" required></textarea></label><button>정정 검토 요청</button></form>'
    elif current:
        proposed=json.loads(current['changes']);result+='<p>검토 중: '+escape(proposed['sender']+' · '+proposed['message'])+'</p>'
        result+=f'<form method="post" action="{escape(path)}">{hidden}<input type="hidden" name="action" value="review_amendment"><input type="hidden" name="amendment_id" value="{current["id"]}"><label>결정<select name="amendment_decision"><option value="approved">정정 승인</option><option value="rejected">정정 반려</option></select></label><label>검토자 (직접 입력)<input name="amendment_reviewer" maxlength="80" required></label><label>검토 근거<textarea name="amendment_review_note" maxlength="600" required></textarea></label><label><input type="checkbox" name="amendment_checked" value="yes" required>원주문·변경 문구·보내는 분을 대조했습니다</label><button>정정 결정 저장</button></form>'
    else:result+='<p>정정은 결제·잔액 대조 후 배송 시작 전 가능합니다. 이후 처리는 AS 요청으로 기록해 주세요.</p>'
    if current and not eligible:result=result.replace('<option value="approved">정정 승인</option>','')
    for event in c.execute('SELECT * FROM shop_amendment_events WHERE order_id=? ORDER BY id',(row['id'],)):
        result+='<p>'+escape(f'정정 v{event["id"]} · {event["decision"]} · {event["reviewer"]} ({event["actor"]}) · {event["changed"][:16]} · {event["note"]}')+'</p>'
    return result
