"""Four synthetic, payment-disabled order forms; no merchant calls."""
import json,shutil,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import checkout
from PIL import Image,ImageDraw
folder=Path(sys.argv[1] if len(sys.argv)>1 else 'work/checkout-demo');folder.mkdir(parents=True,exist_ok=True)
shutil.copytree(checkout.ARTWORK,folder/'checkout-art',dirs_exist_ok=True)
photo=Image.new('RGB',(900,600),'#eae2d6');ImageDraw.Draw(photo).text((300,290),'SYNTHETIC PHOTO PLACEHOLDER',fill='black');photo.save(folder/'demo-photo.webp')
for kind in ('event','wedding'):
 for price in (93500,99000):
  product={'number':str(price),'title':'가상 일반 행사' if kind=='event' else '가상 혼주용 예식','price':price,'images':['https://demo.invalid/sunflower.webp'],'venue':'가상 문화홀','address':'가상 테스트로 10','date':'2099년 12월 13일','clock':'오후 1시','event_time':'2099-12-13T13:00:00+09:00','groups':[
   {'label':'상품 선택','choices':{str(kg):{'label':f'쌀 {kg}kg','amount':(kg-10)*1000,'custom':False} for kg in (10,20,30)}},
   {'label':'받으시는 분','choices':{'host':{'label':'가상 주최 측','amount':0,'custom':False}}},
   {'label':'축하 문구','choices':{'greeting':{'label':'1. 축하합니다','amount':0,'custom':False},'custom':{'label':'직접 입력','amount':0,'custom':True}}}]}
  if kind=='wedding':product.update(wedding_photo='https://demo.invalid/photo.webp',wedding_crop=50)
  page=checkout.render(product,'#',can_pay=False).replace('https://demo.invalid/photo.webp','/demo-photo.webp').replace('https://demo.invalid/sunflower.webp','/checkout-art/sunflower-easel.webp')
  page=page.replace('<body>','<body><p role="status">가상 검수 전용 · 결제 비활성 · 실제 주문이 아닙니다.</p>')
  (folder/f'{kind}-{price}.html').write_text(page,encoding='utf8')
print(json.dumps({'samples':[f'{kind}-{price}.html' for kind in ('event','wedding') for price in (93500,99000)],'payments':False}))
