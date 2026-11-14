"""Daily operator work tracking and customer-safe summaries."""
import datetime as dt
import json
import re
import secrets
from html import escape
from zoneinfo import ZoneInfo
import shop_admin_queries
import shop_admin_workflow as workflow
import order_amendments

SEOUL=ZoneInfo('Asia/Seoul')
STATES={'open':'미처리','done':'처리 완료'}


def now():return dt.datetime.now(SEOUL)


def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS shop_operator_tasks(id TEXT PRIMARY KEY,order_id TEXT NOT NULL,title TEXT NOT NULL,owner TEXT NOT NULL,due TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,note TEXT NOT NULL,updated TEXT NOT NULL)')
    c.execute('CREATE INDEX IF NOT EXISTS shop_task_order ON shop_operator_tasks(order_id,state,due)')
    c.execute('CREATE TABLE IF NOT EXISTS shop_operator_task_events(task_id TEXT NOT NULL,version INTEGER NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(task_id,version))')


def save_task(c,f):
    oid=f.get('order_id','');identity=f.get('task_id','')
    if not re.fullmatch('[a-f0-9]{32}',identity) or not c.execute('SELECT 1 FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?',(oid,)).fetchone():raise ValueError('주문과 업무 요청번호를 확인하세요.')
    old=c.execute('SELECT * FROM shop_operator_tasks WHERE id=?',(identity,)).fetchone()
    if old and old['order_id']!=oid:raise ValueError('다른 주문의 업무를 수정할 수 없습니다.')
    version=old['version'] if old else 0
    if f.get('task_version')!=str(version):raise ValueError('업무가 변경됐습니다. 최신 내용으로 다시 작성하세요.')
    data={key:f.get('task_'+key,'').strip() for key in ('title','owner','due','state','note')}
    if not 1<=len(data['title'])<=100 or not 1<=len(data['owner'])<=60 or len(data['note'])>1200 or data['state'] not in STATES:raise ValueError('업무명·담당자·상태·메모를 확인하세요.')
    if not old and data['state']!='open':raise ValueError('새 업무는 미처리로 등록하세요.')
    try:valid=dt.datetime.strptime(data['due'],'%Y-%m-%dT%H:%M').strftime('%Y-%m-%dT%H:%M')==data['due']
    except ValueError:valid=False
    if not valid:raise ValueError('업무 기한은 올바른 한국 시간으로 입력하세요.')
    if data['state']=='open' and (not old or old['state']!='open') and c.execute("SELECT count(*) FROM shop_operator_tasks WHERE order_id=? AND state='open'",(oid,)).fetchone()[0]>=30:raise ValueError('주문당 미처리 업무는 최대 30개입니다.')
    version+=1;changed=now().isoformat()
    c.execute('INSERT INTO shop_operator_tasks VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,owner=excluded.owner,due=excluded.due,state=excluded.state,version=excluded.version,note=excluded.note,updated=excluded.updated',(identity,oid,data['title'],data['owner'],data['due'],data['state'],version,data['note'],changed))
    c.execute('INSERT INTO shop_operator_task_events VALUES (?,?,?,?)',(identity,version,json.dumps(data,ensure_ascii=False),changed))


def quick_dates(c,path,clock=now):
    today=clock().astimezone(SEOUL).date();monday=today-dt.timedelta(days=today.weekday())
    body='<section class="event-shortcuts"><h2>행사 빠른 보기</h2><p>한국 시간 행사일 기준 · 모든 결제 상태 포함 · 기존 검색 조건은 초기화합니다.</p>'
    for label,start,end in [('오늘 행사',today,today),('이번 주 행사',monday,monday+dt.timedelta(days=6)),('앞으로 7일 행사',today,today+dt.timedelta(days=6))]:
        fields={'from_date':start.isoformat(),'to_date':end.isoformat()}
        count=c.execute("SELECT count(*) FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE substr(json_extract(o.details,'$.event_time'),1,10) BETWEEN ? AND ?",(fields['from_date'],fields['to_date'])).fetchone()[0]
        body+='<p><a href="'+escape(path+'?'+shop_admin_queries.query(fields)+'#admin-orders')+'">'+label+' · '+str(count)+'건</a> <small>'+str(start)+' ~ '+str(end)+'</small></p>'
    return body+'</section>'


def task_board(c,path,clock=now):
    current=clock().astimezone(SEOUL).strftime('%Y-%m-%dT%H:%M')
    total=c.execute("SELECT count(*) FROM shop_operator_tasks WHERE state='open'").fetchone()[0]
    overdue=c.execute("SELECT count(*) FROM shop_operator_tasks WHERE state='open' AND due<?",(current,)).fetchone()[0]
    body='<section class="task-board"><h2>미처리 업무</h2><p>업무 담당자는 배송 담당 배정과 별도입니다. 한국 시간 기준 · 미처리 '+str(total)+'건 / 기한 초과 '+str(overdue)+'건 · 기한순 최대 50건 표시</p><p><a href="'+escape(path+'?task=open#admin-orders')+'">미처리 업무 주문 전체 보기</a> · <a href="'+escape(path+'?task=overdue#admin-orders')+'">기한 초과 주문 보기</a></p>'
    for task in c.execute("SELECT * FROM shop_operator_tasks WHERE state='open' ORDER BY due,id LIMIT 50"):
        body+='<p><a href="'+escape(path+'?order='+task['order_id']+'#order-'+task['order_id'])+'">'+escape(task['title'])+'</a> · '+escape(task['owner'])+' · '+escape(task['due'].replace('T',' '))+(' · 기한 초과' if task['due']<current else '')+'</p>'
    return body+'</section>'


def task_fields(task):
    body=''
    for key,label,typ,limit in [('title','업무명','text',100),('owner','업무 담당자','text',60),('due','처리 기한 · 한국 시간','datetime-local',16)]:body+='<label>'+label+'<input name="task_'+key+'" type="'+typ+'" value="'+escape(str(task.get(key,'')))+'" required maxlength="'+str(limit)+'"></label>'
    body+='<label>업무 상태<select name="task_state">'+''.join('<option value="'+k+'"'+(' selected' if task.get('state','open')==k else '')+'>'+v+'</option>' for k,v in STATES.items())+'</select></label>'
    return body+'<label>처리 근거·남은 일<textarea name="task_note" maxlength="1200">'+escape(task.get('note',''))+'</textarea></label>'


def tasks(c,row,path):
    oid=row['id'];body='<details class="order-tasks"><summary>담당자·기한 업무 관리</summary><p>처리 완료는 업무 메모 상태입니다. 결제·배송·제작 상태를 변경하거나 알림을 보내지 않습니다. 기한이 지난 업무도 등록할 수 있으며 기한 초과로 표시합니다.</p>'
    records=c.execute("SELECT * FROM shop_operator_tasks WHERE order_id=? ORDER BY (state='open') DESC,due,id LIMIT 50",(oid,)).fetchall()
    for record in records:
        task=dict(record);fields=workflow.hidden('order_id',oid)+workflow.hidden('task_id',task['id'])+workflow.hidden('task_version',task['version'])+task_fields(task)
        body+='<details><summary>'+escape(task['title']+' · '+task['owner']+' · '+STATES[task['state']]+' · '+task['due'].replace('T',' '))+'</summary>'+workflow.form(path,'save_operator_task',fields,'업무 변경 저장')+'<h4>변경 이력 · 최근 10회</h4>'
        for event in c.execute('SELECT * FROM shop_operator_task_events WHERE task_id=? ORDER BY version DESC LIMIT 10',(task['id'],)):
            data=json.loads(event['details']);body+='<p>'+escape(event['created'][:16]+' · '+data['title']+' · '+data['owner']+' · '+data['due']+' · '+STATES[data['state']]+' · '+data['note'])+'</p>'
        body+='</details>'
    body+='<h4>새 업무 등록</h4>'+workflow.form(path,'save_operator_task',workflow.hidden('order_id',oid)+workflow.hidden('task_id',secrets.token_hex(16))+workflow.hidden('task_version',0)+task_fields({}),'업무 등록')
    return body+'<p>주문당 미처리 최대 30개 · 화면 최대 50개 · 전체 변경 기록은 서버에 보존합니다.</p></details>'


def customer_summary(c,row):
    order=json.loads(order_amendments.effective(c,row)['details'])
    state=shop_admin_queries.STATES.get(row['state'],'확인 필요')
    stage=c.execute('SELECT stage FROM shop_fulfillment WHERE order_id=?',(row['id'],)).fetchone()
    text=['벨라비타 주문 안내','주문번호: '+row['id'],'상품: '+order['title'],'결제 상태: '+state,'원결제금액: '+f'{order["amount"]:,}원','행사 시간: '+order['event_time'][:16].replace('T',' '),'행사장: '+order['venue'],'보내는 분: '+order['sender'],'문구: '+(order['message'] or ' / '.join(value for key,value in order['options'] if '문구' in key) or '선택 사항 확인')]
    if stage:text.append('배송 상태: '+shop_admin_queries.STAGES.get(stage['stage'],'확인 필요'))
    refunds=c.execute("SELECT COALESCE(sum(amount),0) FROM payment_events WHERE order_id=? AND kind='partial'",(row['id'],)).fetchone()[0]
    if c.execute("SELECT 1 FROM payment_events WHERE order_id=? AND kind='cancel'",(row['id'],)).fetchone():refunds=order['amount']
    if row['state'] in ('partial_cancel','cancelled'):text.append('취소 금액: '+(f'{refunds:,}원 (통보 기록 기준)' if refunds else '판매자 거래 내역 대조 필요'))
    return '\n'.join(text)


def summary_tools(c,row):
    text=customer_summary(c,row)
    return '<div class="customer-summary"><button type="button" data-copy-summary="'+escape(text)+'">고객 응대용 요약 복사</button><label>고객 전달 전 확인 · 내부 업무 메모 제외<textarea readonly class="summary-source">'+escape(text)+'</textarea></label></div>'
