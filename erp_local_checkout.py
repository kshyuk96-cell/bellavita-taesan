"""Create local two-price checkout snapshots from the existing approved own form."""
import copy
import datetime as dt
import hashlib
import json
import os
import re
import secrets
from pathlib import Path

TEMPLATE='7f494a310ca9371914b8863b45732bd5'
PRICES=(93500,99000)

def build(root,origin,data):
 v=data['values'];key=hashlib.sha256(('local-checkout:'+data['invitationId']+':'+str(data['version'])).encode()).hexdigest()[:32];items=[];snapshots={}
 template=os.getenv('ERP_CHECKOUT_TEMPLATE_JOB',TEMPLATE)
 if not re.fullmatch('[a-f0-9]{32}',template):raise ValueError('자체 주문서 기준 양식을 확인하세요.')
 for price in PRICES:
  source=Path(root)/'invitations'/template/f'{price}.order.json'
  if not source.is_file():raise ValueError('기존 자체 주문서 기준 양식이 없습니다. 93,500원·99,000원 양식을 확인하세요.')
  original=json.loads(source.read_text());groups=copy.deepcopy(original['groups'])
  if {g['label'] for g in groups}!={'축하 문구','받으시는 분','상품 선택'}:raise ValueError('기준 주문서 옵션을 확인하세요.')
  if data['kind']=='event':
   for g in groups:
    if g['label']=='받으시는 분':g['choices']={'1':{'label':v['name'],'amount':0,'custom':False}}
    if g['label']=='축하 문구':g['choices']={'0':{'label':'직접 입력','amount':0,'custom':True},'1':{'label':'행사 개최를 진심으로 축하드립니다.','amount':0,'custom':False}}
  number=str(int(hashlib.sha256((key+':'+str(price)).encode()).hexdigest()[:15],16))
  when=dt.datetime.fromisoformat(v['date']+'T'+v['time']).replace(tzinfo=dt.timezone(dt.timedelta(hours=9)))
  snapshots[price]={'number':number,'title':v['name'],'price':price,'images':original.get('images',[]),'groups':groups,'date':v['date'],'clock':v['time'],'venue':v['venue'],'address':v['address'],'source':v.get('sourceUrl',''),'event_time':when.isoformat(),'localCheckout':True,'kind':data['kind']}
  items.append({'price':price,'productId':number,'orderUrl':origin+f'/i/{key}/{price}/order','verified':True,'source':'own-checkout'})
 return key,items,snapshots

def publish(root,key,snapshots,publication,fingerprint,origin,wedding=False,crop=None):
 folder=Path(root)/'invitations'/key;folder.mkdir(parents=True,exist_ok=True)
 for price,snapshot in snapshots.items():
  product={**snapshot,'publicationId':publication,'releaseFingerprint':fingerprint}
  if wedding:product['wedding_photo']=origin+'/erp/i/'+publication+'/hero.webp'
  if crop is not None:product['wedding_crop']=crop
  raw=json.dumps(product,ensure_ascii=False);target=folder/f'{price}.order.json'
  if target.exists() and target.read_text()!=raw:raise ValueError('이미 발행된 자체 상품의 내용이 다릅니다. 새 버전으로 제작하세요.')
  temp=folder/(str(price)+'.'+secrets.token_hex(6)+'.tmp');temp.write_text(raw);temp.replace(target)
