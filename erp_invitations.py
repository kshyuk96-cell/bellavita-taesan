import erp_invitation_library
"""ERP-only async invitation bridge. Add dispatch(self, ...) before existing agent routes.

Existing /jobs, Kakao and payment paths are not changed. This bridge reuses only
verified existing BlogPay products; it never copies, saves or charges a product.
"""
import base64
import calendar
import concurrent.futures
import datetime as dt
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import time
import warnings
from pathlib import Path
from urllib.parse import urlsplit
from PIL import Image, ImageOps
import erp_invitation_render as render
import erp_invitation_management
import erp_local_checkout
import erp_memorial_invitation
import erp_ai
import erp_manual_blogpay
import own_checkout_pricing

TEMPLATE_HASHES={'seoul.ttf':'7b3e16b8ccd97d903520f87f22ff970d0ab0594231807d0a0b752eb8fcb670f4','event-wreath-catalog.webp': '5a264152394b1d34f847192b3c05ff7d809a4e1e072eff580220e127070d8b25', 'event-v5.css': '852ce3f563c959ed59ee240ec99896434f62bf91e019e14d63ef3ba7927fdfb3', 'rice-wreath-10kg.webp': '6a0e396b644daeaa76ef79aeee3acaa6b9893f24a31aa129fca070b29209c7a7'}

PRICES=(93500,99000)
FIELDS={'name':160,'date':10,'time':5,'venue':180,'address':250,'sourceUrl':1000,'greeting':1200,'note':700,'host':100,'groomName':40,'brideName':40,'groomParents':100,'brideParents':100,'groomRelation':24,'brideRelation':24,'hostSide':8,'logo':1000,'hero':1000,'frame':1000,'scrollLeft':1000,'scrollRight':1000,'congrats':160}
FIELDS.update(erp_memorial_invitation.FIELDS)
FIELDS.update(erp_manual_blogpay.FIELDS)
IMAGE_FIELDS=('logo','hero','frame','scrollLeft','scrollRight')
LOCK=threading.RLock()
ENGINES={}


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def safe_https(value):
    try:
        p=urlsplit(value)
        return p.scheme=='https' and p.hostname and not p.username and not p.password and not p.fragment and p.port in (None,443) and '.' in p.hostname and not p.hostname.endswith(('.local','.internal'))
    except (ValueError,TypeError):return False


def clean_values(values):
    import invitation_charmcard
    if not isinstance(values,dict):raise ValueError('입력 항목을 확인하세요.')
    out={}
    for key,limit in FIELDS.items():
        v=values.get(key) or ''
        if not isinstance(v,str) or len(v)>limit:raise ValueError('입력 길이를 확인하세요: '+key)
        out[key]=v.strip()
    for key in ('sourceUrl',*IMAGE_FIELDS):
        value=out[key]
        if value and not (key in IMAGE_FIELDS and re.fullmatch(r'attachment:[a-f0-9]{32}',value)) and not (key=='sourceUrl' and invitation_charmcard.is_card_url(value)) and not safe_https(value):raise ValueError('HTTPS 주소를 확인하세요: '+key)
    erp_manual_blogpay.validate_pair(out)
    out['mournerAccounts']=erp_memorial_invitation.clean_mourner_accounts(values.get('mournerAccounts',[]),out.get('host') or out.get('productPrefix'))
    if out.get('portrait') and not safe_https(out['portrait']):raise ValueError('고인 사진의 공개 HTTPS 주소를 확인하세요.')
    out['hostSide']='bride' if out['hostSide']=='bride' else 'groom'
    out['wreathRestriction']=values.get('wreathRestriction') is True
    out['images']=[u for u in values.get('images',[]) if isinstance(u,str) and (safe_https(u) or re.fullmatch(r'attachment:[a-f0-9]{32}',u))][:20]
    return out


def missing(kind,v):
    keys=['name','date','time','venue','address']+(['deceasedName','productPrefix'] if kind=='memorial' else ['host'] if kind=='event' else ['groomName','brideName','groomParents','brideParents','groomRelation','brideRelation',*IMAGE_FIELDS[1:]])
    return [k for k in keys if not v.get(k)]


def decode_image(raw):
    if not 0<len(raw)<=5*1024*1024:raise ValueError('사진은 5MB 이하로 올려주세요.')
    with warnings.catch_warnings():
        warnings.simplefilter('error',Image.DecompressionBombWarning)
        image=Image.open(io.BytesIO(raw))
        if image.format not in ('JPEG','PNG','WEBP') or image.width*image.height>30_000_000:raise ValueError('지원하는 사진 형식과 크기를 확인하세요.')
        image.load();image=ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((2400,2400))
        return image


class PublicHTTPSConnection(http.client.HTTPSConnection):
    """Pin the checked public IP while retaining the hostname for TLS verification."""
    def connect(self):
        results=socket.getaddrinfo(self.host,self.port,type=socket.SOCK_STREAM)
        ips=list(dict.fromkeys(r[4][0] for r in results))
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):raise ValueError('공개 이미지 주소만 사용할 수 있습니다.')
        sock=socket.create_connection((ips[0],self.port),self.timeout)
        try:self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
        except BaseException:sock.close();raise


def download_image(url):
    if not safe_https(url):raise ValueError('공개 HTTPS 사진 주소를 확인하세요.')
    for _ in range(4):
        p=urlsplit(url)
        connection=PublicHTTPSConnection(p.hostname,443,timeout=10,context=ssl.create_default_context())
        try:
            connection.request('GET',(p.path or '/')+('?' + p.query if p.query else ''),headers={'User-Agent':'BellavitaERP/1.0','Accept':'image/*'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                from urllib.parse import urljoin
                url=urljoin(url,response.getheader('Location',''))
                if not safe_https(url):raise ValueError('사진 이동 주소를 확인하세요.')
                continue
            if response.status!=200:raise ValueError('원본 사진을 읽지 못했습니다. 파일로 첨부해 주세요.')
            return decode_image(response.read(5*1024*1024+1))
        finally:connection.close()
    raise ValueError('사진 주소가 너무 많이 이동합니다.')


class JobCancelled(Exception):
    """Cooperative stop signal; ordinary provider fallbacks must not swallow it."""


class CancellationConflict(Exception):
    def __init__(self,message,result):
        super().__init__(message);self.result=result


class Engine:
    def __init__(self,root,origin,legacy,extract,*,async_jobs=True):
        self.checkout_root=Path(os.getenv('TAESAN_DATA') or root);self.root=Path(root)/'erp-invitations';self.root.mkdir(parents=True,exist_ok=True)
        self.origin=origin.rstrip('/');self.legacy=legacy;self.extract=extract;self.async_jobs=async_jobs
        self.executor=concurrent.futures.ThreadPoolExecutor(max_workers=2,thread_name_prefix='erp-invitation')
        self.active=set()
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, invitation_id TEXT NOT NULL, version INTEGER NOT NULL, action TEXT NOT NULL, hash TEXT NOT NULL, input TEXT NOT NULL, state TEXT NOT NULL, result TEXT, public_id TEXT NOT NULL, created_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS publications(key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, path TEXT NOT NULL)')
            recover=[r['id'] for r in db.execute("SELECT id FROM jobs WHERE state IN ('queued','running')")]
        erp_invitation_library.register(self,self.checkout_root)
        for key in recover:self.schedule(key)

    def db(self):
        db=sqlite3.connect(self.root/'journal.sqlite',timeout=30);db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL');return db

    def schedule(self,key):
        with LOCK:
            if key in self.active:return
            self.active.add(key)
        if self.async_jobs:self.executor.submit(self.run,key)
        else:self.run(key)

    def check_cancelled(self,key):
        if not key:return
        with self.db() as db:row=db.execute('SELECT state FROM jobs WHERE id=?',(key,)).fetchone()
        if row and row['state']=='cancelled':raise JobCancelled()

    def _quarantine_unpublished(self,row,data):
        """Keep crash leftovers privately without indexing a cancelled native pair."""
        if data.get('action')!='publish' or data.get('orderMode')!='invitation-checkout':return
        local_key=hashlib.sha256(('local-checkout:'+data['invitationId']+':'+str(data['version'])).encode()).hexdigest()[:32]
        folder=self.checkout_root/'invitations'/local_key;owned=[]
        for price in PRICES:
            file=folder/f'{price}.order.json'
            try:product=json.loads(file.read_text(encoding='utf-8'))
            except FileNotFoundError:continue
            if isinstance(product,dict) and product.get('localCheckout') is True and product.get('publicationId')==row['public_id']:owned.append(file)
        if not owned:return
        private=self.root/'private'/row['id']/('cancelled-checkout-'+secrets.token_hex(6));private.mkdir(parents=True,exist_ok=True)
        with erp_invitation_library.LOCK:
            index=erp_invitation_library.state(self.checkout_root);legacy=index.get(local_key)
            if legacy and legacy.get('legacy') and legacy.get('orderOnly'):
                (private/'library-row.json').write_text(json.dumps(legacy,ensure_ascii=False),encoding='utf-8')
                index.pop(local_key);erp_invitation_library.save(self.checkout_root,index)
            for file in owned:file.replace(private/file.name)

    def cancel(self,key,payload=None):
        if not isinstance(key,str) or not re.fullmatch(r'[\w-]{10,100}',key):raise ValueError('중단할 작업 번호를 확인하세요.')
        if payload is None:payload={}
        if not isinstance(payload,dict):raise ValueError('제작 중단 요청을 확인하세요.')
        with LOCK:
            with self.db() as db:row=db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
            if not row:return None
            if ('invitationId' in payload and payload['invitationId']!=row['invitation_id']) or ('version' in payload and (type(payload['version']) is not int or payload['version']!=row['version'])):raise ValueError('중단할 초대장과 버전을 확인하세요.')
            if row['state']=='cancelled':return {**json.loads(row['result']),'replayed':True}
            data=json.loads(row['input'])
            # A crash may leave a completed manifest before the final job update.
            # Reconcile it instead of pretending the already-public pair stopped.
            committed=self.recover_publication(row,data)
            if not committed and data['action']=='publish':
                with self.db() as db:published=db.execute('SELECT * FROM publications WHERE key=?',(f"{row['invitation_id']}:{row['version']}",)).fetchone()
                if published:committed=json.loads(published['result'])
            if committed:
                completed=self.finish(row,data,committed,committed=True)
                raise CancellationConflict('이미 발행이 완료되었습니다. 기존 초대장과 주문 링크를 보존했습니다.',completed)
            if row['state'] in ('done','error'):raise CancellationConflict('이미 종료된 제작 작업입니다. 새 작업으로 시작해 주세요.',json.loads(row['result']))
            self._quarantine_unpublished(row,data)
            current=self.get(key) or {};at=dt.datetime.now(dt.timezone.utc).isoformat()
            result={'requestId':key,'status':'cancelled','action':row['action'],'cancelledAt':at,'cancelledBy':str(payload.get('actor') or 'ERP 직원')[:160],'replayed':False,
                    **({'progress':current['progress']} if isinstance(current.get('progress'),dict) else {})}
            with self.db() as db:db.execute("UPDATE jobs SET state='cancelled',result=? WHERE id=? AND state IN ('queued','running')",(json.dumps(result,ensure_ascii=False),key))
            return result

    def submit(self,data):
        if not isinstance(data,dict):raise ValueError('입력 형식 오류')
        key=data.get('requestId','');inv=data.get('invitationId','');version=data.get('version')
        if not re.fullmatch(r'[\w-]{10,100}',key) or not re.fullmatch(r'[\w-]{10,130}',inv) or type(version) is not int or not 0<version<100000:raise ValueError('요청 ID 또는 버전 오류')
        if data.get('action') not in ('parse','preview','publish') or data.get('kind') not in ('event','wedding','memorial') or data.get('orderMode') not in ('blogpay-direct','blogpay-manual','invitation-checkout'):raise ValueError('ERP 주문 경로를 확인하세요.')
        if not isinstance(data.get('source'),str) or not 0<len(data['source'])<=6000 or len(set(re.findall(r'https?://[^\s<>]+',data['source'])))>1:raise ValueError('원본은 6,000자 이하로 입력하세요.')
        data={**data,'values':clean_values(data.get('values',{}))};hashed=digest(data)
        with LOCK,self.db() as db:
            old=db.execute('SELECT hash FROM jobs WHERE id=?',(key,)).fetchone()
            if old and old['hash']!=hashed:raise ValueError('같은 요청 ID의 내용이 다릅니다.')
            if not old:
                pending=db.execute("SELECT id FROM jobs WHERE state IN ('queued','running') AND invitation_id=?",(inv,)).fetchone()
                if pending:raise ValueError('기존 요청 상태를 먼저 확인하세요: '+pending['id'])
                if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]>=20:raise ValueError('제작 대기 중입니다. 잠시 후 요청하세요.')
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',(key,inv,version,data['action'],hashed,json.dumps(data,ensure_ascii=False),'queued',None,secrets.token_hex(16),dt.datetime.now(render.SEOUL).isoformat()))
        if not old:self.schedule(key)
        return self.get(key)

    def progress(self,key,step,label,detail='',completedPhotos=None):
        """Persist observed work only; never replace a completed/error job result."""
        if not isinstance(key,str) or not re.fullmatch(r'[\w-]{10,100}',key) or type(step) is not int or not 1<=step<=7:return None
        with self.db() as db:
            row=db.execute('SELECT state,result,created_at FROM jobs WHERE id=?',(key,)).fetchone()
            if row and row['state']=='cancelled':raise JobCancelled()
            if not row or row['state'] not in ('queued','running'):return None
            try:payload=json.loads(row['result'] or '{}')
            except (ValueError,TypeError):payload={}
            if not isinstance(payload,dict):payload={}
            previous=payload.get('progress') if isinstance(payload.get('progress'),dict) else {}
            current={'step':step,'total':7,'label':str(label)[:100],'detail':str(detail)[:240],
                     'totalPhotos':4,'startedAt':previous.get('startedAt') or row['created_at'],
                     'updatedAt':dt.datetime.now(dt.timezone.utc).isoformat()}
            if completedPhotos is not None:
                if type(completedPhotos) is int:current['completedPhotos']=min(4,max(0,completedPhotos))
            elif type(previous.get('completedPhotos')) is int:current['completedPhotos']=previous['completedPhotos']
            payload['progress']=current
            db.execute("UPDATE jobs SET result=? WHERE id=? AND state IN ('queued','running')",(json.dumps(payload,ensure_ascii=False),key))
        return current

    def get(self,key):
        with self.db() as db:row=db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
        if not row:return None
        if row['state'] in ('done','error','cancelled'):
            result=json.loads(row['result'])
            for item in result.get('items',[]):
                if 'message' in item:
                    item['message']=own_checkout_pricing.public_copy(item['message'],item['price'],item.get('orderUrl'))
            return result
        answer={'requestId':key,'status':'processing','action':row['action']}
        try:progress=json.loads(row['result'] or '{}').get('progress')
        except (ValueError,TypeError,AttributeError):progress=None
        if isinstance(progress,dict) and type(progress.get('step')) is int and 1<=progress['step']<=7:answer['progress']=progress
        return answer

    def upload(self,raw,*,job_key=''):
        image=decode_image(raw);data=render.image_bytes(image);hashed=hashlib.sha256(data).hexdigest()
        with LOCK,self.db() as db:
            if job_key:self.check_cancelled(job_key)
            old=db.execute('SELECT id FROM uploads WHERE digest=?',(hashed,)).fetchone()
            if old:return {'id':old['id']}
            if db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0]>=2000:raise ValueError('첨부 보관 용량을 확인하세요.')
            key=secrets.token_hex(16);folder=self.root/'uploads';folder.mkdir(exist_ok=True)
            path=folder/(key+'.webp');path.write_bytes(data)
            db.execute('INSERT INTO uploads VALUES(?,?,?)',(key,hashed,str(path)))
            return {'id':key}

    def image(self,value):
        if value.startswith('attachment:'):
            key=value.removeprefix('attachment:')
            with self.db() as db:row=db.execute('SELECT path FROM uploads WHERE id=?',(key,)).fetchone()
            if not row:raise ValueError('첨부 사진이 없습니다. 다시 올려주세요.')
            return decode_image(Path(row['path']).read_bytes())
        return download_image(value)

    def parse(self,data):
        self.progress(data.get('requestId',''),1,'원본 청첩장 읽기','공개 링크의 행사 정보와 사진 목록을 읽고 있습니다.')
        if data['kind']=='memorial':
            import sys
            manual=data.get('values') or {};target=manual.get('host') or manual.get('productPrefix') or ''
            v,warnings_out=erp_memorial_invitation.extract(data['source'],lambda url:erp_memorial_invitation.fetch_source(url,sys.modules[__name__]),target_mourner=target,account_fetcher=lambda url,fields:erp_memorial_invitation.fetch_source(url,sys.modules[__name__],method='POST',data=fields))
            for key in ('host','productPrefix'):
                if isinstance(manual.get(key),str) and manual[key].strip():v[key]=manual[key].strip()
            if not manual.get('host') and manual.get('productPrefix'):v['host']=erp_memorial_invitation.normalize_mourner_name(target)
            self.progress(data.get('requestId',''),2,'행사 정보 추출','원본에서 확인한 항목을 정리하고 있습니다.')
            values=clean_values(v);return {'status':'done','values':values,'missing':missing('memorial',values),'warnings':warnings_out}
        # Reuse all five provider parsers. On incomplete/unsupported sources, return
        # only available fields; never drop the original URL or invent relationships.
        source=data['source'];warnings_out=[]
        try:
            event=self.legacy.parse_source(source,self.extract)
            import invitation_charmcard
            if invitation_charmcard.is_card_url(event.get('source','')):
                attachments=[]
                self.progress(data.get('requestId',''),1,'참카드 원본 사진 수집','HTTP 원본 사진을 제작용 첨부로 저장하고 있습니다.',completedPhotos=0)
                for url in event.get('photos',[])[:20]:
                    if not invitation_charmcard.is_image_url(url):continue
                    try:
                        self.check_cancelled(data.get('requestId',''))
                        attachments.append('attachment:'+self.upload(invitation_charmcard.fetch(url,image=True),job_key=data.get('requestId',''))['id'])
                    except (ValueError,OSError):continue
                event={**event,'photos':attachments,'image':next(iter(attachments),'')}
                warnings_out.extend(event.get('warnings') or [])
                if not attachments:warnings_out.append('참카드 원본 사진을 저장하지 못했습니다. 사진을 직접 첨부해 주세요.')
            self.progress(data.get('requestId',''),2,'행사 정보 추출','원본에서 확인한 예식일·장소·혼주 정보를 정리하고 있습니다.')
            when=event.get('when');families=event.get('families') or []
            values={'name':event.get('name',''),'date':when.date().isoformat() if when else '', 'time':when.strftime('%H:%M') if when else '', 'venue':event.get('venue',''),'address':event.get('address',''),'sourceUrl':event.get('source',''),'greeting':event.get('greeting',''),'note':event.get('note',''),'host':event.get('signature','').removesuffix(' 올림'),'hostSide':event.get('host_side',''),'wreathRestriction':event.get('wreath_restriction') is True,'images':event.get('photos') or [],'hero':event.get('image','')}
            for side,family in zip(('groom','bride'),families):
                values.update({side+'Parents':family[0],side+'Relation':family[1],side+'Name':family[2]})
        except (ValueError,OSError):
            self.progress(data.get('requestId',''),2,'행사 정보 추출','확인된 원문에서 누락 항목을 보완하고 있습니다.')
            urls=list(dict.fromkeys(re.findall(r'https://[^\s<>"\]]+',source)))
            url=urls[0].rstrip(').,') if len(urls)==1 else ''
            text=source.replace(url,'') if url else source
            fields=self.extract(text) if text.strip() else {}
            if not isinstance(fields,dict):fields={}
            when=self.legacy.parse_time(fields.get('date',''),required=False)
            values={'name':fields.get('name',''),'date':when.date().isoformat() if when else '', 'time':when.strftime('%H:%M') if when else '', 'venue':fields.get('venue',''),'address':fields.get('address',''),'sourceUrl':url if safe_https(url) else '', 'host':fields.get('signature','').removesuffix(' 올림'),'greeting':fields.get('greeting',''),'note':fields.get('note','')}
            for side in ('groom','bride'):
                for tail in ('Name','Parents','Relation'):values[side+tail]=fields.get(side+'_'+tail.lower(),'')
            warnings_out.append('원본을 모두 읽지 못했습니다. 누락된 항목만 보완해 주세요. 원본 링크는 유지했습니다.')
        values=clean_values(values)
        if data['kind']=='wedding' and data.get('automatic') is True:
            import erp_invitation_auto
            self.progress(data.get('requestId',''),3,'커플 사진 4장 선별','중복·독사진·흑백 사진을 제외하고 있습니다.',completedPhotos=0)
            values,photo_warnings=erp_invitation_auto.select_photos(self,values,progress=lambda done,total:self.progress(data.get('requestId',''),3,'커플 사진 4장 선별',f'{done}/{total}장 확인',completedPhotos=done))
            warnings_out.extend(photo_warnings)
        if data['kind']=='event' and not values['host']:
            labelled=re.search(r'(?:주최(?:\s*측)?|주관)\s*[:：]\s*([^\n.;。]{1,100})',source)
            if labelled:values['host']=labelled.group(1).strip()
        if data['kind']=='wedding' and values['groomName'] and values['brideName']:values['name']=values['groomName']+' '+values['brideName']+' 결혼'
        return {'status':'done','values':values,'missing':missing(data['kind'],values),'warnings':warnings_out}

    def template_check(self,number,client=None):
        if not isinstance(number,str) or not re.fullmatch(r'[0-9]{1,20}',number):raise ValueError('복제 원본 상품 번호를 확인해 주세요.')
        if client is None:
            from blogpay import BlogPay
            client=BlogPay(os.getenv('BLOGPAY_SHOP_ID',''),os.getenv('BLOGPAY_API_TOKEN',''))
            with self.legacy.PRODUCT_LOCK:return self.template_check(number,client)
        row=client.detail(number)
        images=client.image_checksums(row)
        settings=client.settings(row)
        eligible=('쌀빛그림화환' in row.get('goodName','') and str(row.get('isRecommend'))=='0'
                  and str(row.get('isShow'))=='0' and str(row.get('optionState'))=='1'
                  and not row.get('addNormalOption') and bool(images and images[0])
                  and int(row.get('enablePayMethod') or 0)>0)
        fingerprint=digest({'productId':number,'name':row.get('goodName'),'price':row.get('goodPrice'),
                            'settings':settings,'images':[x.hex() if x else None for x in images]})
        return {'productId':number,'name':row.get('goodName',''),'price':int(row.get('goodPrice') or 0),
                'eligible':eligible,'fingerprint':fingerprint,'optionGroups':[x.get('noTitle','') for x in row.get('normalOption') or []],
                'checks':{'notOnMain':str(row.get('isRecommend'))=='0','sourceHidden':str(row.get('isShow'))=='0',
                          'hasImages':bool(images and images[0]),'paymentEnabled':int(row.get('enablePayMethod') or 0)>0}}

    def products(self,title):
        from blogpay import BlogPay
        shop,token=os.getenv('BLOGPAY_SHOP_ID'),os.getenv('BLOGPAY_API_TOKEN')
        if not shop or not token:raise ValueError('기존 블로그페이 서버 연결을 확인하세요.')
        client=BlogPay(shop,token)
        with self.legacy.PRODUCT_LOCK:
            cached=getattr(self,'catalog_cache',None)
            if cached is None or time.monotonic()-getattr(self,'catalog_cached_at',0)>300:
                self.catalog_cache=list(client.products(all_pages=True))
                self.catalog_cached_at=time.monotonic()
            rows=client.existing_products(title,self.catalog_cache)
            if set(rows)!=set(PRICES):
                template=os.getenv('BLOGPAY_INVITATION_TEMPLATE_ID','').strip()
                pinned=os.getenv('BLOGPAY_INVITATION_TEMPLATE_FINGERPRINT','').strip()
                if os.getenv('BLOGPAY_INVITATION_AUTO_CREATE')!='1':
                    raise ValueError('신규 상품 자동 생성은 주소 입력 조건 검증으로 중지되어 있습니다. 이미 등록된 두 가격 상품은 연결할 수 있습니다.')
                if not template or not pinned:
                    raise ValueError('해당 행사명의 두 가격 상품이 없습니다. 검증한 복제 원본 상품 번호를 지정해 주세요.')
                check=self.template_check(template,client)
                if not check['eligible'] or check['fingerprint']!=pinned:raise ValueError('복제 원본 설정이 검증값과 다릅니다. 원본을 다시 확인해 주세요.')
                client.ensure_links(title,self.root,int(template))
                if self.template_check(template,client)['fingerprint']!=pinned:raise ValueError('복제 중 원본 설정이 변경됐습니다. 저장된 상품을 먼저 확인해 주세요.')
                self.catalog_cache=list(client.products(all_pages=True));self.catalog_cached_at=time.monotonic()
                rows=client.existing_products(title,self.catalog_cache)
                if set(rows)!=set(PRICES):raise ValueError('두 가격의 복제 결과를 모두 확인해야 합니다. 기존 복제 원장을 유지합니다.')
            result=[]
            for price in PRICES:
                row=client.detail(rows[price]['goodNum'])
                if int(row.get('goodPrice',0))!=price or client.normalized_title(row.get('goodName',''))!=client.normalized_title(title) or str(row.get('isShow'))!='1' or str(row.get('isSoldout'))!='0' or str(row.get('isRecommend'))!='0':raise ValueError('상품 상세의 이름·가격·판매 상태·메인진열을 확인하세요.')
                snapshot=None
                for attempt in range(2):
                    try:snapshot=self.legacy.checkout.product_snapshot(client,row);break
                    except OSError:
                        if attempt:raise
                if snapshot['price']!=price or snapshot['number']!=str(row['goodNum']):raise ValueError('주문서 상품번호와 가격 불일치')
                result.append({'price':price,'productId':str(row['goodNum']),'orderUrl':client.order_link(row),'verified':True})
        if len({p['productId'] for p in result})!=2 or len({p['orderUrl'] for p in result})!=2:raise ValueError('가격별 서로 다른 두 상품을 확인하세요.')
        return result

    def prepare(self,data):
        v=data['values'];kind=data['kind'];required=missing(kind,v)
        if required:raise ValueError('필요한 항목: '+', '.join(required))
        when,_,_=render.date_words(v)
        if not re.fullmatch(r'20\d\d-\d\d-\d\d',v['date']) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',v['time']):raise ValueError('행사 일시 확인 필요')
        year,month=(when.year+1,1) if when.month==12 else (when.year,when.month+1)
        expires=dt.date(year,month,min(when.day,calendar.monthrange(year,month)[1])).isoformat()
        if dt.datetime.now(render.SEOUL).date().isoformat()>expires:raise ValueError('공유 기간이 지난 행사입니다.')
        # Keep the main card photo unchanged. Frame and scroll derivatives use
        # the shared automatic-scroll face-safe/edge-preserving fallback.
        self.progress(data.get('requestId',''),4,'액자·족자 제작','얼굴 보존 크롭과 세로 액자·배너 정렬을 계산하고 있습니다.')
        photos={}
        for key in IMAGE_FIELDS:
            if v.get(key):
                self.check_cancelled(data.get('requestId',''));photos[key]=self.image(v[key]);self.check_cancelled(data.get('requestId',''))
        import erp_invitation_auto
        if kind=='wedding':
            parts=erp_invitation_auto.components(render,v,kind,photos)
            image,boxes=erp_invitation_auto.preview_composite(render,parts)
        elif kind=='memorial':
            import erp_memorial_auto
            parts=erp_memorial_auto.components(render,v)
            image,boxes=erp_memorial_auto.preview_composite(render,parts)
        else:
            image,boxes=render.composite(v,kind,photos)
            parts=erp_invitation_auto.components(render,v,kind,photos,image)
        local=data.get('orderMode')=='invitation-checkout'
        prices='93,500원·120,000원' if local else '93,500원·99,000원'
        self.progress(data.get('requestId',''),5,'두 가격 주문서 생성',prices+' 주문서를 준비하고 있습니다.')
        local_key,items,snapshots=erp_local_checkout.build(self.checkout_root,self.origin,data) if local else ('',erp_manual_blogpay.products(v) if data.get('orderMode')=='blogpay-manual' else self.products(render.product_name(v,kind)),{})
        sample=render.image_bytes(image)
        hero=render.image_bytes(photos['hero']) if kind=='wedding' else b''
        og=render.thumbnail(v,kind)
        hashes={k:hashlib.sha256(render.image_bytes(photo)).hexdigest() for k,photo in photos.items()}
        fingerprint=digest({'values':v,'kind':kind,'items':items,'images':hashes,'orderMode':data.get('orderMode'),**({'renderVersion':getattr(erp_invitation_auto,'RENDER_VERSION','wedding-rules-v2')} if kind=='wedding' else {'renderVersion':erp_memorial_auto.RENDER_VERSION} if kind=='memorial' else {}),**({'orderMode':'invitation-checkout','products':snapshots} if local else {})})
        warnings_out=[]
        if len(set(hashes.values()))<len(hashes):warnings_out.append('원본이 부족한 제작 위치는 같은 커플 사진을 역할별 안전 크롭으로 사용했습니다.')
        if kind=='event' and not photos.get('logo'):warnings_out.append('로고·행사 이미지가 없어 행사명으로 시안을 구성했습니다. 설치 예시를 확인하세요.')
        return {'status':'done','items':items,'fingerprint':fingerprint,'expires':expires,'warnings':warnings_out}, {**({'local-products.json':json.dumps({'key':local_key,'products':snapshots}).encode()} if local else {}),'reference-components.json':json.dumps(parts).encode(),'sample.webp':sample,'hero.webp':hero,'thumbnail.jpg':og,'layout.json':json.dumps(boxes,ensure_ascii=False).encode()}

    def recover_publication(self,row,data):
        """Recover the completed filesystem commit if the process died before journal commit."""
        if data['action']!='publish':return None
        with LOCK:
            self.check_cancelled(row['id'])
            folder=self.root/'public'/row['public_id']
            try:result=json.loads((folder/'result.json').read_text(encoding='utf-8'))
            except FileNotFoundError:return None
            items=result.get('items') or []
            complete=(result.get('status')=='done' and result.get('jobId')==row['public_id']
                      and result.get('fingerprint')==data.get('fingerprint')
                      and len(items)==2 and {i.get('price') for i in items}==set(PRICES)
                      and all((folder/name).is_file() for name in ('93500.html','99000.html','sample.webp','thumbnail.jpg')))
            if not complete:raise ValueError('저장된 발행 결과의 두 가격을 확인해야 합니다. 기존 요청 ID를 유지해 주세요.')
            publication_key=f"{data['invitationId']}:{data['version']}"
            with self.db() as db:
                old=db.execute('SELECT fingerprint FROM publications WHERE key=?',(publication_key,)).fetchone()
                if old and old['fingerprint']!=result['fingerprint']:raise ValueError('발행 원장의 버전이 일치하지 않습니다.')
                db.execute('INSERT OR IGNORE INTO publications VALUES(?,?,?)',(publication_key,result['fingerprint'],json.dumps(result,ensure_ascii=False)))
            return result

    def finish(self,row,data,result,*,committed=False):
        """Terminal result and library registration share the cancellation fence."""
        key=row['id']
        with LOCK:
            self.check_cancelled(key)
            with self.db() as db:current_row=db.execute('SELECT state,result FROM jobs WHERE id=?',(key,)).fetchone()
            if not current_row:return None
            if current_row['state']=='done' or current_row['state']=='error' and not committed:return json.loads(current_row['result'])
            if data['action']=='publish':
                self.progress(key,7,'초대장 관리 등록','발행된 두 링크와 연결 상품을 관리 목록에 등록하고 있습니다.')
                pending={'input':json.dumps(data,ensure_ascii=False),'result':json.dumps(result,ensure_ascii=False),'public_id':row['public_id'],'created_at':row['created_at']}
                erp_invitation_library.register(self,self.checkout_root,pending=pending)
            current=self.get(key) or {};progress=current.get('progress')
            result={**result,'requestId':key,**({'progress':progress} if isinstance(progress,dict) else {})}
            with self.db() as db:db.execute("UPDATE jobs SET state='done',result=? WHERE id=? AND state IN ('queued','running'"+(",'error'" if committed else '')+")",(json.dumps(result,ensure_ascii=False),key))
            return result

    def publish_commit(self,row,data,result,assets):
        """Prepare HTML first; cancel and the final visibility commit are atomic."""
        key=row['id']
        with self.db() as db:previous=db.execute("SELECT * FROM jobs WHERE id=? AND action='preview' AND state='done'",(data.get('previewRequestId'),)).fetchone()
        if not previous or previous['invitation_id']!=data['invitationId'] or previous['version']!=data['version'] or json.loads(previous['result']).get('fingerprint')!=data.get('fingerprint') or result['fingerprint']!=data.get('fingerprint'):raise ValueError('상품 또는 사진이 변경됐습니다. 현재 버전을 다시 미리보기하세요.')
        if not safe_https(self.origin):raise ValueError('고객용 공개 HTTPS 주소를 확인하세요.')
        self.progress(key,6,'초대장 발행','두 가격의 초대장·썸네일·주문 링크를 저장하고 있습니다.')
        origin=self.origin+'/erp/i/'+row['public_id'];documents={};items=[]
        for item in result['items']:
            self.check_cancelled(key)
            price=item['price'];url=origin+f'/{price}.html'
            amount=own_checkout_pricing.current_base(price) if data.get('orderMode')=='invitation-checkout' else price
            documents[f'{price}.html']=render.page(data['values'],data['kind'],amount,item['orderUrl'],json.loads(assets['reference-components.json']) if data['kind'] in ('wedding','memorial') else origin+'/sample.webp',origin+'/hero.webp' if assets['hero.webp'] else '',origin+'/thumbnail.jpg',url)
            items.append({**item,'url':url,'message':render.message(data['values'],data['kind'],amount,url)})
        result={**result,'items':items,'jobId':row['public_id'],'publishedAt':dt.datetime.now(dt.timezone.utc).isoformat(),'thumbnail':origin+'/thumbnail.jpg'}
        with LOCK:
            self.check_cancelled(key)
            public=self.root/'public'/row['public_id'];public.mkdir(parents=True,exist_ok=True)
            for name,raw in assets.items():
                if raw:(public/name).write_bytes(raw)
            for name,page in documents.items():(public/name).write_text(page,encoding='utf-8')
            if data.get('orderMode')=='invitation-checkout':
                local=json.loads(assets['local-products.json']);erp_local_checkout.publish(self.checkout_root,local['key'],local['products'],row['public_id'],result['fingerprint'],self.origin,data['kind']=='wedding',data['values'].get('weddingCrop'))
            # Manifest last: a cancelled request never enters this fenced commit.
            temp=public/'result.tmp';temp.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');temp.replace(public/'result.json')
            with self.db() as db:db.execute('INSERT INTO publications VALUES(?,?,?)',(f"{data['invitationId']}:{data['version']}",result['fingerprint'],json.dumps(result,ensure_ascii=False)))
            return self.finish(row,data,result,committed=True)

    def run(self,key):
        try:
            with LOCK,self.db() as db:
                row=db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
                if not row or row['state'] not in ('queued','running'):return
                db.execute("UPDATE jobs SET state='running' WHERE id=? AND state IN ('queued','running')",(key,))
            data=json.loads(row['input']);action=data['action']
            recovered=self.recover_publication(row,data)
            if recovered:result=recovered
            elif action=='parse':result=self.parse(data)
            else:
                publication_key=f"{data['invitationId']}:{data['version']}"
                with self.db() as db:published=db.execute('SELECT * FROM publications WHERE key=?',(publication_key,)).fetchone()
                if action=='publish' and published:
                    if published['fingerprint']!=data.get('fingerprint'):raise ValueError('발행 버전이 이미 존재합니다. 내용을 바꾸려면 ERP 보정값을 새 버전으로 저장하세요.')
                    result=json.loads(published['result'])
                else:
                    result,assets=self.prepare(data)
                    with LOCK:
                        self.check_cancelled(key)
                        folder=self.root/'private'/key;folder.mkdir(parents=True,exist_ok=True)
                        for name,raw in assets.items():
                            if raw:(folder/name).write_bytes(raw)
                    if action=='preview':
                        v=data['values'];sample=render.data_image(assets['sample.webp']);hero=render.data_image(assets['hero.webp']) if assets['hero.webp'] else ''
                        page=render.page(v,data['kind'],93500,'#',json.loads(assets['reference-components.json']) if data['kind'] in ('wedding','memorial') else sample,hero,render.data_image(assets['thumbnail.jpg'],'image/jpeg'),'about:blank',preview=True)
                        with LOCK:
                            self.check_cancelled(key)
                            (folder/'preview.html').write_text(page,encoding='utf-8')
                    else:
                        self.publish_commit(row,data,result,assets);return
            self.finish(row,data,result,committed=action=='publish')
        except JobCancelled:
            pass
        except Exception as error:
            import traceback
            frames=[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)[-6:]]
            print(json.dumps({'event':'ERP_INVITATION_FAILURE','requestId':key,'errorType':type(error).__name__,'frames':frames}),flush=True)
            with LOCK:
                current=self.get(key) or {};progress=current.get('progress')
                result={'requestId':key,'status':'error','error':str(error)[:500] if isinstance(error,ValueError) else '제작 서버 연결 또는 원본 자료를 확인해 주세요.',**({'progress':progress} if isinstance(progress,dict) else {})}
                with self.db() as db:db.execute("UPDATE jobs SET state='error',result=? WHERE id=? AND state IN ('queued','running')",(json.dumps(result,ensure_ascii=False),key))
        finally:
            with LOCK:self.active.discard(key)

    def public(self,key,name):
        if not re.fullmatch(r'[a-f0-9]{32}',key) or name not in ('93500.html','99000.html','thumbnail.jpg','sample.webp','hero.webp','checkout-photo.webp'):return 404,None,None
        managed=erp_invitation_library.public(self.checkout_root,key,name)
        if managed:return managed
        folder=self.root/'public'/key
        try:result=json.loads((folder/'result.json').read_text())
        except (OSError,ValueError):return 404,None,None
        if not all((folder/required).is_file() for required in ('93500.html','99000.html','sample.webp','thumbnail.jpg')):return 404,None,None
        if dt.datetime.now(render.SEOUL).date().isoformat()>result['expires']:return 410,None,None
        if name=='checkout-photo.webp':
            # Generated from a fixed, already-published local asset, never a caller URL.
            # Both price variants share the cache; immutable checkout snapshots stay intact.
            target=folder/name
            if not target.is_file():
                with LOCK:
                    if not target.is_file():
                        try:
                            import erp_wedding_photos
                            with Image.open(folder/'hero.webp') as source:
                                image=erp_wedding_photos.automatic_crop(source,(1380,915))
                            raw=render.image_bytes(image);temp=target.with_suffix('.tmp');temp.write_bytes(raw);temp.replace(target)
                        except (ValueError,OSError):return 422,'text/plain; charset=utf-8','주문서 사진 원본을 확인해 주세요.'.encode()
        try:raw=(folder/name).read_bytes()
        except OSError:return 404,None,None
        if name.endswith('.html'):
            slot=int(name.split('.')[0])
            order_url=next((item.get('orderUrl') for item in result['items'] if item.get('price')==slot),'')
            raw=own_checkout_pricing.public_copy(raw.decode(),slot,order_url).encode()
        return 200,('text/html; charset=utf-8' if name.endswith('.html') else 'image/jpeg' if name.endswith('.jpg') else 'image/webp'),raw


def engine(root,origin,legacy,extract):
    root=Path(os.getenv('ERP_INVITATION_DATA') or root)
    key=str(Path(root).resolve())
    with LOCK:
        if key not in ENGINES:ENGINES[key]=Engine(root,origin,legacy,extract)
        return ENGINES[key]


def dispatch(handler,method,path,root,origin,legacy,extract):
    """Return True when consumed. Invoke before existing do_GET/do_POST routes."""
    legacy_page=re.fullmatch(r'/i/([a-f0-9]{32})/(93500\.html|99000\.html)',path)
    if legacy_page and method in ('GET','HEAD'):
        worker=engine(root,origin,legacy,extract);reply=erp_invitation_library.public(root,*legacy_page.groups())
        if reply:
            status,mime,raw=reply;handler.send_response(status);handler.send_header('Content-Type',mime);handler.send_header('Cache-Control','no-store');handler.send_header('Content-Length',str(len(raw)));handler.end_headers()
            if method!='HEAD':handler.wfile.write(raw)
            return True
    if not path.startswith(('/erp/invitations/','/erp/i/','/erp/ai/')):return False
    public=re.fullmatch(r'/erp/i/([a-f0-9]{32})/(93500\.html|99000\.html|thumbnail\.jpg|sample\.webp|hero\.webp|checkout-photo\.webp)',path)
    if public and method in ('GET','HEAD'):
        status,mime,raw=engine(root,origin,legacy,extract).public(*public.groups())
        handler.send_response(status);handler.send_header('Cache-Control','no-store')
        handler.send_header('Content-Security-Policy',"default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'");handler.send_header('X-Content-Type-Options','nosniff');handler.send_header('Referrer-Policy','no-referrer')
        if mime:handler.send_header('Content-Type',mime)
        handler.end_headers()
        if raw and method!='HEAD':handler.wfile.write(raw)
        return True
    # Distinct token: existing invitation API / Kakao credentials remain unchanged.
    token=os.getenv('ERP_INVITATION_TOKEN','')
    tokens=[v for v in (token,os.getenv('ERP_AUTOMATION_TOKEN',''),os.getenv('ERP_INVITATION_BRIDGE_TOKEN','')) if v]
    if not any(hmac.compare_digest(handler.headers.get('Authorization',''),'Bearer '+v) for v in tokens):
        handler.send_json({'error':'초대장 서버 인증이 필요합니다.'},401);return True
    if path=='/erp/invitations/order-stats':
        from erp_invitation_stats import handle_read
        return handle_read(handler,method,path,root,origin)
    if path=='/erp/invitations/capabilities' and method=='GET':
        hashes={name:hashlib.sha256((render.ASSETS/name).read_bytes()).hexdigest() for name in TEMPLATE_HASHES if (render.ASSETS/name).is_file()}
        model=os.getenv('TAESAN_MODEL','')
        handler.send_json({'bridgeVersion':2,'oneClickPhotoSelection':True,'referenceLayout':'geonuk-jiwon','templatesReady':hashes==TEMPLATE_HASHES,'templateHashes':hashes,
                           'storagePersistent':Path(os.getenv('ERP_INVITATION_DATA') or root).resolve().is_relative_to(Path('/var/data')),'templateProductConfigured':bool(os.getenv('BLOGPAY_INVITATION_TEMPLATE_ID')),'autoCopyConfigured':os.getenv('BLOGPAY_INVITATION_AUTO_CREATE')=='1','openaiConfigured':bool(os.getenv('OPENAI_API_KEY')),'blogpayConfigured':bool(os.getenv('BLOGPAY_SHOP_ID') and os.getenv('BLOGPAY_API_TOKEN')),
                           'textModel':model if re.fullmatch(r'(?:gpt-|o[134])[a-zA-Z0-9_.:-]{1,90}',model) else '',
                           'transcriptionModel':os.getenv('TAESAN_TRANSCRIBE_MODEL') or 'gpt-4o-mini-transcribe'})
        return True
    if path=='/erp/invitations/templates' and method=='POST':
        try:
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=12*1024*1024:raise ValueError('설치 예시 크기를 확인하세요.')
            data=json.loads(handler.rfile.read(size));name=data.get('name');raw=base64.b64decode(data.get('data',''),validate=True)
            if name not in TEMPLATE_HASHES or hashlib.sha256(raw).hexdigest()!=TEMPLATE_HASHES[name]:raise ValueError('승인된 설치 예시 원본이 필요합니다.')
            with LOCK:
                render.ASSETS.mkdir(parents=True,exist_ok=True);target=render.ASSETS/name
                if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()!=TEMPLATE_HASHES[name]:raise ValueError('다른 템플릿 파일이 있습니다. 기존 파일을 확인하세요.')
                temp=render.ASSETS/(name+'.upload');temp.write_bytes(raw);temp.replace(target)
            handler.send_json({'stored':True,'name':name,'sha256':TEMPLATE_HASHES[name]},201)
        except (ValueError,TypeError,KeyError):handler.send_json({'error':'승인된 설치 예시 원본과 형식을 확인하세요.'},422)
        return True
    if erp_ai.dispatch(handler,method,path):return True
    worker=engine(root,origin,legacy,extract)
    if erp_invitation_library.dispatch(handler,method,path,root,worker):return True
    if erp_invitation_management.dispatch(handler,method,path,root,worker):return True
    try:
        if method=='GET' and path=='/erp/invitations/template-candidates':
            from blogpay import BlogPay
            client=BlogPay(os.getenv('BLOGPAY_SHOP_ID',''),os.getenv('BLOGPAY_API_TOKEN',''))
            configured=os.getenv('BLOGPAY_INVITATION_TEMPLATE_ID','').strip()
            if configured:items=[worker.template_check(configured,client)]
            else:
                items=[]
                for r in client.products(all_pages=True):
                    if '쌀빛그림화환' not in r.get('goodName','') or str(r.get('isShow'))!='0' or str(r.get('isRecommend'))!='0':continue
                    items.append({'productId':str(r['goodNum']),'name':r.get('goodName',''),'price':int(r.get('goodPrice') or 0),'hidden':True})
                    if len(items)>=12:break
                # Detailed image/settings fingerprints are fetched only for the selected source.
            handler.send_json({'items':items,'configured':configured or None});return True
        diagnostic=re.fullmatch(r'/erp/invitations/jobs/([\w-]{10,100})/diagnostics',path)
        if method=='GET' and diagnostic:
            with worker.db() as db:row=db.execute('SELECT * FROM jobs WHERE id=?',(diagnostic[1],)).fetchone()
            if not row:handler.send_json({'error':'작업 없음'},404);return True
            data=json.loads(row['input']);title=render.product_name(data['values'],data['kind'])
            copies=[];ledger=worker.root/'invitation_products.sqlite3'
            if ledger.is_file():
                with sqlite3.connect(ledger) as db:
                    for price in PRICES:
                        copy_key=hashlib.sha256(f'{title}\0{price}'.encode()).hexdigest()
                        record=db.execute('SELECT number,before FROM copies WHERE key=?',(copy_key,)).fetchone()
                        copies.append({'price':price,'recorded':bool(record),'productId':record[0] if record else None,'baselineProductCount':len(json.loads(record[1])) if record else None})
            if any(x['productId'] for x in copies):
                from blogpay import BlogPay
                client=BlogPay(os.getenv('BLOGPAY_SHOP_ID',''),os.getenv('BLOGPAY_API_TOKEN',''))
                template=client.detail(os.getenv('BLOGPAY_INVITATION_TEMPLATE_ID'))
                for entry in copies:
                    if not entry['productId']:continue
                    item=client.detail(entry['productId'])
                    entry['matches']={'name':item.get('goodName')==title,'price':int(item.get('goodPrice',0))==entry['price'],'settings':client.settings(item)==client.settings(template),'images':client.image_checksums(item)==client.image_checksums(template)}
                    entry['shippingPolicy']={'source':template.get('shipSubNum'),'copy':item.get('shipSubNum')}
                    entry['effectiveSettingsMatch']=client.same_settings(item,template)
                    entry['differentSettingKeys']=[k for k in template if k in item and item[k]!=template[k] and k not in ('goodNum','goodName','goodPrice','goodImgUri','goodImg','subImg1','subImg2','subImg3','subImg4','shortGoodUrl')]
                    keys=('isDeliveryPrice','isTodayShipUse','todayShipTime','isEvent','goodsCode','subtitleColor')
                    entry['settingChanges']={k:{'source':template.get(k),'copy':item.get(k)} for k in keys if template.get(k)!=item.get(k)}
                    def flags(row):
                        return {'normal':[[x.get('noRequire'),[[v.get(k) for k in ('noPrice','noInven','noSourcePrice','optionView')] for v in x.get('list',[])]] for x in row.get('normalOption') or []],'input':[[x.get('ioPrice'),x.get('ioRequire')] for x in row.get('inputOption') or []]}
                    entry['optionFlags']={'source':flags(template),'copy':flags(item)}
            handler.send_json({'requestId':diagnostic[1],'state':row['state'],'copies':copies,'templateConfigured':bool(os.getenv('BLOGPAY_INVITATION_TEMPLATE_ID'))});return True
        if method=='POST' and path=='/erp/invitations/template-check':
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=1000:raise ValueError('원본 상품 번호 입력을 확인하세요.')
            data=json.loads(handler.rfile.read(size));handler.send_json(worker.template_check(data.get('productId')));return True
        if method=='POST' and path=='/erp/invitations/jobs':
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=80000:raise ValueError('입력 크기 오류')
            data=json.loads(handler.rfile.read(size));result=worker.submit(data)
            handler.send_json(result,200 if result['status'] in ('done','error','cancelled') else 202);return True
        cancel_match=re.fullmatch(r'/erp/invitations/jobs/([\w-]{10,100})/cancel',path)
        if cancel_match:
            if method!='POST':handler.send_json({'error':'제작 중단은 POST 요청으로 처리합니다.'},405);return True
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<=size<=4000:raise ValueError('제작 중단 요청 크기를 확인하세요.')
            payload=json.loads(handler.rfile.read(size)) if size else {}
            try:result=worker.cancel(cancel_match[1],payload)
            except CancellationConflict as error:
                handler.send_json({**error.result,'error':str(error)},409);return True
            if result is None:handler.send_json({'error':'중단할 작업을 찾지 못했습니다.'},404)
            else:handler.send_json(result)
            return True
        if method=='POST' and path=='/erp/invitations/uploads':
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=5*1024*1024:raise ValueError('사진 크기 오류')
            handler.send_json(worker.upload(handler.rfile.read(size)),201);return True
        order_preview=re.fullmatch(r'/erp/invitations/jobs/([\w-]{10,100})/order-preview/(93500|99000)',path)
        if method=='GET' and order_preview:
            with worker.db() as c:row=c.execute("SELECT * FROM jobs WHERE id=? AND action='preview' AND state='done'",(order_preview[1],)).fetchone()
            if not row:handler.send_json({'error':'검증된 주문서 없음'},404);return True
            file=worker.root/'private'/row['id']/'local-products.json'
            if not file.is_file():handler.send_json({'error':'자체 주문서가 아닙니다.'},409);return True
            product=json.loads(file.read_text())['products'][order_preview[2]]
            if json.loads(row['input'])['kind']=='wedding':product['wedding_photo']='https://preview.invalid/hero.webp'
            if product.get('kind')=='memorial':product['images']=[render.data_image((worker.root/'private'/row['id']/'sample.webp').read_bytes())]
            page=legacy.checkout.render(own_checkout_pricing.apply(product,order_preview[2]),'#',can_pay=False)
            hero=worker.root/'private'/row['id']/'hero.webp'
            if hero.is_file():page=page.replace('https://preview.invalid/hero.webp',render.data_image(hero.read_bytes()))
            raw=page.encode();handler.send_response(200);handler.send_header('Content-Type','text/html; charset=utf-8');handler.send_header('Cache-Control','no-store');handler.end_headers();handler.wfile.write(raw);return True
        match=re.fullmatch(r'/erp/invitations/jobs/([\w-]{10,100})(/preview)?',path)
        if method=='GET' and match:
            result=worker.get(match[1])
            if not result:handler.send_json({'error':'작업 없음'},404)
            elif match[2]:
                with worker.db() as db:row=db.execute("SELECT id FROM jobs WHERE id=? AND action='preview' AND state='done'",(match[1],)).fetchone()
                if not row:handler.send_json({'error':'미리보기 없음'},404)
                else:
                    raw=(worker.root/'private'/match[1]/'preview.html').read_bytes();handler.send_response(200);handler.send_header('Content-Type','text/html; charset=utf-8');handler.send_header('Cache-Control','no-store');handler.end_headers();handler.wfile.write(raw)
            else:handler.send_json(result)
            return True
        handler.send_json({'error':'Not found'},404)
    except (ValueError,TypeError,KeyError) as error:handler.send_json({'error':str(error)[:300]},422)
    except Exception:handler.send_json({'error':'기존 작업 ID로 상태를 확인해 주세요.'},503)
    return True
