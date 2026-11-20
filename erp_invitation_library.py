"""Published invitation pairs, lifetime and a shared checkout sales gate.

The manifest switch is atomic: both prices always use the same lifecycle/sales state.
Public reads use stored HTML; no generation or model request runs on a page view.
"""
import datetime as dt
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit
import erp_manual_blogpay
from zoneinfo import ZoneInfo
from lxml import html

SEOUL=ZoneInfo('Asia/Seoul')
LOCK=threading.RLock()
PRICES=(93500,99000)

def now():return dt.datetime.now(dt.timezone.utc)
def base(root):return Path(root)/'erp-invitation-library'
def state(root):
    try:return json.loads((base(root)/'state.json').read_text())
    except FileNotFoundError:return {}
def save(root,data):
    folder=base(root);folder.mkdir(parents=True,exist_ok=True)
    tmp=folder/'state.tmp';tmp.write_text(json.dumps(data,ensure_ascii=False));tmp.replace(folder/'state.json')
def timestamp(value):
    value=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
def deadline(kind,created,date):
    if kind=='memorial':return timestamp(created)+dt.timedelta(hours=72)
    return dt.datetime.combine(dt.date.fromisoformat(date)+dt.timedelta(days=1),dt.time(),SEOUL)
def status(row,at=None):
    try:end=deadline(row['kind'],row['createdAt'],row['date']);active=(at or now())<end
    except (KeyError,ValueError,TypeError):end=None;active=False
    return {'operating':active,'operatingUntil':end.isoformat() if end else None,
            'salesStopped':row.get('salesStopped',False),'saleStatus':('운영 종료' if not active else '판매 중지' if row.get('salesStopped') else '판매 중')}
def register(worker,root,pending=None):
    """Discover successful publications and legacy HTML pairs. Never count previews."""
    with LOCK:
        data=state(root);changed=False
        with worker.db() as c:rows=c.execute("SELECT * FROM jobs WHERE action='publish' AND state='done' ORDER BY created_at").fetchall()
        if pending is not None:rows=[*rows,pending]
        for row in rows:
            try:
                job=json.loads(row['input']);result=json.loads(row['result']);key=result.get('jobId') or (row['public_id'] if 'public_id' in row.keys() else '');v=job['values']
                if not re.fullmatch('[a-f0-9]{32}',key) or len(result.get('items',[]))!=2:continue
                if key in data and (data[key].get('orderMode')=='blogpay-manual' or data[key].get('orderRewrites') or all('/i/' in x.get('orderUrl','') for x in data[key]['links'])):continue
                old_row=data.get(key,{})
                folder=worker.root/'public'/key
                if not all((folder/f'{p}.html').is_file() for p in PRICES):continue
                data[key]={'id':key,'kind':job['kind'],'createdAt':result.get('publishedAt') or dt.datetime.fromtimestamp((folder/'result.json').stat().st_mtime,dt.timezone.utc).isoformat(),'date':v.get('date',''),'title':v.get('productPrefix') or v.get('name',''),'folder':str(folder),'links':result['items'],'revision':1,'salesStopped':False,'invitationId':job.get('invitationId',''),'orderMode':job.get('orderMode',''),**old_row};changed=True
                # Earlier ERP publications used BlogPay links. Reuse the approved
                # own-checkout snapshots so every managed pair shares the same gate.
                if job.get('orderMode')!='blogpay-manual' and any('/i/' not in x.get('orderUrl','') for x in result['items']):
                    import erp_local_checkout
                    local_key,links,snapshots=erp_local_checkout.build(root,worker.origin,job)
                    erp_local_checkout.publish(root,local_key,snapshots,key,result['fingerprint'],worker.origin,job['kind']=='wedding',v.get('weddingCrop'))
                    data[key]['orderRewrites']={old['orderUrl']:new['orderUrl'] for old,new in zip(sorted(result['items'],key=lambda x:x['price']),links)}
                    data[key]['links']=[{**old,**new} for old,new in zip(sorted(result['items'],key=lambda x:x['price']),links)]

            except (ValueError,KeyError,TypeError,OSError):continue
        legacy=Path(root)/'invitations'
        if legacy.is_dir():
            for folder in legacy.iterdir():
                key=folder.name
                if not re.fullmatch('[a-f0-9]{32}',key) or key in data or not all((folder/f'{p}.html').is_file() for p in PRICES):continue
                try:
                    product=json.loads((folder/'93500.order.json').read_text())
                    if product.get('localCheckout'):continue
                    created=dt.datetime.fromtimestamp(min((folder/f'{p}.html').stat().st_mtime for p in PRICES),dt.timezone.utc).isoformat()
                    data[key]={'id':key,'kind':product.get('kind','wedding'),'createdAt':created,'date':product.get('date') or str(product.get('event_time',''))[:10],'title':product.get('title',''),'folder':str(folder),'links':[{'price':p,'url':worker.origin+f'/i/{key}/{p}.html','orderUrl':worker.origin+f'/i/{key}/{p}/order'} for p in PRICES],'revision':1,'salesStopped':False,'legacy':True};changed=True
                except (OSError,ValueError):continue
        if changed:save(root,data)
        return data

def catalog(worker,root):
    data=register(worker,root);items=[{k:v for k,v in row.items() if k not in ('folder','edits')}|status(row) for row in data.values()]
    items.sort(key=lambda r:r['createdAt'],reverse=True)
    active=[r for r in items if r['operating']]
    return {'items':items,'summary':{'operating':len(active),'links':len(active)*2,'selling':sum(not r['salesStopped'] for r in active),'byKind':{k:sum(r['kind']==k for r in active) for k in ('wedding','event','memorial')},'checkedAt':now().isoformat()}}

def product_status(root,product):
    """Return None for unmanaged products, otherwise the authoritative shared gate."""
    key=product.get('publicationId') or product.get('_libraryKey')
    if not key:return None
    row=state(root).get(key)
    if row:return status(row)
    return None

def current_html(root,row,price):
    if row.get('external'):raise ValueError('외부 발행 초대장은 등록된 링크에서 확인하세요. ERP에서 문구·디자인을 직접 수정할 수 없습니다.')
    raw=(base(root)/row['editPath']/f'{price}.html').read_text() if row.get('editPath') else (Path(row['folder'])/f'{price}.html').read_text()
    for old,new in row.get('orderRewrites',{}).items():raw=raw.replace(old,new)
    if row.get('thumbnailVersion'):
        raw=re.sub(r'(thumbnail\.jpg)(?:\?v=[a-f0-9]+)?',r'\1?v='+row['thumbnailVersion'],raw)
    return raw
def nodes(tree):
    return tree.xpath('//h1[not(*)]|//h2[not(*)]|//h3[not(*)]|//p[not(*)]')
def detail(root,row):
    if row.get('external'):return {'id':row['id'],'revision':row['revision'],'title':row['title'],'fields':[],'design':{},'external':True,'readOnly':True,'links':row['links'],'notice':'외부 발행 링크를 관리 목록에 등록한 초대장입니다. 문구·디자인·상품 판매 상태는 원래 발행 사이트와 블로그페이에서 수정하세요.'}
    tree=html.fromstring(current_html(root,row,93500));other=nodes(html.fromstring(current_html(root,row,99000)))
    return {'id':row['id'],'revision':row['revision'],'title':row['title'],'fields':[{'key':str(i),'text':e.text or ''} for i,e in enumerate(nodes(tree)) if (e.text or '').strip() and i<len(other) and e.text==other[i].text],'design':row.get('design',{})}
def edit_documents(root,row,payload):
    fields=payload.get('fields',{});design=payload.get('design',{})
    if not isinstance(fields,dict) or len(fields)>100 or not isinstance(design,dict):raise ValueError('수정 항목 형식을 확인하세요.')
    for key,value in fields.items():
        if not re.fullmatch(r'\d{1,3}',str(key)) or not isinstance(value,str) or len(value)>1500:raise ValueError('문구 길이와 항목을 확인하세요.')
    styles=[]
    for key,selector in [('background','body'),('color','body'),('accent','h1,h2,h3')]:
        value=design.get(key,'')
        if value:
            if not re.fullmatch(r'#[a-fA-F0-9]{6}',value):raise ValueError('색상 형식을 확인하세요.')
            styles.append(selector+'{'+('background-color' if key=='background' else 'color')+':'+value+'!important}')
    line=design.get('lineHeight','')
    if line!='' and line is not None:
        if not 1.2<=float(line)<=2.2:raise ValueError('줄간격은 1.2~2.2입니다.')
        styles.append('p{line-height:'+str(float(line))+'!important}')
    common={f['key'] for f in detail(root,row)['fields']}
    if not set(fields)<=common:raise ValueError('가격별 고유 문구는 변경할 수 없습니다.')
    docs={}
    for price in PRICES:
        tree=html.fromstring(current_html(root,row,price));elements=nodes(tree)
        for key,value in fields.items():
            if int(key)>=len(elements):raise ValueError('원문이 변경됐습니다. 다시 불러오세요.')
            elements[int(key)].text=value
        for old in tree.xpath('//style[@id="erp-library-design"]'):old.getparent().remove(old)
        style=html.Element('style',id='erp-library-design');style.text=''.join(styles)
        head=tree.find('head')
        if head is not None:head.append(style)
        docs[str(price)]=html.tostring(tree,encoding='unicode',doctype='<!DOCTYPE html>')
    return docs

def update(root,key,payload,actor):
    with LOCK:
        data=state(root);row=data.get(key)
        if not row:raise ValueError('발행된 초대장을 찾지 못했습니다.')
        if row.get('external'):raise ValueError('외부 발행 초대장입니다. 문구·디자인은 원래 발행 사이트에서, 상품 판매 중지는 블로그페이에서 처리하세요.')
        if payload.get('revision')!=row['revision']:raise ValueError('다른 직원이 수정했습니다. 새로고침하세요.')
        action=payload.get('action')
        if action=='preview':return {'documents':edit_documents(root,row,payload),'revision':row['revision']}
        if action=='sales' and row.get('orderMode')=='blogpay-manual':raise ValueError('수동 연결된 블로그페이 상품입니다. 두 상품의 판매 상태는 블로그페이에서 변경하세요.')
        if action=='sales':
            if type(payload.get('stopped')) is not bool:raise ValueError('판매 상태를 확인하세요.')
            row['salesStopped']=payload['stopped']
        elif action=='edit':
            docs=edit_documents(root,row,payload);rev=row['revision']+1;rel=f'{key}/{rev}';folder=base(root)/rel;folder.mkdir(parents=True,exist_ok=False)
            for price,raw in docs.items():(folder/f'{price}.html').write_text(raw)
            row['editPath']=rel;row['design']=payload.get('design',{})
        else:raise ValueError('지원하지 않는 관리 작업입니다.')
        row['revision']+=1;row['updatedAt']=now().isoformat();row['updatedBy']=str(actor)[:160]
        row.setdefault('history',[]).append({'revision':row['revision'],'action':action,'at':row['updatedAt'],'by':row['updatedBy'],'salesStopped':row['salesStopped'],'editPath':row.get('editPath')})
        save(root,data)
        return {'id':key,'revision':row['revision'],**status(row)}

def public(root,key,name):
    row=state(root).get(key)
    if not row or row.get('external'):return None
    if not status(row)['operating']:return (410,'text/plain; charset=utf-8','운영 기간이 종료된 초대장입니다.'.encode())
    if name in ('93500.html','99000.html'):
        return (200,'text/html; charset=utf-8',current_html(root,row,int(name.split('.')[0])).encode())
    return None


def external_url(value):
    if not isinstance(value,str) or len(value)>1500:return False
    try:
        p=urlsplit(value);host=(p.hostname or '').lower()
        return bool(p.scheme=='https' and not p.username and not p.password and not p.fragment and p.port in (None,443) and re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}',host) and not host.endswith(('.local','.internal','.localhost')))
    except ValueError:return False

def register_external_pair(root,payload,actor):
    """Index an existing pair without changing either original URL or creating products."""
    kind=payload.get('kind');values=payload.get('values') or {};title=payload.get('title','')
    if not isinstance(values,dict):raise ValueError('초대장 입력값을 확인하세요.')
    date=payload.get('date') or values.get('date','')
    if kind not in ('wedding','event','memorial') or not isinstance(values,dict) or not isinstance(title,str) or not 0<len(title.strip())<=160:raise ValueError('초대장 종류·제목을 확인하세요.')
    try:
        if not isinstance(date,str) or not re.fullmatch(r'20\d\d-\d\d-\d\d',date):raise ValueError()
        dt.date.fromisoformat(date)
    except (ValueError,TypeError):raise ValueError('행사 날짜를 YYYY-MM-DD로 입력하세요.') from None
    items=payload.get('items') or payload.get('links')
    if not isinstance(items,list) or len(items)!=2 or any(not isinstance(x,dict) for x in items):raise ValueError('두 가격의 초대장·상품 링크를 함께 등록하세요.')
    if {x.get('price') for x in items}!=set(PRICES):raise ValueError('93,500원과 99,000원 초대장을 각각 등록하세요.')
    links=[]
    for item in sorted(items,key=lambda x:x['price']):
        url=item.get('url','');order=item.get('orderUrl','')
        if not external_url(url) or not erp_manual_blogpay.order_url(order,os.getenv('BLOGPAY_SHOP_ID') or None):raise ValueError('공개 HTTPS 초대장 링크와 현재 상점의 블로그페이 상품 링크를 확인하세요.')
        links.append({'price':item['price'],'url':url,'orderUrl':order,'source':'manual-blogpay','verification':'provided-links'})
    if len({x['url'] for x in links})!=2 or len({x['orderUrl'] for x in links})!=2:raise ValueError('두 가격은 서로 다른 초대장·상품 링크를 사용해야 합니다.')
    if len({urlsplit(x['url']).hostname for x in links})!=1:raise ValueError('두 초대장은 같은 발행 사이트의 링크를 사용하세요.')
    key=hashlib.sha256(json.dumps(links,sort_keys=True).encode()).hexdigest()[:32]
    with LOCK:
        data=state(root)
        if key in data:
            if not data[key].get('external'):raise ValueError('기존 발행 정보와 충돌합니다.')
            return {'id':key,'revision':data[key]['revision'],'replayed':True,**status(data[key])}
        kept={k:str(values[k])[:250] for k in ('name','date','time','venue','address','groomName','brideName','groomParents','brideParents','groomRelation','brideRelation','hostSide') if isinstance(values.get(k),str)}
        created=now().isoformat();row={'id':key,'external':True,'readOnly':True,'orderMode':'blogpay-manual','kind':kind,'createdAt':created,'date':date,'title':title.strip(),'values':kept,'links':links,'revision':1,'salesStopped':False,'updatedAt':created,'updatedBy':str(actor)[:160]}
        data[key]=row;save(root,data)
    return {'id':key,'revision':1,'replayed':False,**status(row)}


def refresh_thumbnail(worker,root,key,actor):
    """Re-render only the managed thumbnail; keep invitation and checkout URLs stable."""
    with LOCK:
        data=state(root);row=data.get(key)
        if not row or row.get('external'):raise ValueError('ERP에서 발행한 초대장만 썸네일을 갱신할 수 있습니다.')
        with worker.db() as db:
            job=db.execute("SELECT input FROM jobs WHERE public_id=? AND action='publish' AND state='done' ORDER BY created_at DESC LIMIT 1",(key,)).fetchone()
        if not job:raise ValueError('발행 원본을 확인할 수 없습니다.')
        source=json.loads(job['input'])
        from erp_invitation_render import thumbnail
        raw=thumbnail(source['values'],source['kind']);version=hashlib.sha256(raw).hexdigest()[:16]
        target=Path(row['folder'])/'thumbnail.jpg'
        if target.is_file() and not target.with_name('thumbnail.previous.jpg').exists():target.with_name('thumbnail.previous.jpg').write_bytes(target.read_bytes())
        temp=target.with_name('thumbnail.refresh.tmp');temp.write_bytes(raw);temp.replace(target)
        row['thumbnailVersion']=version;row['revision']+=1;row['updatedAt']=now().isoformat();row['updatedBy']=str(actor)[:160]
        save(root,data)
        return {'id':key,'revision':row['revision'],'thumbnailVersion':version}


def dispatch(handler,method,path,root,worker):
    if path!='/erp/invitations/library':return False
    try:
        if method=='GET':handler.send_json(catalog(worker,root));return True
        if method!='POST':handler.send_json({'error':'허용하지 않는 요청입니다.'},405);return True
        size=int(handler.headers.get('Content-Length','0'))
        if not 0<size<=40000:raise ValueError('요청 크기를 확인하세요.')
        payload=json.loads(handler.rfile.read(size))
        if payload.get('action')=='register-external-pair':
            handler.send_json(register_external_pair(root,payload,payload.get('actor','ERP 직원')));return True
        key=payload.get('id','');data=register(worker,root)
        if payload.get('action')=='refresh-thumbnail':
            handler.send_json(refresh_thumbnail(worker,root,key,payload.get('actor','ERP 직원')));return True
        if key not in data:raise ValueError('발행된 초대장을 찾지 못했습니다.')
        if payload.get('action')=='detail':handler.send_json(detail(root,data[key]));return True
        handler.send_json(update(root,key,payload,payload.get('actor','ERP 직원')))
    except (ValueError,KeyError,TypeError,OSError) as e:handler.send_json({'error':str(e)[:240]},409)
    return True
