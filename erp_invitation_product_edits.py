"""Versioned native-product text overlays; immutable snapshots and paid orders stay intact."""
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

FIELD_LIMITS={'title':300,'venue':300,'address':1000,'orderNotice':2000}
NATIVE_ID=re.compile(r'own:([a-f0-9]{32}):(93500|99000)')

def path(root):return Path(root)/'invitation-product-overrides.sqlite3'
def key(job,price):
    if not re.fullmatch(r'[a-f0-9]{32}',str(job)) or str(price) not in ('93500','99000'):raise ValueError('자체 상품 번호를 확인하세요.')
    return 'own:'+str(job)+':'+str(price)

def metadata(root):
    file=path(root)
    if not file.is_file():return {}
    with sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True,timeout=10) as db:
        db.row_factory=sqlite3.Row
        return {row['product_key']:dict(row) for row in db.execute('SELECT * FROM overrides')}

def apply(root,job,price,product,meta=None):
    current=(metadata(root) if meta is None else meta).get(key(job,price)) or {}
    fields=json.loads(current.get('fields') or '{}')
    result={**product,**{name:value for name,value in fields.items() if name in FIELD_LIMITS}}
    result['_productEditRevision']=current.get('revision',0)
    if current.get('updated_at'):result.update(_productEditedAt=current['updated_at'],_productEditedBy=current.get('updated_by',''))
    return result

def resolve(root,product_id):
    if not isinstance(product_id,str) or not 1<=len(product_id)<=160:raise ValueError('상품 번호를 확인하세요.')
    direct=NATIVE_ID.fullmatch(product_id)
    if direct:return direct[1],direct[2]
    if not re.fullmatch(r'\d{1,40}',product_id):raise ValueError('외부 상품은 연결된 판매처에서 수정하세요.')
    matches=[];base=Path(root)/'invitations'
    if base.is_dir():
        for folder in base.iterdir():
            if not re.fullmatch(r'[a-f0-9]{32}',folder.name) or not folder.is_dir():continue
            for price in ('93500','99000'):
                try:
                    product=json.loads((folder/(price+'.order.json')).read_text(encoding='utf-8'))
                    if str(product.get('number',''))==product_id:matches.append((folder.name,price))
                except (OSError,ValueError,TypeError):continue
    if len(matches)!=1:raise ValueError('자체 주문서 상품을 정확히 찾지 못했습니다. 상품 목록을 새로고침하세요.')
    return matches[0]

def read_product(root,job,price):
    import checkout
    product=checkout.read(Path(root),job,str(price))
    # The URL selects an immutable snapshot, not its payable amount. Legacy
    # payment-test snapshots may legitimately contain 1,000 won at /93500/order.
    if not isinstance(product,dict) or type(product.get('price')) is not int or product['price']<=0:raise ValueError('발행된 자체 주문서 상품을 찾지 못했습니다.')
    return product

def describe(product_id,product):
    fields={name:str(product.get(name) or '') for name in FIELD_LIMITS}
    revision=product.get('_productEditRevision',0)
    return {'productId':product_id,'revision':revision,'productEditRevision':revision,'price':product['price'],
            'canEditProduct':True,'fields':fields,**fields,
            'editedAt':product.get('_productEditedAt',''),'editedBy':product.get('_productEditedBy','')}

def detail(root,product_id):
    job,price=resolve(root,product_id)
    return describe(product_id,read_product(root,job,price))

def clean_fields(fields):
    if not isinstance(fields,dict) or not fields:raise ValueError('수정할 상품 정보를 입력하세요.')
    cleaned={}
    for name,value in fields.items():
        if name not in FIELD_LIMITS or not isinstance(value,str) or len(value)>FIELD_LIMITS[name]:raise ValueError('상품명·장소·주소·주문 안내만 수정할 수 있습니다. 가격·상품 번호·결제 정보는 변경할 수 없습니다.')
        cleaned[name]=value.strip()
        if name!='orderNotice' and not cleaned[name]:raise ValueError('상품명·장소·주소는 빈칸으로 저장할 수 없습니다.')
    return cleaned

def update(root,payload,actor='ERP 직원'):
    if not isinstance(payload,dict):raise ValueError('상품 수정 요청을 확인하세요.')
    product_id=payload.get('productId');job,price=resolve(root,product_id)
    read_product(root,job,price)
    revision=payload.get('revision');fields=clean_fields(payload.get('fields'))
    if type(revision) is not int or revision<0:raise ValueError('상품 수정 버전을 확인하세요.')
    product_key=key(job,price)
    with sqlite3.connect(path(root),timeout=10) as db:
        db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS overrides(product_key TEXT PRIMARY KEY,revision INTEGER NOT NULL,fields TEXT NOT NULL,updated_at TEXT NOT NULL,updated_by TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,product_key TEXT NOT NULL,revision INTEGER NOT NULL,fields TEXT NOT NULL,at TEXT NOT NULL,actor TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT * FROM overrides WHERE product_key=?',(product_key,)).fetchone()
        if revision!=(old['revision'] if old else 0):raise ValueError('다른 직원이 상품을 수정했습니다. 상품 정보를 새로고침하세요.')
        changed=json.loads(old['fields']) if old else {};changed.update(fields)
        at=dt.datetime.now(dt.timezone.utc).isoformat();who=str(actor)[:160]
        db.execute('INSERT INTO overrides VALUES(?,?,?,?,?) ON CONFLICT(product_key) DO UPDATE SET revision=excluded.revision,fields=excluded.fields,updated_at=excluded.updated_at,updated_by=excluded.updated_by',(product_key,revision+1,json.dumps(changed,ensure_ascii=False),at,who))
        db.execute('INSERT INTO history(product_key,revision,fields,at,actor) VALUES(?,?,?,?,?)',(product_key,revision+1,json.dumps(fields,ensure_ascii=False),at,who))
    return {'success':True,**detail(root,product_id)}
