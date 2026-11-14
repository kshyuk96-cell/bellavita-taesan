"""Versioned assignment capabilities, handover audit and event-date work board."""
import json
import os
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

SEOUL=ZoneInfo('Asia/Seoul')
STAGES={'confirmed':'주문 확인','shipping':'배송 중','delivered':'배송 완료'}


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_assignments (
        order_id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
        version INTEGER NOT NULL, note TEXT NOT NULL, updated TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_assignment_history (
        order_id TEXT NOT NULL, version INTEGER NOT NULL, owner TEXT NOT NULL,
        name TEXT NOT NULL, note TEXT NOT NULL, actor TEXT NOT NULL, changed TEXT NOT NULL,
        PRIMARY KEY(order_id,version))''')
    c.execute('''CREATE TABLE IF NOT EXISTS shop_assignment_accepts (
        order_id TEXT NOT NULL, version INTEGER NOT NULL, actor TEXT NOT NULL,
        note TEXT NOT NULL, accepted TEXT NOT NULL, PRIMARY KEY(order_id,version))''')


def roster(c):
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result={};known={}
    if 'staff' in tables:
        for row in c.execute('SELECT owner,role FROM staff'):
            known[row['owner']]=row['role']
            if row['role'] in ('admin','writer','staff'):result[row['owner']]=row['owner']+' (이름 미등록)'
    for key in ('TAESAN_ADMIN_IDS','TAESAN_STAFF_IDS','TAESAN_STOCK_WRITERS'):
        for owner in os.getenv(key,'').split(','):
            if owner.strip() and (owner.strip() not in known or known[owner.strip()] in ('admin','writer','staff')):result.setdefault(owner.strip(),owner.strip()+' (이름 미등록)')
    if 'access_requests' in tables:
        for row in c.execute("SELECT owner,name FROM access_requests WHERE decision='승인'"):
            if row['owner'] in result:result[row['owner']]=row['name']+' (등록명)'
    return result


def current(c,order_id):
    return c.execute('SELECT * FROM shop_assignments WHERE order_id=?',(order_id,)).fetchone()


def assign(c,fields,actor='admin-link'):
    order_id=fields.get('order_id','')
    row=c.execute('SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?',(order_id,)).fetchone()
    stage=c.execute('SELECT stage FROM shop_fulfillment WHERE order_id=?',(order_id,)).fetchone()
    if not row or row['state']=='cancelled' or (stage and stage['stage']=='delivered'):
        raise ValueError('취소·배송 완료 주문은 담당자를 변경할 수 없습니다.')
    old=current(c,order_id);version=old['version'] if old else 0
    owner=fields.get('assignee','');note=fields.get('assignment_note','').strip();people=roster(c)
    if (fields.get('assignment_version')!=str(version) or owner and owner not in people
            or not 1<=len(note)<=600):raise ValueError('최신 배정 상태·등록 직원·변경 근거를 확인해 주세요.')
    if old and old['owner']==owner:return
    name=people.get(owner,'미배정');changed=datetime.now(SEOUL).isoformat();version+=1
    c.execute('''INSERT INTO shop_assignments VALUES (?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET
        owner=excluded.owner,name=excluded.name,version=excluded.version,note=excluded.note,updated=excluded.updated''',
        (order_id,owner,name,version,note,changed))
    c.execute('INSERT INTO shop_assignment_history VALUES (?,?,?,?,?,?,?)',(order_id,version,owner,name,note,actor,changed))


def valid(c,order_id,version):
    assignment=current(c,order_id)
    return bool(assignment and assignment['owner'] and str(assignment['version'])==str(version) and assignment['owner'] in roster(c))


def link(c,order_id,secret,lifetime=86400):
    import order_ops
    assignment=current(c,order_id)
    if not assignment or not assignment['owner']:return order_ops.signed_path('delivery',order_id,secret,lifetime)
    import time
    version=assignment['version'];expiry=str(int(time.time())+lifetime)
    signature=order_ops.signature('assigned-delivery-'+str(version),order_id,expiry,secret)
    return f'/shop/assigned-delivery/{order_id}/{version}/{expiry}/{signature}'


def accept(c,fields,actor):
    order_id,version=fields.get('order_id',''),fields.get('assignment_version','')
    note=fields.get('handover_note','').strip()
    import partial_reviews
    order=c.execute('SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?',(order_id,)).fetchone()
    if not order or not partial_reviews.allowed(c,order):raise ValueError('결제·취소 대조 상태를 먼저 확인해 주세요.')
    if not valid(c,order_id,version) or fields.get('handover_checked')!='yes' or not 1<=len(note)<=600:
        raise ValueError('현재 담당 주문의 문구·장소·행사 일시를 확인하고 인수 근거를 입력해 주세요.')
    c.execute('INSERT OR IGNORE INTO shop_assignment_accepts VALUES (?,?,?,?,?)',
              (order_id,int(version),actor,note,datetime.now(SEOUL).isoformat()))


def panel(c,row,path,staff=False):
    assignment=current(c,row['id']);people=roster(c)
    name=assignment['name'] if assignment else '미배정';version=assignment['version'] if assignment else 0
    result='<h3>배송 담당·인수인계</h3><p>담당: '+escape(name)+'</p>'
    hidden=f'<input type="hidden" name="order_id" value="{row["id"]}"><input type="hidden" name="assignment_version" value="{version}">'
    if not staff:
        result+=f'<form method="post" action="{escape(path)}">{hidden}<input type="hidden" name="action" value="assign_delivery"><label>담당 직원<select name="assignee"><option value="">미배정</option>'
        for owner,label in people.items():result+=f'<option value="{escape(owner)}"'+(' selected' if assignment and assignment['owner']==owner else '')+'>'+escape(label)+'</option>'
        result+='</select></label><label>배정·변경 근거<textarea name="assignment_note" maxlength="600" required></textarea></label><button>배송 담당 저장</button></form>'
        if assignment and assignment['owner']:
            result+='<p><a href="'+escape(link(c,row['id'],os.environ['TAESAN_WEBHOOK_SECRET']))+'">현재 담당자 주문 링크 (24시간)</a></p>'
    elif assignment and assignment['owner']:
        result+=f'<form method="post" action="{escape(path)}">{hidden}<input type="hidden" name="action" value="accept_assignment"><label>인수 확인 근거<textarea name="handover_note" maxlength="600" required></textarea></label><label><input type="checkbox" name="handover_checked" value="yes" required>문구·장소·행사 일시를 확인했습니다</label><button>담당 주문 인수 확인</button></form>'
    for check in c.execute('SELECT * FROM shop_assignment_accepts WHERE order_id=? ORDER BY version',(row['id'],)):
        result+='<p>'+escape(f'배정 v{check["version"]} 인수 확인 · {check["accepted"][:16]} · {check["actor"]} · {check["note"]}')+'</p>'
    if not staff:
        for history in c.execute('SELECT * FROM shop_assignment_history WHERE order_id=? ORDER BY version',(row['id'],)):
            result+='<p>'+escape(f'배정 v{history["version"]} · {history["name"]} · {history["changed"][:16]} · {history["actor"]} · {history["note"]}')+'</p>'
    return result


def period(fields,prefix,days=30,maximum=93,today=None):
    today=today or datetime.now(SEOUL).date()
    try:
        start=datetime.strptime(fields.get(prefix+'_from') or (today-timedelta(days=7) if prefix=='schedule' else today).isoformat(),'%Y-%m-%d').date()
        end=datetime.strptime(fields.get(prefix+'_to') or (today+timedelta(days=days)).isoformat(),'%Y-%m-%d').date()
    except (ValueError,TypeError):raise ValueError('조회 기간은 YYYY-MM-DD 날짜로 입력해 주세요.') from None
    if end<start or (end-start).days>maximum:raise ValueError(f'조회 기간은 {maximum+1}일 이내로 선택해 주세요.')
    return start,end


def schedule(c,fields,now=None):
    import partial_reviews
    import production_files
    now=now or datetime.now(SEOUL);start,end=period(fields,'schedule',today=now.date())
    owner=fields.get('schedule_assignee','');stage=fields.get('schedule_stage','open')
    if stage not in ('open','all','confirmed','shipping','delivered'):raise ValueError('배송 단계 필터를 확인해 주세요.')
    rows=c.execute('''SELECT o.*,f.stage,a.owner AS assignee,a.name AS assignee_name FROM invitation_orders o
        JOIN shop_offers s ON s.id=o.job LEFT JOIN shop_fulfillment f ON f.order_id=o.id
        LEFT JOIN shop_assignments a ON a.order_id=o.id
        WHERE substr(json_extract(o.details,'$.event_time'),1,10) BETWEEN ? AND ?
        ORDER BY json_extract(o.details,'$.event_time'),o.id LIMIT 2001''',(start.isoformat(),end.isoformat())).fetchall()
    if len(rows)>2000:raise ValueError('일정이 2,000건을 초과합니다. 기간을 줄여 주세요.')
    result=[]
    for row in rows:
        if owner and (row['assignee'] or '')!=('' if owner=='unassigned' else owner):continue
        if stage=='open' and (row['stage']=='delivered' or row['state']=='cancelled'):continue
        if stage not in ('open','all') and row['stage']!=stage:continue
        data=json.loads(row['details']);when=datetime.fromisoformat(data['event_time'])
        if when.tzinfo is None:when=when.replace(tzinfo=SEOUL)
        when=when.astimezone(SEOUL);done=row['stage']=='delivered';held=not partial_reviews.allowed(c,row) or not production_files.allowed(c,row)
        urgency='완료' if done else '보류 확인' if row['state']=='cancelled' else '기한 경과' if when<now else '24시간 이내' if when-now<=timedelta(hours=24) else '예정'
        result.append({'id':row['id'],'event_time':when.isoformat(),'title':data['title'],'venue':data['venue'],
                       'stage':row['stage'] or '결제 대기','assignee':row['assignee_name'] or '미배정','held':held,
                       'urgency':urgency,'production':'검수 완료' if production_files.allowed(c,row) else '검수 대기'})
    return result


def schedule_panel(c,path,fields):
    start,end=period(fields,'schedule');result='<section id="shop-schedule"><h2>행사일 기준 제작·배송 일정</h2>'
    result+=f'<form method="get" action="{escape(path)}"><label>행사 시작일<input name="schedule_from" type="date" value="{start}" required></label><label>행사 종료일<input name="schedule_to" type="date" value="{end}" required></label><label>담당자<select name="schedule_assignee">'
    for owner,label in [('', '전체'),('unassigned','미배정')]+list(roster(c).items()):
        result+=f'<option value="{escape(owner)}"'+(' selected' if fields.get('schedule_assignee','')==owner else '')+'>'+escape(label)+'</option>'
    result+='</select></label><label>배송 단계<select name="schedule_stage">'
    for stage,label in [('open','미완료'),('all','전체')]+list(STAGES.items()):
        result+=f'<option value="{stage}"'+(' selected' if fields.get('schedule_stage','open')==stage else '')+'>'+label+'</option>'
    result+='</select></label><button>일정 조회</button></form><p>행사 일시 오름차순 · 한국 시간 · '+str(start)+' ~ '+str(end)+'</p>'
    for row in schedule(c,fields):
        label=STAGES.get(row['stage'],row['stage'])
        result+='<div class="shop-schedule-row"><a href="'+escape(path)+'?order='+row['id']+'#order-'+row['id']+'">'+escape(row['event_time'][:16].replace('T',' ')+' · '+row['title'])+'</a><p>'+escape(row['urgency']+' · '+row['assignee']+' · '+label+' · '+row['production']+(' · 진행 보류' if row['held'] else ''))+'</p><small>주문 '+row['id']+'</small></div>'
    return result+'</section>'
