"""Read-only ERP views over invitation products, checkout and BlogPay orders.

This module never creates goods, changes payments, writes the Sheet or sends SMS.
"""
import datetime as dt
import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def read_db(path):
    if not Path(path).is_file():return None
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    c.row_factory=sqlite3.Row
    return c


def notifications(root):
    result={'customer':{'state':'not-configured','message':'자체 주문 결제 요청은 smsuse=n이며 별도 고객 주문알림 발송기가 없습니다.'},'staff':{'state':'disabled','recipients':0,'accepted':0,'uncertain':0,'delivered':None},'providerConfigured':all(os.getenv(k) for k in ('SOLAPI_API_KEY','SOLAPI_API_SECRET','SOLAPI_FROM')),'queueEnabled':False,'payappConfigured':all(os.getenv('INVITATION_PAYAPP_'+k) for k in ('USER_ID','LINK_KEY','LINK_VALUE','REQUEST_KEY')),'checkoutEnabled':os.getenv('INVITATION_CHECKOUT_ENABLED')=='1','localCheckoutEnabled':os.getenv('INVITATION_LOCAL_CHECKOUT_ENABLED')=='1','checkoutPilotRestricted':bool(os.getenv('INVITATION_CHECKOUT_PILOT_JOBS')),'blogpay':'자체 주문서·페이앱 결제 기준입니다. 별도 고객 알림 발송 연결이 필요합니다.'}
    result['localTemplateReady']=all((Path(root)/'invitations'/'7f494a310ca9371914b8863b45732bd5'/f'{price}.order.json').is_file() for price in (93500,99000))
    c=read_db(Path(root)/'state.sqlite3')
    if c:
        try:
            result['staff']['recipients']=c.execute("SELECT COUNT(*) FROM order_staff_notify n JOIN staff s ON s.owner=n.owner WHERE n.center='구로'").fetchone()[0]
            for state,n in c.execute('SELECT state,COUNT(*) FROM order_sms GROUP BY state'):
                if state=='접수 완료':result['staff']['accepted']+=n
                else:result['staff']['uncertain']+=n
        except sqlite3.OperationalError:result['staff']['state']='unavailable'
        finally:c.close()
    return result


def products(worker,root=None):
    items={}
    with worker.db() as c:
        rows=c.execute("SELECT input,result,created_at,invitation_id FROM jobs WHERE action IN ('preview','publish') AND state='done' ORDER BY created_at DESC LIMIT 2000").fetchall()
    for row in rows:
        try:
            data=json.loads(row['input']);result=json.loads(row['result'] or '{}');v=data.get('values',{})
            for item in result.get('items',[]):
                key=str(item.get('productId',''))
                if not re.fullmatch(r'\d+',key) or key in items:continue
                items[key]={'productId':key,'source':'own-checkout' if re.search(r'/i/[a-f0-9]{32}/(?:93500|99000)/order$',item.get('orderUrl','')) else 'blogpay','price':item.get('price'),'orderUrl':item.get('orderUrl',''),'url':item.get('url',''),'verifiedAt':row['created_at'],'invitationId':row['invitation_id'],'name':v.get('name',''),'date':v.get('date',''),'time':v.get('time',''),'venue':v.get('venue',''),'address':v.get('address',''),'kind':data.get('kind',''),'hostSide':v.get('hostSide',''),'groomName':v.get('groomName',''),'brideName':v.get('brideName',''),'groomParents':v.get('groomParents',''),'brideParents':v.get('brideParents',''),'sourceUrl':v.get('sourceUrl',''),'verification':'saved-result'}
        except (ValueError,TypeError):continue
    if root is not None:
        base=Path(root)/'invitations'
        if base.is_dir():
            for folder in sorted(base.iterdir(),key=lambda p:p.stat().st_mtime,reverse=True)[:1000]:
                if not re.fullmatch(r'[a-f0-9]{32}',folder.name) or not folder.is_dir():continue
                for price in (93500,99000):
                    file=folder/f'{price}.order.json'
                    if not file.is_file():continue
                    try:
                        v=json.loads(file.read_text());url=worker.origin+f'/i/{folder.name}/{price}/order'
                        if any(x.get('orderUrl')==url for x in items.values()):continue
                        key='own:'+folder.name+':'+str(price);items[key]={'productId':key,'source':'own-checkout','price':price,'orderUrl':url,'url':worker.origin+f'/i/{folder.name}/{price}.html','name':v.get('title',''),'date':str(v.get('event_time',''))[:10],'time':str(v.get('event_time',''))[11:16],'verification':'own-snapshot','invitationId':'','verifiedAt':''}
                    except (ValueError,OSError):continue
    return list(items.values())


def label_value(options,words):
    for label,value in options:
        if any(w in str(label) for w in words):return str(value or '')[:1000]
    return ''


def paid_order(row):
    d=json.loads(row['details']);when=str(d.get('event_time') or '')
    try:
        t=dt.datetime.fromisoformat(when)
        if t.tzinfo:t=t.astimezone(dt.timezone(dt.timedelta(hours=9)))
        date,time=t.date().isoformat(),t.strftime('%H:%M')
    except ValueError:date=time=''
    options=[x for x in d.get('options',[]) if isinstance(x,list) and len(x)==2]
    return {'id':'payapp:'+row['id'],'legacyOrderNumber':row['legacy_order_number'] if 'legacy_order_number' in row.keys() else None,'source':'payapp','sourceId':row['id'],'createdAt':row['created'],'state':row['state'],'stateLabel':{'paid':'결제완료','cancelled':'취소','partial_cancel':'일부취소'}.get(row['state'],row['state']),'amount':d.get('amount'),'quantity':1,'productId':str(d.get('product','')),'title':d.get('title',''),'buyer':d.get('buyer',''),'buyerPhone':d.get('buyer_phone',''),'options':options,'fields':{'date':date,'time':time,'address':' / '.join(dict.fromkeys(x for x in [d.get('address'),d.get('venue')] if x)),'recipient':label_value(options,['받으시는 분','받는 분']) or d.get('recipient',''),'sender':d.get('sender',''),'size':label_value(options,['상품','규격','사이즈']) or '2호 선물형','artwork':'','message':d.get('message',''),'eventType':'결혼' if '예식' in d.get('title','') else '일반 행사','photoTo':d.get('buyer_phone','')}}


def query(worker,root,params):
    today=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date();start=params.get('from',[(today-dt.timedelta(days=30)).isoformat()])[0];end=params.get('to',[today.isoformat()])[0]
    try:
        a,b=dt.date.fromisoformat(start),dt.date.fromisoformat(end);page=int(params.get('page',['1'])[0])
        if a>b or (b-a).days>366 or not 1<=page<=100:raise ValueError()
    except ValueError:raise ValueError('조회 기간은 1년 이내, 페이지는 1~100으로 입력하세요.')
    catalog=[p for p in products(worker,root) if p.get('source')=='own-checkout'];orders=[];warnings=[];pages=1
    c=read_db(Path(root)/'state.sqlite3')
    if c:
        try:
            target=params.get('id',[''])[0]
            count=c.execute('SELECT COUNT(*) FROM invitation_orders WHERE substr(created,1,10)>=? AND substr(created,1,10)<=?',(start,end)).fetchone()[0]
            pages=max(1,(count+499)//500)
            rows=c.execute("SELECT i.*,o.order_number AS legacy_order_number FROM invitation_orders i LEFT JOIN order_operations o ON replace(o.id,'-','')=i.id WHERE substr(i.created,1,10)>=? AND substr(i.created,1,10)<=? AND (?='' OR 'payapp:'||i.id=?) ORDER BY i.created DESC LIMIT 500 OFFSET ?",(start,end,target,target,0 if target else (page-1)*500)).fetchall()
            for row in rows:
                d=paid_order(row)
                if not params.get('id') or d['id']==params['id'][0]:orders.append(d)
        except sqlite3.OperationalError:warnings.append('자체 주문 원장이 아직 없습니다.')
        finally:c.close()
    if pages>page:warnings.append('다음 페이지 주문이 있습니다. 다음 페이지도 확인하세요.')
    return {'orders':sorted(orders,key=lambda r:r['createdAt'],reverse=True),'products':catalog,'notifications':notifications(root),'from':start,'to':end,'page':page,'pages':pages,'warnings':warnings,'autoCreateEnabled':True,'productMode':'own-checkout'}


def dispatch(handler,method,path,root,worker):
    if path!='/erp/invitations/management':return False
    if method!='GET':handler.send_json({'error':'조회만 허용됩니다.'},405);return True
    try:
        params=parse_qs(urlsplit(getattr(handler,'path',path)).query)
        handler.send_json(query(worker,root,params))
    except (ValueError,RuntimeError,sqlite3.Error):handler.send_json({'error':'초대장 주문 조회에 실패했습니다. 연결·기간·블로그페이 권한을 확인하세요.'},502)
    return True
