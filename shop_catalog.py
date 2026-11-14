"""Four-product storefront; SQLite revisions and immutable checkout snapshots."""
import datetime as dt
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import secrets

import checkout

ASSETS=Path(__file__).with_name('assets')/'shop'
IDS=('1454214','1440817','1452802','1969605')
PRODUCT_PATH=re.compile(r'/shop/products/([a-f0-9]{32})')


def source_products():
    data=json.loads((ASSETS/'catalog.json').read_text())
    if tuple(p['id'] for p in data['products'])!=IDS:raise ValueError('대표 상품 4개 목록을 확인하세요.')
    return data['products']


def seed(c):
    """Explicit, insert-only seed. Re-deploys never overwrite admin edits."""
    inserted=[]
    for source in source_products():
        identity=hashlib.sha256(('bellavita-product:'+source['id']).encode()).hexdigest()[:32]
        groups=[]
        for group in source['options']:
            if group['type']!='select':continue
            label='축하 문구' if group['title']=='보내는 문구' else group['title']
            values={choice['id']:{'label':choice['label'],'amount':choice['extra'],
                    'custom':bool(re.search(r'직접|개인 문구|배송 요청',choice['label']))}
                    for choice in group['choices'] if not choice['disabled']}
            groups.append({'label':label,'choices':values})
        # The generic storefront captures event/date/address itself and sender once.
        extras=[{'label':'모바일청첩장 주소 또는 없음','max':200}] if source['id'] in ('1454214','1440817') else []
        product={'title':source['displayTitle'],'price':source['price'],'images':source['images'],
            'source_id':source['id'],'source':source['sourceUrl'],'category':source['category'],
            'description':source['subtext'],'detail_text':source['detailText'],'detail_images':source['detailImages'],
            'groups':groups,'extra_fields':extras,'public':True,'available':not source['soldOut'],
            'cash_tax_type':'','thumbnail':source['thumbnail'],'proportion_evidence':source['proportionEvidence'],
            'force_message':source['id']=='1452802','options':'','greetings':''}
        if c.execute('INSERT OR IGNORE INTO shop_products VALUES (?,?,1)',(identity,json.dumps(product,ensure_ascii=False))).rowcount:inserted.append(identity)
    return inserted


def thumbnail(product):
    thumb=product.get('thumbnail')
    if not thumb:return '<img class="photo" src="'+escape(product['images'][0])+'" alt="'+escape(product['title'])+'">'
    parts=['<div class="catalog-thumb ratio-thumb" role="img" aria-label="'+escape(product['title'])+' 디자인 시안">']
    parts += ['<span class="easel-leg '+name+'"></span>' for name in ('left','center','right')]
    size=thumb['canvas']
    for i,(x,y,w,h) in enumerate(thumb['regions']):
        style=f'width:{size/w*100}%;height:{size/h*100}%;left:{-x/w*100}%;top:{-y/h*100}%;'
        parts.append('<span class="ratio-piece '+('art-frame' if i==0 else 'message-scroll')+'"><img src="/shop-art/thumbnails-v3.png" alt="" aria-hidden="true" style="'+style+'"></span>')
    return ''.join(parts)+'</div>'


def page(c):
    cards=[]
    for row in c.execute('SELECT * FROM shop_products ORDER BY rowid'):
        p=json.loads(row['details'])
        if not p.get('public'):continue
        state='판매 중' if p.get('available',True) else '판매 중지'
        cards.append('<a class="card" href="/shop/products/'+row['id']+'">'+thumbnail(p)+'<div class="card-content"><h2>'+escape(p['title'])+'</h2><div class="price">'+f'{p["price"]:,}원'+'</div><small>'+state+'</small></div></a>')
    banner='<div class="ribbon">독립 검증 환경 · 실제 결제·알림·배송·현금영수증 발급 없음 · 가상 정보로 주문하세요.</div>' if os.getenv('SHOP_VERIFICATION_MODE')=='1' else ''
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>벨라비타 액자화환</title><link rel="stylesheet" href="/shop-art/store.css"><header><a class="brand" href="/shop">액자화환 갤러리화환</a><a href="/shop">전체 상품</a></header>'''+banner+'''<main><div class="store-heading"><h1>전체상품</h1><p class="muted">웨딩 사진 · 결혼 축하 · 행사 축하 · 근조 액자화환</p></div><div class="grid">'''+(''.join(cards) or '<p>전시 상품을 준비 중입니다.</p>')+'''</div><details class="muted"><summary>디자인 시안의 규격 근거</summary><p>액자: 상품 옵션 600×400mm / 700×467mm, 약 3:2. 족자: 공식 구성사진 기준 약 2:3 추정치이며 실측 규격은 미확인입니다. 제작·출력용 도면이 아닙니다.</p></details></main></html>'''


def read(c,identity):
    row=c.execute('SELECT * FROM shop_products WHERE id=?',(identity,)).fetchone()
    if not row:return None,None
    p=json.loads(row['details'])
    if not p.get('public'):return None,None
    return row,p


def snapshot(identity,p,fields=None):
    fields=fields or {}
    product=dict(p,number=identity,customer_event_input=True,force_message=p.get('force_message',False),
                 venue='',address='',date='',clock='',event_time='',product_title=p['title'])
    if fields:
        def val(key,maxlen):
            value=fields.get(key,'').strip()
            if not value or len(value)>maxlen:raise ValueError('설치 장소·주소·행사 일시를 확인해 주세요.')
            return value
        raw=val('event_time',16)
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}',raw):raise ValueError('행사 일시를 확인해 주세요.')
        try:when=dt.datetime.fromisoformat(raw).replace(tzinfo=checkout.SEOUL)
        except ValueError:raise ValueError('행사 일시를 확인해 주세요.') from None
        if when<=dt.datetime.now(checkout.SEOUL):raise ValueError('앞으로 열릴 행사 일시를 입력해 주세요.')
        product.update(venue=val('venue',120),address=val('address',200),date=when.date().isoformat(),clock=when.strftime('%H:%M'),event_time=when.isoformat(),event_name=val('venue',120))
    return product


def prepare(c,identity,fields):
    attempt=fields.get('attempt','')
    if not re.fullmatch(r'[a-f0-9]{32}',attempt):raise ValueError('주문서를 다시 열어 주세요.')
    # Retry uses the stored offer, even after product edits, never the new price.
    offer_id=hashlib.sha256(('catalog-order:'+identity+':'+attempt).encode()).hexdigest()[:32]
    existing=c.execute('SELECT details FROM shop_offers WHERE id=? AND product_id=?',(offer_id,identity)).fetchone()
    if existing:
        product=json.loads(existing['details']);expected=snapshot(identity,product,fields)
        if any(expected[k]!=product[k] for k in ('venue','address','event_time')):raise ValueError('같은 접수번호의 설치 정보가 변경됐습니다. 주문 내역을 먼저 확인하세요.')
        return offer_id,product
    row,p=read(c,identity)
    if not p or not p.get('available',True):raise ValueError('현재 판매하지 않는 상품입니다.')
    if fields.get('product_revision')!=str(row['revision']):raise ValueError('상품이 변경됐습니다. 최신 주문서를 다시 열어 주세요.')
    product=snapshot(identity,p,fields)
    product['production_review_required']=True
    # Validate every option and required input before storing an offer.
    checkout.normalize_order(product,fields)
    c.execute('INSERT INTO shop_offers VALUES (?,?,?,?,?,1)',(offer_id,identity,row['revision'],p['price'],json.dumps(product,ensure_ascii=False)))
    return offer_id,product
