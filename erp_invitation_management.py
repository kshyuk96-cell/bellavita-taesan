"""Read-only ERP views over invitation products, checkout and BlogPay orders.

This module never creates goods, changes payments, writes the Sheet or sends SMS.
"""
import datetime as dt
import json
import os
import re
import sqlite3
import erp_invitation_order_edits as order_edits
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def read_db(path):
    if not Path(path).is_file():return None
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    c.row_factory=sqlite3.Row
    return c


def notifications(root):
    import order_sms
    active = order_sms.enabled()
    provider = order_sms.configured()
    readiness = 'ready' if active and provider else 'provider-missing' if active else 'disabled'
    note = ('새 결제 완료 주문에 고객·직원 알림을 발송합니다. 접수 완료는 단말 수신 확인과 다릅니다.'
            if readiness == 'ready' else '문자 서비스의 사업자 인증·발신번호 인증·API 연결·잔액 확인이 필요합니다.'
            if active else '주문 문자 알림이 꺼져 있습니다.')
    audience = lambda: {'state':readiness,'pending':0,'accepted':0,'uncertain':0,'expired':0,'delivered':None}
    result={'customer':audience(),'staff':{**audience(),'recipients':len(order_sms.staff_phones())},
            'providerConfigured':provider,'provider':order_sms.provider_name(),'queueEnabled':active,
            'payappConfigured':all(os.getenv('INVITATION_PAYAPP_'+k) for k in ('USER_ID','LINK_KEY','LINK_VALUE','REQUEST_KEY')),
            'checkoutEnabled':os.getenv('INVITATION_CHECKOUT_ENABLED')=='1',
            'localCheckoutEnabled':os.getenv('INVITATION_LOCAL_CHECKOUT_ENABLED')=='1',
            'checkoutPilotRestricted':bool(os.getenv('INVITATION_CHECKOUT_PILOT_JOBS')),
            'blogpay':'자체 주문서·페이앱 결제 기준입니다. 결제 완료 후 직원 확인을 거쳐 ERP 주문에 등록합니다.'}
    result['customer']['message']=note
    result['localTemplateReady']=all((Path(root)/'invitations'/'7f494a310ca9371914b8863b45732bd5'/f'{price}.order.json').is_file() for price in (93500,99000))
    c=read_db(Path(root)/'state.sqlite3')
    if c:
        try:
            result['staff']['recipients']=len(order_sms.staff_phones(c))
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='invitation_notice_outbox'").fetchone()
            if exists:
                for recipient,state,n in c.execute('SELECT audience,state,COUNT(*) FROM invitation_notice_outbox GROUP BY audience,state'):
                    if recipient in ('customer','staff') and state in ('pending','accepted','uncertain','expired'):
                        result[recipient][state]+=n
            # Retain prior staff-provider acceptance counts without labelling them delivered.
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_sms'").fetchone()
            if exists:
                for state,n in c.execute('SELECT state,COUNT(*) FROM order_sms GROUP BY state'):
                    result['staff']['accepted' if state=='접수 완료' else 'uncertain']+=n
        except sqlite3.OperationalError:
            result['staff']['state']='unavailable'
        finally:c.close()
    return result


def products(worker,root=None):
    items={}
    with worker.db() as c:
        rows=c.execute("SELECT input,result,created_at,invitation_id FROM jobs WHERE action='publish' AND state='done' ORDER BY created_at DESC LIMIT 2000").fetchall()
    for row in rows:
        try:
            data=json.loads(row['input']);result=json.loads(row['result'] or '{}');v=data.get('values',{})
            for item in result.get('items',[]):
                key=str(item.get('productId',''))
                if not re.fullmatch(r'\d+',key) or key in items:continue
                items[key]={'productId':key,'source':'own-checkout' if re.search(r'/i/[a-f0-9]{32}/(?:93500|99000)/order$',item.get('orderUrl','')) else 'blogpay','price':item.get('price'),'orderUrl':item.get('orderUrl',''),'url':item.get('url',''),'verifiedAt':row['created_at'],'invitationId':row['invitation_id'],'name':v.get('name',''),'date':v.get('date',''),'time':v.get('time',''),'venue':v.get('venue',''),'address':v.get('address',''),'kind':data.get('kind',''),'hostSide':v.get('hostSide',''),'groomName':v.get('groomName',''),'brideName':v.get('brideName',''),'groomParents':v.get('groomParents',''),'brideParents':v.get('brideParents',''),'sourceUrl':v.get('sourceUrl',''),'verification':'saved-result','orderMode':data.get('orderMode','')}
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
    if root is not None:
        import erp_invitation_library as library
        catalog=library.catalog(worker,root)['items']
        all_rows=library.state(root)
        deleted=[row for row in all_rows.values() if row.get('deleted')]
        deleted_urls={link.get('orderUrl') for row in deleted for link in row.get('links',[])}
        deleted_products={str(link.get('productId')) for row in deleted for link in row.get('links',[]) if link.get('productId')}
        items={key:item for key,item in items.items() if item.get('orderUrl') not in deleted_urls and str(item.get('productId')) not in deleted_products}
        # Existing externally published pairs have no engine job or local product
        # snapshot. Index both supplied prices explicitly, preserving their links.
        for row in catalog:
            if not row.get('external'):continue
            v=row.get('values') or {}
            for link in row.get('links',[]):
                price=link.get('price')
                if price not in (93500,99000):continue
                key='external:'+row['id']+':'+str(price)
                items[key]={'productId':key,'source':'blogpay','external':True,'readOnly':True,'orderMode':'blogpay-manual','price':price,'orderUrl':link.get('orderUrl',''),'url':link.get('url',''),'verifiedAt':row.get('createdAt',''),'invitationId':row.get('invitationId') or row['id'],'name':row['title'],'date':row.get('date',''),'time':v.get('time',''),'venue':v.get('venue',''),'address':v.get('address',''),'kind':row['kind'],'hostSide':v.get('hostSide',''),'groomName':v.get('groomName',''),'brideName':v.get('brideName',''),'groomParents':v.get('groomParents',''),'brideParents':v.get('brideParents',''),'verification':'provided-links'}
        for item in items.values():
            match=next((r for r in catalog if any(x.get('orderUrl')==item.get('orderUrl') for x in r['links'])),None)
            if match:
                item.update({k:match[k] for k in ('operating','operatingUntil','salesStopped','saleStatus')})
                item.update(libraryId=match['id'],libraryRevision=match['revision'],readOnly=match.get('readOnly',False),orderOnly=match.get('orderOnly',False),legacyOrderOnly=match.get('legacyOrderOnly',False))
                if not item.get('invitationId'):item['invitationId']=match.get('invitationId') or match['id']
                if item.get('verification')=='own-snapshot':item['date']=match.get('date') or item.get('date','')
                linked=next((link for link in match['links'] if link.get('orderUrl')==item.get('orderUrl')),None)
                if linked and (not item.get('url') or item.get('verification')=='own-snapshot'):item['url']=linked.get('url') or item['orderUrl']
            else:item['saleStatus']='미발행'
        # Native product text may be edited independently of a read-only legacy
        # invitation. Apply only the display/fulfillment overlay, never payment data.
        import checkout
        for item in items.values():
            item['canEditProduct']=False;item['productEditRevision']=0
            native=re.fullmatch(r'/i/([a-f0-9]{32})/(93500|99000)/order',urlsplit(item.get('orderUrl','')).path)
            if item.get('source')!='own-checkout' or not native:continue
            try:
                product=checkout.read(Path(root),native[1],native[2])
                if product and type(product.get('price')) is int and product['price']>0:
                    item.update(name=product.get('title',''),venue=product.get('venue',''),address=product.get('address',''),orderNotice=product.get('orderNotice',''),price=product['price'],canEditProduct=True,productEditRevision=product.get('_productEditRevision',0))
            except (OSError,ValueError,TypeError,sqlite3.Error):continue
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
    memorial=bool(re.search('부고|근조',d.get('title','')))
    if memorial:date=time=''
    return {'id':'payapp:'+row['id'],'legacyOrderNumber':row['legacy_order_number'] if 'legacy_order_number' in row.keys() else None,'source':'payapp','sourceId':row['id'],'createdAt':row['created'],'state':row['state'],'stateLabel':{'paid':'결제완료','bank_pending':'무통장 · 입금 대기','cancelled':'취소','partial_cancel':'일부취소'}.get(row['state'],row['state']),'paymentMethod':d.get('payment_method','payapp'),'amount':d.get('amount'),'quantity':1,'productId':str(d.get('product','')),'title':d.get('title',''),'buyer':d.get('buyer',''),'buyerPhone':d.get('buyer_phone',''),'options':options,'fields':{'date':date,'time':time,'address':' / '.join(dict.fromkeys(x for x in [d.get('address'),d.get('venue')] if x)),'recipient':label_value(options,['받으시는 분','받는 분']) or d.get('recipient',''),'sender':d.get('sender',''),'size':label_value(options,['상품','규격','사이즈']) or '2호 선물형','artwork':'','message':d.get('message',''),'eventType':'근조' if re.search('부고|근조',d.get('title','')) else '결혼' if d.get('wedding_preview') or re.search('예식|결혼',d.get('title','')) else '일반 행사','photoTo':d.get('buyer_phone',''),'note':('발인(배송시각 아님): '+str(d.get('event_time',''))) if memorial else ''}}


def confirm_deposit(root,payload):
    source=order_edits.source_key(payload.get('sourceId',''))
    if not re.fullmatch(r'[a-f0-9]{32}',source):raise ValueError('입금 확인할 주문을 확인하세요.')
    actor=str(payload.get('actor') or 'ERP 직원').strip()[:80]
    file=Path(root)/'state.sqlite3'
    with sqlite3.connect(file) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM invitation_orders WHERE id=?',(source,)).fetchone()
        if not row:raise ValueError('주문을 찾지 못했습니다.')
        details=json.loads(row['details'])
        if details.get('payment_method')!='bank_transfer':raise ValueError('무통장 주문만 입금 확인할 수 있습니다.')
        if row['state']=='bank_pending':
            db.execute("UPDATE invitation_orders SET state='paid' WHERE id=? AND state='bank_pending'",(source,))
        elif row['state']!='paid':raise ValueError('입금 확인할 수 없는 주문 상태입니다.')
        operations=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_operations'").fetchone()
        current=db.execute(('SELECT i.*,o.order_number AS legacy_order_number FROM invitation_orders i LEFT JOIN order_operations o ON replace(o.id,"-","")=i.id WHERE i.id=?' if operations else 'SELECT i.* FROM invitation_orders i WHERE i.id=?'),(source,)).fetchone()
    result=paid_order(current);result['depositConfirmedBy']=actor
    return result


def query(worker,root,params):
    today=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date();start=params.get('from',[(today-dt.timedelta(days=30)).isoformat()])[0];end=params.get('to',[today.isoformat()])[0]
    try:
        a,b=dt.date.fromisoformat(start),dt.date.fromisoformat(end);page=int(params.get('page',['1'])[0])
        if a>b or (b-a).days>366 or not 1<=page<=100:raise ValueError()
    except ValueError:raise ValueError('조회 기간은 1년 이내, 페이지는 1~100으로 입력하세요.')
    edits=order_edits.metadata(root);hidden=[order_edits.source_key(key) for key,row in edits.items() if row['hidden']]
    include_hidden=params.get('includeHidden',['0'])[0]=='1'
    excluded=json.dumps([] if include_hidden else hidden)
    catalog=[p for p in products(worker,root) if p.get('source')=='own-checkout' or p.get('external') or p.get('orderMode')=='blogpay-manual'];orders=[];warnings=[];pages=1;count=0
    c=read_db(Path(root)/'state.sqlite3')
    if c:
        try:
            target=params.get('id',[''])[0]
            count=c.execute('SELECT COUNT(*) FROM invitation_orders WHERE substr(created,1,10)>=? AND substr(created,1,10)<=? AND id NOT IN (SELECT value FROM json_each(?))',(start,end,excluded)).fetchone()[0]
            pages=max(1,(count+499)//500)
            rows=c.execute("SELECT i.*,o.order_number AS legacy_order_number FROM invitation_orders i LEFT JOIN order_operations o ON replace(o.id,'-','')=i.id WHERE substr(i.created,1,10)>=? AND substr(i.created,1,10)<=? AND i.id NOT IN (SELECT value FROM json_each(?)) AND (?='' OR 'payapp:'||i.id=?) ORDER BY i.created DESC LIMIT 500 OFFSET ?",(start,end,excluded,target,target,0 if target else (page-1)*500)).fetchall()
            for row in rows:
                d=order_edits.apply(paid_order(row),edits)
                if not params.get('id') or d['id']==params['id'][0]:orders.append(d)
        except sqlite3.OperationalError:warnings.append('자체 주문 원장이 아직 없습니다.')
        finally:c.close()
    if pages>page:warnings.append('다음 페이지 주문이 있습니다. 다음 페이지도 확인하세요.')
    return {'orders':sorted(orders,key=lambda r:r['createdAt'],reverse=True),'products':catalog,'notifications':notifications(root),'from':start,'to':end,'page':page,'pages':pages,'warnings':warnings,'autoCreateEnabled':True,'productMode':'own-checkout','total':count if c else 0,'hiddenCount':len(hidden)}


def dispatch(handler,method,path,root,worker):
    if path in ('/erp/invitations/product-detail','/erp/invitations/product-update'):
        expected='GET' if path.endswith('product-detail') else 'POST'
        if method!=expected:handler.send_json({'error':'상품 조회는 GET, 수정은 POST 요청으로 처리합니다.'},405);return True
        try:
            import erp_invitation_product_edits as product_edits
            if method=='GET':
                params=parse_qs(urlsplit(getattr(handler,'path',path)).query)
                result=product_edits.detail(root,params.get('productId',[''])[0])
            else:
                size=int(handler.headers.get('Content-Length','0'))
                if not 0<size<=20000:raise ValueError('상품 수정 요청 크기를 확인하세요.')
                payload=json.loads(handler.rfile.read(size));result=product_edits.update(root,payload,payload.get('actor','ERP 직원'))
            handler.send_json(result)
        except (ValueError,TypeError,KeyError,sqlite3.Error) as error:handler.send_json({'error':str(error)[:240]},409)
        return True
    if path=='/erp/invitations/pending-orders':
        if method!='GET':handler.send_json({'error':'승인 대기 목록은 조회만 허용됩니다.'},405);return True
        try:
            import erp_invitation_approvals
            params=parse_qs(urlsplit(getattr(handler,'path',path)).query)
            handler.send_json(erp_invitation_approvals.query(root,params))
        except (ValueError,TypeError,sqlite3.Error):handler.send_json({'error':'초대장 승인 대기 목록을 확인하지 못했습니다. 다시 조회하세요.'},502)
        return True
    if path=='/erp/invitations/order-update':
        if method!='POST':handler.send_json({'error':'주문 수정은 POST 요청으로 처리합니다.'},405);return True
        try:
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=20000:raise ValueError('주문 수정 요청 크기를 확인하세요.')
            payload=json.loads(handler.rfile.read(size));handler.send_json(order_edits.update(root,payload,payload.get('actor','ERP 직원')))
        except (ValueError,TypeError,KeyError,sqlite3.Error) as error:handler.send_json({'error':str(error)[:240]},409)
        return True
    if path=='/erp/invitations/deposit-confirm':
        if method!='POST':handler.send_json({'error':'입금 확인은 POST 요청으로 처리합니다.'},405);return True
        try:
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=20000:raise ValueError('입금 확인 요청 크기를 확인하세요.')
            payload=json.loads(handler.rfile.read(size));handler.send_json(confirm_deposit(root,payload))
        except (ValueError,TypeError,KeyError,sqlite3.Error) as error:handler.send_json({'error':str(error)[:240]},409)
        return True
    if path!='/erp/invitations/management':return False
    if method!='GET':handler.send_json({'error':'조회만 허용됩니다.'},405);return True
    try:
        params=parse_qs(urlsplit(getattr(handler,'path',path)).query)
        handler.send_json(query(worker,root,params))
    except (ValueError,RuntimeError,sqlite3.Error):handler.send_json({'error':'초대장 주문 조회에 실패했습니다. 연결·기간·블로그페이 권한을 확인하세요.'},502)
    return True
