"""Shared operator notes, response templates and shift handover summaries."""
import datetime as dt
import json
import re
import secrets
import string
from html import escape
import shop_admin_workflow as workflow
import order_amendments
import shop_admin_queries
import shop_admin_shortcuts as shortcuts
from urllib.parse import urlencode

PRIORITIES={'high':'높음','normal':'보통','low':'낮음'}
PRIORITY_SQL="CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"
TOKENS={'주문번호','상품','결제상태','배송상태','행사시간','행사장','보내는분','문구','원결제금액','요약'}
DEFAULTS=[('주문 안내','{요약}'),('결제 확인 안내','주문번호 {주문번호}\n현재 결제 상태: {결제상태}\n원결제금액: {원결제금액}\n거래 내역을 확인한 후 안내드리겠습니다.'),('배송 확인 안내','주문번호 {주문번호}\n행사 시간: {행사시간}\n행사장: {행사장}\n기록된 배송 상태: {배송상태}\n실제 설치 진행 상황을 확인한 후 안내드리겠습니다.')]


def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS shop_response_templates(id TEXT PRIMARY KEY,name TEXT NOT NULL,body TEXT NOT NULL,active INTEGER NOT NULL,version INTEGER NOT NULL,updated TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS shop_response_template_events(template_id TEXT NOT NULL,version INTEGER NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(template_id,version))')
    c.execute('CREATE TABLE IF NOT EXISTS shop_product_notes(product_id TEXT NOT NULL,version INTEGER NOT NULL,author TEXT NOT NULL,note TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(product_id,version))')


def validate_template(body):
    if not 1<=len(body.strip())<=1600:raise ValueError('응대 문구는 1~1600자로 입력하세요.')
    try:
        for _,key,spec,conversion in string.Formatter().parse(body):
            if key is not None and (key not in TOKENS or spec or conversion):raise ValueError('허용된 치환 항목만 사용하세요.')
    except ValueError:raise ValueError('중괄호와 치환 항목을 확인하세요. 사용 가능: '+', '.join(sorted(TOKENS))) from None


def save_template(c,f):
    identity=f.get('template_id','');name=f.get('template_name','').strip();body=f.get('template_body','').strip()
    if not re.fullmatch('[a-f0-9]{32}',identity) or not 1<=len(name)<=60:raise ValueError('템플릿 이름·요청번호를 확인하세요.')
    validate_template(body)
    old=c.execute('SELECT * FROM shop_response_templates WHERE id=?',(identity,)).fetchone();version=old['version'] if old else 0
    if f.get('template_version')!=str(version):raise ValueError('템플릿이 변경됐습니다. 최신 내용을 다시 확인하세요.')
    if not old and c.execute('SELECT count(*) FROM shop_response_templates').fetchone()[0]>=20:raise ValueError('공유 템플릿은 최대 20개입니다. 기존 템플릿을 수정하세요.')
    if c.execute('SELECT 1 FROM shop_response_templates WHERE name=? AND id<>?',(name,identity)).fetchone():raise ValueError('같은 템플릿 이름이 있습니다.')
    active=int(f.get('template_active')=='yes');changed=workflow.now();version+=1
    c.execute('INSERT INTO shop_response_templates VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,body=excluded.body,active=excluded.active,version=excluded.version,updated=excluded.updated',(identity,name,body,active,version,changed))
    c.execute('INSERT INTO shop_response_template_events VALUES (?,?,?,?)',(identity,version,json.dumps({'name':name,'body':body,'active':bool(active)},ensure_ascii=False),changed))


def template_fields(record):
    return workflow.hidden('template_id',record.get('id',secrets.token_hex(16)))+workflow.hidden('template_version',record.get('version',0))+'<label>템플릿 이름<input name="template_name" maxlength="60" required value="'+escape(record.get('name',''))+'"></label><label>응대 문구<textarea name="template_body" maxlength="1600" required>'+escape(record.get('body',''))+'</textarea></label><label><input type="checkbox" name="template_active" value="yes"'+(' checked' if record.get('active',1) else '')+'>주문에서 사용</label>'


def templates(c,path):
    body='<section class="response-template-settings"><h2>고객 응대 템플릿</h2><p>공유 문구 최대 20개 · 자동 발송하지 않습니다. 실제 주문 상태와 문구를 검토한 뒤 해당 고객에게만 전달하세요.</p><p>사용 가능: '+escape(' '.join('{'+key+'}' for key in sorted(TOKENS)))+'</p>'
    for row in c.execute('SELECT * FROM shop_response_templates ORDER BY name,id'):
        body+='<details><summary>'+escape(row['name'])+(' · 사용 중' if row['active'] else ' · 사용 안 함')+'</summary>'+workflow.form(path,'save_response_template',template_fields(dict(row)),'템플릿 저장')+'<h4>최근 변경 5회</h4>'
        for event in c.execute('SELECT * FROM shop_response_template_events WHERE template_id=? ORDER BY version DESC LIMIT 5',(row['id'],)):
            d=json.loads(event['details']);body+='<p>'+escape(event['created'][:16]+' UTC · '+d['name']+' · '+('사용' if d['active'] else '사용 안 함'))+'</p><p class="preserve-lines">'+escape(d['body'])+'</p>'
        body+='</details>'
    return body+'<details><summary>새 템플릿 등록</summary>'+workflow.form(path,'save_response_template',template_fields({}),'템플릿 등록')+'</details></section>'


def response_tools(c,row):
    from shop_admin_daily import customer_summary
    order=json.loads(order_amendments.effective(c,row)['details']);stage=c.execute('SELECT stage FROM shop_fulfillment WHERE order_id=?',(row['id'],)).fetchone()
    values={'요약':customer_summary(c,row),'주문번호':row['id'],'상품':order['title'],'결제상태':shop_admin_queries.STATES.get(row['state'],'확인 필요'),'배송상태':shop_admin_queries.STAGES.get(stage['stage'],'확인 필요') if stage else '결제 확인 후 시작','행사시간':order['event_time'][:16].replace('T',' '),'행사장':order['venue'],'보내는분':order['sender'],'문구':order['message'] or ' / '.join(v for k,v in order['options'] if '문구' in k) or '선택 사항 확인','원결제금액':f'{order["amount"]:,}원'}
    templates=[('builtin:'+str(i),name,body) for i,(name,body) in enumerate(DEFAULTS)]+[(r['id'],r['name'],r['body']) for r in c.execute('SELECT id,name,body FROM shop_response_templates WHERE active=1 ORDER BY name,id')]
    favorite=shortcuts.favorite_keys(c);templates.sort(key=lambda item:item[0] not in favorite)
    options=''
    initial=''
    for key,name,body in templates:
        validate_template(body);rendered=body.format_map(values)
        if not initial:initial=rendered
        options+='<option value="'+escape(rendered)+'">'+escape(('★ ' if key in favorite else '')+name)+'</option>'
    return '<details class="response-tools copy-panel"><summary>고객 응대 문구 선택</summary><label>응대 템플릿<select class="response-choice">'+options+'</select></label><label>고객 전달 전 검토<textarea class="copy-field response-text" readonly>'+escape(initial)+'</textarea></label><button type="button" data-copy-field>응대 문구 복사</button><p>복사 후 실제 상태를 확인해 전달하세요. 내부 메모는 치환하지 않습니다.</p></details>'


def save_product_note(c,f):
    identity=f.get('product_id','');author=f.get('note_author','').strip();note=f.get('product_note','').strip()
    if not c.execute('SELECT 1 FROM shop_products WHERE id=?',(identity,)).fetchone() or not 1<=len(author)<=60 or len(note)>2000:raise ValueError('상품·작성자·메모 길이를 확인하세요.')
    version=c.execute('SELECT COALESCE(MAX(version),0) FROM shop_product_notes WHERE product_id=?',(identity,)).fetchone()[0]
    if f.get('note_version')!=str(version):raise ValueError('상품 운영 메모가 변경됐습니다. 최신 내용을 확인하세요.')
    c.execute('INSERT INTO shop_product_notes VALUES (?,?,?,?,?)',(identity,version+1,author,note,workflow.now()))


def product_note(c,identity,path):
    records=c.execute('SELECT * FROM shop_product_notes WHERE product_id=? ORDER BY version DESC LIMIT 10',(identity,)).fetchall();latest=records[0] if records else None
    fields=workflow.hidden('product_id',identity)+workflow.hidden('note_version',latest['version'] if latest else 0)+'<label>작성자<input name="note_author" maxlength="60" required value="'+escape(latest['author'] if latest else '')+'"></label><label>상품 운영 메모 · 직원 내부용<textarea name="product_note" maxlength="2000">'+escape(latest['note'] if latest else '')+'</textarea></label>'
    body='<details class="product-note"><summary>상품 운영 메모 · 내부용</summary><p>고객 쇼핑몰·주문 요약·제작 파일에는 표시하지 않습니다. 상품 가격·옵션·리비전은 바꾸지 않습니다. 빈 메모 저장은 현재 메모를 비웁니다.</p>'+workflow.form(path,'save_product_note',fields,'운영 메모 저장')+'<h4>최근 메모 10회</h4>'
    for record in records:body+='<p>'+escape(record['created'][:16]+' UTC · '+record['author'])+'</p><p class="preserve-lines">'+escape(record['note'] or '(빈 메모)')+'</p>'
    return shortcuts.note_preview(latest)+body+'</details>'


def focus(c,row):
    order=json.loads(order_amendments.effective(c,row)['details'])
    pairs=[('행사 시간',order['event_time'][:16].replace('T',' ')),('설치 장소',order['venue']+' · '+order['address']),('보내는 분',order['sender']),('문구',order['message'] or ' / '.join(v for k,v in order['options'] if '문구' in k) or '선택 사항 확인'),('선택 사항',' / '.join(k+' '+v for k,v in order['options']))]
    return '<div class="order-focus"><h3>작업 핵심 정보</h3><p>승인된 정정 반영 · 원접수 정보는 아래 기록에서 확인합니다.</p><dl>'+''.join('<dt>'+label+'</dt><dd>'+escape(value)+'</dd>' for label,value in pairs)+'</dl></div>'


def shift_report(c,fields,clock=None):
    from shop_admin_daily import now,SEOUL
    current=(clock or now)().astimezone(SEOUL);owner=fields.get('handover_owner','').strip();until=fields.get('handover_until','').strip()
    if len(owner)>60:raise ValueError('교대 담당자 검색은 60자 이내로 입력하세요.')
    if until:
        try:valid=dt.date.fromisoformat(until).isoformat()==until
        except ValueError:valid=False
        if not valid:raise ValueError('교대 요약의 기한 날짜를 확인하세요.')
    clauses=["state='open'"];args=[]
    if owner:clauses.append('instr(lower(owner),lower(?))>0');args.append(owner)
    if until:clauses.append('substr(due,1,10)<=?');args.append(until)
    where=' WHERE '+' AND '.join(clauses);total=c.execute('SELECT count(*) FROM shop_operator_tasks'+where,args).fetchone()[0]
    rows=c.execute('SELECT * FROM shop_operator_tasks'+where+' ORDER BY '+PRIORITY_SQL+',due,id LIMIT 100',args).fetchall()
    lines=['직원 내부 인수인계용 · 고객 전달 금지','기준 시각: '+current.strftime('%Y-%m-%d %H:%M')+' 한국 시간','미처리 '+str(total)+'건 중 '+str(len(rows))+'건 · 우선순위/기한순','담당자 조건: '+(owner or '전체')+' · 기한까지: '+(until or '전체')]
    for row in rows:lines.append('주문 '+row['order_id']+' | '+PRIORITIES[row['priority']]+' | '+row['title']+' | '+row['owner']+' | '+row['due'].replace('T',' ')+' | '+row['note'])
    if total>len(rows):lines.append('100건을 초과했습니다. 담당자·기한 조건을 좁혀 다시 생성하세요.')
    if not rows:lines.append('조건에 맞는 미처리 업무 없음')
    return {'owner':owner,'until':until,'text':'\n'.join(lines)}


def shift_summary(c,path,fields,clock=None):
    from shop_admin_launch import snapshot_form
    report=shift_report(c,fields,clock);owner,until=report['owner'],report['until']
    query='<form method="get" action="'+escape(path)+'"><label>교대 업무 담당자<input name="handover_owner" maxlength="60" value="'+escape(owner)+'"></label><label>이 날짜까지의 기한<input type="date" name="handover_until" value="'+escape(until)+'"></label><button>교대 요약 갱신</button></form>'
    return '<section class="shift-summary copy-panel"><h2>교대 미처리 업무 요약</h2><p>전체 주문의 미처리 업무만 포함합니다. 화면 생성 시점의 기록이며 고객 정보 대신 주문번호로 인계합니다. 담당자·기한 조건은 이 요약에만 적용합니다.</p>'+query+'<p><a class="shift-download" href="'+escape(path+'/shift-summary.txt?'+urlencode({'handover_owner':owner,'handover_until':until}))+'">교대 요약 파일 다운로드</a> · 직원 내부용 · 다운로드 시점의 최신 기록</p><label>직원 내부 전달용<textarea class="copy-field shift-text" readonly>'+escape(report['text'])+'</textarea></label><button type="button" data-copy-field>교대 요약 복사</button>'+snapshot_form(path,owner,until)+'</section>'


SCRIPT=r'''
for(const select of document.querySelectorAll('.response-choice'))select.addEventListener('change',()=>{select.closest('.response-tools').querySelector('.response-text').value=select.value;});
for(const button of document.querySelectorAll('[data-copy-field]'))button.addEventListener('click',async()=>{const field=button.closest('.copy-panel').querySelector('.copy-field');const status=document.getElementById('shop-action-status');try{if(!navigator.clipboard)throw Error('clipboard');await navigator.clipboard.writeText(field.value);status.textContent='복사했습니다. 고객용 문구와 직원 내부용 요약의 전달 대상을 확인하세요.';}catch(error){field.focus();field.select();status.textContent='내용을 선택했습니다. 직접 복사해 주세요.';}});
'''
