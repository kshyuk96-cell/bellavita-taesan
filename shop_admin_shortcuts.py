"""Versioned shared favorites and safe operator task shortcuts."""
import re
from html import escape
import shop_admin_workflow as workflow


def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS shop_response_favorites(template_key TEXT PRIMARY KEY,version INTEGER NOT NULL,favorite INTEGER NOT NULL,updated TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS shop_response_favorite_events(template_key TEXT NOT NULL,version INTEGER NOT NULL,favorite INTEGER NOT NULL,changed TEXT NOT NULL,PRIMARY KEY(template_key,version))')


def quick_task(c,f):
    from shop_admin_daily import save_task
    row=c.execute('SELECT * FROM shop_operator_tasks WHERE id=? AND order_id=?',(f.get('task_id',''),f.get('order_id',''))).fetchone()
    target=f.get('task_target')
    if not row or target not in ('open','done') or row['state']==target or str(row['version'])!=f.get('task_version'):raise ValueError('업무 상태가 변경됐습니다. 최신 기록에서 다시 확인하세요.')
    fields={key:f.get(key,'') for key in ('order_id','task_id','task_version')}
    fields.update({'task_'+key:row[key] for key in ('title','owner','due','priority','note')});fields['task_state']=target
    save_task(c,fields)


def quick_form(path,task):
    target='done' if task['state']=='open' else 'open'
    fields=''.join(workflow.hidden(key,value) for key,value in [('order_id',task['order_id']),('task_id',task['id']),('task_version',task['version']),('task_target',target)])
    return '<div class="quick-task">'+workflow.form(path,'quick_operator_task',fields,'업무 빠른 완료' if target=='done' else '업무 다시 열기')+'</div>'


def favorite_keys(c):return {row[0] for row in c.execute('SELECT template_key FROM shop_response_favorites WHERE favorite=1')}


def toggle_favorite(c,f):
    key=f.get('favorite_key','');value=f.get('favorite_target')
    if value not in ('0','1'):raise ValueError('즐겨찾기 상태를 확인하세요.')
    if key not in ('builtin:0','builtin:1','builtin:2'):
        row=c.execute('SELECT active FROM shop_response_templates WHERE id=?',(key,)).fetchone() if re.fullmatch('[a-f0-9]{32}',key) else None
        if not row or value=='1' and not row['active']:raise ValueError('사용 가능한 응대 문구를 선택하세요.')
    old=c.execute('SELECT * FROM shop_response_favorites WHERE template_key=?',(key,)).fetchone();version=old['version'] if old else 0
    if f.get('favorite_version')!=str(version) or (old['favorite'] if old else 0)==int(value):raise ValueError('즐겨찾기가 변경됐습니다. 최신 화면을 확인하세요.')
    changed=workflow.now();version+=1
    c.execute('INSERT INTO shop_response_favorites VALUES (?,?,?,?) ON CONFLICT(template_key) DO UPDATE SET version=excluded.version,favorite=excluded.favorite,updated=excluded.updated',(key,version,int(value),changed))
    c.execute('INSERT INTO shop_response_favorite_events VALUES (?,?,?,?)',(key,version,int(value),changed))


def favorites_panel(c,path):
    from shop_admin_team import DEFAULTS
    rows=[('builtin:'+str(i),name,1) for i,(name,_) in enumerate(DEFAULTS)]+[(r['id'],r['name'],r['active']) for r in c.execute('SELECT id,name,active FROM shop_response_templates ORDER BY name,id')]
    body='<section class="response-favorites"><h2>응대 문구 즐겨찾기</h2><p>관리자 공유 설정 · 즐겨찾기를 주문의 문구 목록 앞에 표시하고 첫 문구를 미리 채웁니다. 사용 해제된 템플릿은 주문에서 숨깁니다. 자동 발송하지 않습니다.</p>'
    for key,name,active in rows:
        old=c.execute('SELECT * FROM shop_response_favorites WHERE template_key=?',(key,)).fetchone();favorite=bool(old and old['favorite']);version=old['version'] if old else 0
        body+='<div class="favorite-item"><strong>'+escape(name)+(' · 사용 안 함' if not active else '')+'</strong>'
        if active or favorite:
            fields=workflow.hidden('favorite_key',key)+workflow.hidden('favorite_version',version)+workflow.hidden('favorite_target','0' if favorite else '1')
            body+=workflow.form(path,'toggle_response_favorite',fields,('즐겨찾기 해제 · ' if favorite else '즐겨찾기 추가 · ')+escape(name))
        body+='</div>'
    return body+'</section>'


def note_preview(latest):
    if not latest or not latest['note'].strip():return '<aside class="product-note-preview"><strong>내부 운영 메모</strong><p>운영 메모 없음</p></aside>'
    note=latest['note'];preview=note[:120]+('…' if len(note)>120 else '')
    return '<aside class="product-note-preview"><strong>내부 운영 메모 요약</strong><p class="preserve-lines">'+escape(preview)+'</p><small>'+escape(latest['author']+' · '+latest['created'][:16]+' UTC')+'</small><p>전체 내용은 아래 상품 운영 메모에서 확인하세요.</p></aside>'
