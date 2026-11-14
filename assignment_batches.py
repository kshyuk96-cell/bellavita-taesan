"""CSV preview is immutable; only explicitly approved, still-current rows apply."""
import csv
import hashlib
import io
import json
import re
import secrets
from collections import Counter
from datetime import datetime,timezone
from html import escape
import shop_dispatch


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_assignment_batches (
        id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,plan TEXT NOT NULL,state TEXT NOT NULL,
        result TEXT,actor TEXT NOT NULL,created TEXT NOT NULL,applied TEXT)''')


def fingerprint(c,row,owner):
    assigned=shop_dispatch.current(c,row['id']);stage=c.execute('SELECT stage FROM shop_fulfillment WHERE order_id=?',(row['id'],)).fetchone()
    data=[row['details'],row['state'],assigned['version'] if assigned else 0,stage[0] if stage else '',shop_dispatch.roster(c).get(owner)]
    return hashlib.sha256(json.dumps(data,ensure_ascii=False).encode()).hexdigest()


def preview(c,fields,actor='admin-link'):
    content=fields.get('_csv_bytes') or fields.get('csv_text','').encode('utf-8')
    if not isinstance(content,bytes) or not content or len(content)>131072:raise ValueError('CSV는 128KB 이내로 업로드해 주세요.')
    key=fields.get('batch_attempt','')
    if not re.fullmatch(r'[a-f0-9]{32}',key):raise ValueError('일괄 배정 화면을 다시 열어 주세요.')
    digest=hashlib.sha256(content).hexdigest();old=c.execute('SELECT * FROM shop_assignment_batches WHERE id=?',(key,)).fetchone()
    if old:
        if old['source_hash']!=digest:raise ValueError('같은 사전 검토 번호의 CSV가 달라졌습니다.')
        return
    try:
        reader=csv.DictReader(io.StringIO(content.decode('utf-8-sig')),strict=True)
        if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames) or set(reader.fieldnames)-{'order_id','event_time','assignee','assignment_version','note'} or not {'order_id','event_time','assignee','assignment_version'}<=set(reader.fieldnames):raise ValueError('CSV 열은 order_id,event_time,assignee,assignment_version,note 입니다.')
        rows=list(reader)
    except (UnicodeError,csv.Error):raise ValueError('정상적인 UTF-8 CSV를 업로드해 주세요.') from None
    if not 1<=len(rows)<=80:raise ValueError('CSV는 1~80행까지 사전 검토합니다.')
    duplicates=Counter(r.get('order_id') for r in rows);people=shop_dispatch.roster(c);plan=[]
    for index,raw in enumerate(rows,1):
        errors=[];row=c.execute('SELECT o.* FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?',(raw.get('order_id'),)).fetchone()
        owner=raw.get('assignee');version=shop_dispatch.current(c,row['id']) if row else None
        if None in raw or any(v is None for v in raw.values()):errors.append('열 수 불일치')
        if duplicates[raw.get('order_id')]>1:errors.append('중복 주문 행')
        if not row:errors.append('쇼핑몰 주문 없음')
        if owner not in people:errors.append('등록 담당자 없음')
        if row:
            stage=c.execute('SELECT stage FROM shop_fulfillment WHERE order_id=?',(row['id'],)).fetchone()
            if row['state']=='cancelled' or stage and stage['stage']=='delivered':errors.append('취소·완료 주문')
            if raw.get('assignment_version')!=str(version['version'] if version else 0):errors.append('배정 버전 변경')
            try:
                actual=datetime.fromisoformat(json.loads(row['details'])['event_time']);given=datetime.fromisoformat(raw.get('event_time',''))
                if actual.tzinfo is None:actual=actual.replace(tzinfo=shop_dispatch.SEOUL)
                if given.tzinfo is None:given=given.replace(tzinfo=shop_dispatch.SEOUL)
                if actual!=given:errors.append('행사 일시 불일치')
            except (ValueError,TypeError):errors.append('행사 일시 형식')
        if len(raw.get('note') or '')>600:errors.append('근거 길이 초과')
        plan.append({'row':index,'input':raw,'errors':errors,'fingerprint':fingerprint(c,row,owner) if row and not errors else None})
    c.execute("INSERT INTO shop_assignment_batches VALUES (?,?,?,'preview',NULL,?,?,NULL)",(key,digest,json.dumps(plan,ensure_ascii=False),actor,datetime.now(timezone.utc).isoformat()))


def apply(c,fields,actor='admin-link'):
    batch=c.execute('SELECT * FROM shop_assignment_batches WHERE id=?',(fields.get('batch_id',''),)).fetchone()
    if not batch or fields.get('batch_checked')!='yes':raise ValueError('사전 검토 결과를 확인하고 적용할 행을 승인해 주세요.')
    if batch['state']=='applied':return
    plan=json.loads(batch['plan']);results=[]
    for item in plan:
        raw=item['input'];status='not_selected'
        if item['errors']:status='invalid'
        elif fields.get('batch_row_'+str(item['row']))=='yes':
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',(raw['order_id'],)).fetchone()
            if not row or fingerprint(c,row,raw['assignee'])!=item['fingerprint']:status='stale'
            else:
                shop_dispatch.assign(c,{'order_id':row['id'],'assignee':raw['assignee'],'assignment_version':raw['assignment_version'],'assignment_note':raw.get('note') or 'CSV 사전 검토 승인'},actor=actor+':batch:'+batch['id'])
                status='applied'
        results.append({'row':item['row'],'status':status})
    c.execute("UPDATE shop_assignment_batches SET state='applied',result=?,applied=? WHERE id=?",(json.dumps(results),datetime.now(timezone.utc).isoformat(),batch['id']))


def report(c,key):
    batch=c.execute('SELECT * FROM shop_assignment_batches WHERE id=?',(key,)).fetchone()
    if not batch:raise ValueError('일괄 배정 기록을 찾지 못했습니다.')
    statuses={r['row']:r['status'] for r in json.loads(batch['result'] or '[]')};output=io.StringIO();writer=csv.writer(output)
    writer.writerow(['row','order_id','event_time','assignee','status','errors'])
    def cell(value):
        value=str(value or '')
        return "'"+value if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\n','\r')) else value
    for item in json.loads(batch['plan']):
        raw=item['input'];writer.writerow([cell(v) for v in (item['row'],raw.get('order_id'),raw.get('event_time'),raw.get('assignee'),statuses.get(item['row'],'preview'),'; '.join(item['errors']))])
    return output.getvalue().encode('utf-8-sig')


def panel(c,path):
    result='<section><h2>일정·담당자 CSV 사전 검토</h2><p>UTF-8 CSV · 최대 80행/128KB · 열: order_id,event_time,assignee,assignment_version,note. 검토 후 승인한 유효 행만 배정합니다.</p>'
    result+=f'<form method="post" enctype="multipart/form-data" action="{escape(path)}"><input type="hidden" name="action" value="preview_assignment_csv"><input type="hidden" name="batch_attempt" value="{secrets.token_hex(16)}"><label>배정 CSV<input type="file" name="csv" accept=".csv,text/csv" required></label><button>CSV 사전 검토</button></form>'
    for batch in c.execute('SELECT * FROM shop_assignment_batches ORDER BY created DESC LIMIT 5'):
        result+=f'<details><summary>배정 검토 {batch["id"][:8]} · {batch["state"]}</summary><form method="post" action="{escape(path)}"><input type="hidden" name="action" value="apply_assignment_csv"><input type="hidden" name="batch_id" value="{batch["id"]}">'
        statuses={r['row']:r['status'] for r in json.loads(batch['result'] or '[]')}
        for item in json.loads(batch['plan']):
            raw=item['input'];result+='<p>'+escape(f'{item["row"]}행 · {raw.get("order_id")} · {raw.get("assignee")} · '+('; '.join(item['errors']) or statuses.get(item['row'],'적용 가능')))+'</p>'
            if batch['state']=='preview' and not item['errors']:result+=f'<label><input type="checkbox" name="batch_row_{item["row"]}" value="yes" checked>{item["row"]}행 적용 선택</label>'
        if batch['state']=='preview':result+='<label><input type="checkbox" name="batch_checked" value="yes" required>사전 검토 결과와 적용 행을 확인했습니다</label><button>선택한 배정 행 적용</button>'
        result+='</form><p><a href="'+escape(path)+'/assignment-batch.csv?batch='+batch['id']+'">검토·적용 결과 CSV</a></p></details>'
    return result+'</section>'
