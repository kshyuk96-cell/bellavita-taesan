"""Reversible staff list/fulfillment overlays; source orders and payments are immutable."""
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

FIELD_LIMITS={'date':10,'time':5,'address':1000,'recipient':250,'sender':250,'size':160,'artwork':100,'message':2000,'deliveryChannel':200,'photoTo':200,'note':2000,'buyer':160,'buyerPhone':60}

def path(root):return Path(root)/'invitation-order-overrides.sqlite3'
def source_key(value):
    if not isinstance(value,str) or len(value)>200 or not re.fullmatch(r'payapp:[A-Za-z0-9_-]{1,150}',value):raise ValueError('초대장 주문 번호를 확인하세요.')
    return value.split(':',1)[1]
def metadata(root):
    file=path(root)
    if not file.is_file():return {}
    with sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True,timeout=10) as db:
        db.row_factory=sqlite3.Row
        return {row['source_id']:dict(row) for row in db.execute('SELECT * FROM overrides')}
def hidden_ids(root):return [source_key(key) for key,row in metadata(root).items() if row['hidden']]
def apply(row,meta):
    current=meta.get(row['id']) or {};fields={}
    try:fields=json.loads(current.get('fields') or '{}')
    except (ValueError,TypeError):pass
    if not isinstance(fields,dict):fields={}
    result={**row,'fields':dict(row['fields']),'editRevision':current.get('revision',0),'hidden':bool(current.get('hidden'))}
    for key,value in fields.items():
        if key in ('buyer','buyerPhone'):result[key]=value
        elif key in FIELD_LIMITS:result['fields'][key]=value
    if current.get('updated_at'):result.update(editedAt=current['updated_at'],editedBy=current.get('updated_by',''))
    if current.get('erp_order_id'):result['erpOrderId']=current['erp_order_id']
    return result

def clean_fields(fields):
    if not isinstance(fields,dict) or not fields:raise ValueError('수정할 주문 정보를 입력하세요.')
    cleaned={}
    for key,value in fields.items():
        if key not in FIELD_LIMITS or not isinstance(value,str) or len(value)>FIELD_LIMITS[key]:raise ValueError('배송·고객 정보만 수정할 수 있습니다. 금액·결제 상태·상품 번호는 변경할 수 없습니다.')
        cleaned[key]=value.strip()
    if cleaned.get('date'):
        try:
            if not re.fullmatch(r'20\d\d-\d\d-\d\d',cleaned['date']):raise ValueError()
            dt.date.fromisoformat(cleaned['date'])
        except ValueError:raise ValueError('행사 날짜를 YYYY-MM-DD로 확인하세요.') from None
    if cleaned.get('time') and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',cleaned['time']):raise ValueError('행사 시각을 HH:MM으로 확인하세요.')
    return cleaned

def source_exists(root,source):
    file=Path(root)/'state.sqlite3'
    if not file.is_file():return False
    with sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True,timeout=10) as db:
        return db.execute('SELECT 1 FROM invitation_orders WHERE id=?',(source,)).fetchone() is not None

def update(root,payload,actor):
    if not isinstance(payload,dict):raise ValueError('주문 수정 요청을 확인하세요.')
    source_id=payload.get('sourceId');source=source_key(source_id);action=payload.get('action')
    if action not in ('edit','delete','restore'):raise ValueError('주문 수정·목록 삭제 요청을 확인하세요.')
    if not source_exists(root,source):raise ValueError('초대장 주문을 찾지 못했습니다. 주문 목록을 새로고침하세요.')
    fields=clean_fields(payload.get('fields')) if action=='edit' else None
    revision=payload.get('revision')
    if type(revision) is not int or revision<0:raise ValueError('주문 수정 버전을 확인하세요.')
    with sqlite3.connect(path(root),timeout=10) as db:
        db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS overrides(source_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,fields TEXT NOT NULL,hidden INTEGER NOT NULL,erp_order_id TEXT NOT NULL,updated_at TEXT NOT NULL,updated_by TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL,revision INTEGER NOT NULL,action TEXT NOT NULL,fields TEXT NOT NULL,at TEXT NOT NULL,actor TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM overrides WHERE source_id=?',(source_id,)).fetchone()
        old=dict(row) if row else {'revision':0,'fields':'{}','hidden':0,'erp_order_id':''}
        if action=='delete' and old['hidden'] or action=='restore' and row and not old['hidden']:
            return {'success':True,'sourceId':source_id,'editRevision':old['revision'],'hidden':bool(old['hidden']),'replayed':True}
        if revision!=old['revision']:raise ValueError('다른 직원이 수정했습니다. 주문 목록을 새로고침하세요.')
        if action=='edit' and old.get('erp_order_id'):raise ValueError('ERP에 등록된 주문입니다. 운영현황에서 수정하세요.')
        if action=='edit' and old['hidden']:raise ValueError('목록에서 삭제한 주문입니다. 먼저 복원하세요.')
        changed=json.loads(old['fields']);changed.update(fields or {})
        hidden=1 if action=='delete' else 0 if action=='restore' else old['hidden'];at=dt.datetime.now(dt.timezone.utc).isoformat();next_revision=revision+1;who=str(actor)[:160]
        db.execute('INSERT INTO overrides VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET revision=excluded.revision,fields=excluded.fields,hidden=excluded.hidden,updated_at=excluded.updated_at,updated_by=excluded.updated_by',(source_id,next_revision,json.dumps(changed,ensure_ascii=False),hidden,old['erp_order_id'],at,who))
        db.execute('INSERT INTO history(source_id,revision,action,fields,at,actor) VALUES(?,?,?,?,?,?)',(source_id,next_revision,action,json.dumps(fields or {},ensure_ascii=False),at,who))
    return {'success':True,'sourceId':source_id,'editRevision':next_revision,'hidden':bool(hidden),'replayed':False}
