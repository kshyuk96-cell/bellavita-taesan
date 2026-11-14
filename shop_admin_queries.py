"""Bounded, parameterized all-record admin search and paging."""
import datetime as dt
import math
import re
from urllib.parse import urlencode

STATES={'':'전체 결제 상태','requesting':'결제 요청 확인 필요','waiting':'결제 대기','failed':'결제 요청 실패','paid':'결제 완료','partial_cancel':'일부 취소','cancelled':'취소'}
STAGES={'':'전체 배송 상태','confirmed':'주문 확인','shipping':'배송 중','delivered':'배송 완료'}
KEYS=('q','order','state','stage','from_date','to_date','page')
PAGE_SIZE=30


def filters(fields):
    result={key:str(fields.get(key,'')).strip() for key in KEYS}
    if len(result['q'])>100:raise ValueError('검색어는 100자 이내로 입력하세요.')
    if result['order'] and not re.fullmatch('[a-f0-9]{32}',result['order']):raise ValueError('정확한 주문번호를 입력해 주세요.')
    if result['state'] not in STATES or result['stage'] not in STAGES:raise ValueError('결제·배송 필터를 확인하세요.')
    for key in ('from_date','to_date'):
        if result[key]:
            try:valid=dt.date.fromisoformat(result[key]).isoformat()==result[key]
            except ValueError:valid=False
            if not valid:raise ValueError('행사일은 올바른 날짜로 입력하세요.')
    if result['from_date'] and result['to_date'] and result['from_date']>result['to_date']:raise ValueError('행사 시작일이 종료일보다 늦습니다.')
    if result['page'] and not re.fullmatch(r'[0-9]{1,7}',result['page']):raise ValueError('페이지 번호를 확인하세요.')
    result['page']=str(max(1,int(result['page'] or '1')))
    return result


def select(c,fields):
    f=filters(fields);clauses=[];args=[]
    if f['order']:clauses.append('o.id=?');args.append(f['order'])
    if f['q']:
        # Search human fields only; JSON keys and internal flags are not matches.
        terms="o.id||' '||COALESCE(json_extract(o.details,'$.buyer'),'')||' '||COALESCE(json_extract(o.details,'$.buyer_phone'),'')||' '||COALESCE(json_extract(o.details,'$.sender'),'')||' '||COALESCE(json_extract(o.details,'$.message'),'')||' '||COALESCE(json_extract(o.details,'$.title'),'')||' '||COALESCE(json_extract(o.details,'$.venue'),'')"
        for char in ('-',' ', '(',')'):terms="replace("+terms+",'"+char+"','')"
        clauses.append('instr(lower('+terms+'),lower(?))>0');args.append(re.sub(r'[-\s()]','',f['q']))
    if f['state']:clauses.append('o.state=?');args.append(f['state'])
    if f['stage']:clauses.append('f.stage=?');args.append(f['stage'])
    for key,sign in (('from_date','>='),('to_date','<=')):
        if f[key]:clauses.append("substr(json_extract(o.details,'$.event_time'),1,10)"+sign+'?');args.append(f[key])
    sql=' FROM invitation_orders o JOIN shop_offers s ON s.id=o.job LEFT JOIN shop_fulfillment f ON f.order_id=o.id'
    if clauses:sql+=' WHERE '+' AND '.join(clauses)
    total=c.execute('SELECT count(*)'+sql,args).fetchone()[0];pages=max(1,math.ceil(total/PAGE_SIZE));page=min(int(f['page']),pages);f['page']=str(page)
    rows=c.execute("SELECT o.*"+sql+" ORDER BY (o.state='requesting') DESC,o.created DESC,o.id LIMIT ? OFFSET ?",[*args,PAGE_SIZE,(page-1)*PAGE_SIZE]).fetchall()
    return rows,{'filters':f,'total':total,'page':page,'pages':pages,'displayed':len(rows)}


def query(f,**changes):return urlencode({k:v for k,v in dict(f,**changes).items() if k in KEYS and v})


def redirect(path,fields):
    from urllib.parse import parse_qsl
    encoded=fields.get('_return_query','')
    if len(encoded)>2400:raise ValueError('화면 복귀 정보를 확인하세요.')
    pairs=parse_qsl(encoded,keep_blank_values=True,max_num_fields=10)
    if any(k not in KEYS for k,_ in pairs) or len({k for k,_ in pairs})!=len(pairs):raise ValueError('화면 복귀 정보를 확인하세요.')
    f=filters(dict(pairs))
    order=fields.get('order_id','');product=fields.get('product_id','')
    anchor='order-'+order if re.fullmatch('[a-f0-9]{32}',order) else 'product-'+product if re.fullmatch('[a-f0-9]{32}',product) else fields.get('_return_anchor','admin-orders')
    if anchor not in ('admin-orders','admin-products','admin-delivery','admin-reports','admin-settings') and not re.fullmatch(r'(order|product)-[a-f0-9]{32}',anchor):anchor='admin-orders'
    return path+'?'+query(f)+'&saved=1#'+anchor


def controls(path,meta):
    from html import escape
    f=meta['filters']
    def select(name,labels):return '<label>'+('결제 상태' if name=='state' else '배송 상태')+'<select name="'+name+'">'+''.join('<option value="'+code+'"'+(' selected' if f[name]==code else '')+'>'+label+'</option>' for code,label in labels.items())+'</select></label>'
    form='<form method="get" action="'+escape(path)+'" class="admin-search"><h3>전체 주문 검색·필터</h3><label>검색어<input name="q" maxlength="100" value="'+escape(f['q'])+'" placeholder="주문번호·주문자·휴대폰·문구·상품·행사장"></label><div class="search-grid">'+select('state',STATES)+select('stage',STAGES)
    for key,label in [('from_date','행사 시작일'),('to_date','행사 종료일')]:form+='<label>'+label+'<input type="date" name="'+key+'" value="'+escape(f[key])+'"></label>'
    form+='</div><button>검색</button> <a href="'+escape(path)+'#admin-orders">조건 초기화</a></form>'
    info='<p id="shop-result-count" role="status">전체 일치 '+str(meta['total'])+'건 · '+str(meta['page'])+'/'+str(meta['pages'])+'쪽 · 이번 쪽 '+str(meta['displayed'])+'건 (최대 30건)</p>'
    paging='<nav class="order-pagination" aria-label="주문 페이지">'
    for page,label in [(meta['page']-1,'이전'),(meta['page']+1,'다음')]:
        if 1<=page<=meta['pages']:paging+='<a href="'+escape(path+'?'+query(f,page=str(page)))+'#admin-orders">'+label+'</a> '
    return form+info+paging+'</nav>'
