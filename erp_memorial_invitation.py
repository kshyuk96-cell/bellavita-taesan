"""Standalone memorial-link extraction and funeral invitation/banner rendering."""
import datetime as dt
import html as escape_html
import json
import re
from urllib.parse import urlsplit,urljoin
from lxml import html
from PIL import Image,ImageDraw

FIELDS={'deceasedName':80,'mourners':800,'departure':100,'admission':100,'funeralRoom':120,'burial':200,'contactPhone':30}

def fetch_source(url,bridge):
 for _ in range(4):
  if not bridge.safe_https(url):raise ValueError('공개 HTTPS 부고장 주소를 확인하세요.')
  p=urlsplit(url);conn=bridge.PublicHTTPSConnection(p.hostname,443,timeout=10)
  try:
   conn.request('GET',(p.path or '/')+('?' + p.query if p.query else ''),headers={'User-Agent':'Mozilla/5.0 (compatible; Bellavita/1.0)','Accept':'text/html,application/json'})
   r=conn.getresponse()
   if r.status in (301,302,303,307,308):url=urljoin(url,r.getheader('Location',''));continue
   if r.status!=200:raise ValueError('부고장 서버 응답 '+str(r.status))
   raw=r.read(2_000_001)
   if len(raw)>2_000_000:raise ValueError('부고장 용량 초과')
   return raw.decode('utf-8','replace')
  finally:conn.close()
 raise ValueError('부고장 주소 이동이 너무 많습니다.')

def extract(source,fetcher=None):
 urls=list(dict.fromkeys(re.findall(r'https://[^\s<>"\]]+',source)));url=urls[0].rstrip(').,') if len(urls)==1 else '';warnings=[];text=source
 if len(urls)>1:raise ValueError('부고장 링크는 한 개씩 입력하세요.')
 if url:
  try:text=(fetcher or (lambda _:''))(url)+'\n'+source.replace(url,'')
  except (ValueError,OSError) as e:text=source.replace(url,'');warnings.append(str(e)+' · 원문 내용을 붙여넣어 보완하세요.')
 if '<' in text:
  tree=html.fromstring(text)
  for e in tree.xpath('//script|//style|//noscript'):e.drop_tree()
  for e in tree.xpath('//br'):e.tail='\n'+(e.tail or '')
  for e in tree.xpath('//div|//p|//tr|//li|//dt|//dd|//h1|//h2|//section'):e.tail='\n'+(e.tail or '')
  text=tree.text_content()
 lines=[' '.join(x.split()) for x in text.splitlines() if x.strip()];labels={}
 names=['고인','고인명','성함','빈소','장례식장','주소','장례식장주소','상주','대표상주','발인','발인일시','입관','입관일시','장지']
 for i,line in enumerate(lines):
  m=re.match(r'^('+ '|'.join(names)+r')\s*[:：]\s*(.+)$',line)
  if m:labels[m[1]]=m[2]
  elif line in names and i+1<len(lines):labels[line]=lines[i+1]
 joined='\n'.join(lines);deceased=labels.get('고인') or labels.get('고인명') or labels.get('성함') or ''
 if not deceased:
  m=re.search(r'故\s*([가-힣]{2,10})(?:\s*님|\s*[（(]|\s*(?:별세|소천|영면)|\n|$)',joined);deceased=m[1] if m else ''
 deceased=re.sub(r'^故\s*','',deceased);deceased=re.split(r'님|[（(]|별세|소천|영면',deceased)[0].strip()[:80]
 departure=labels.get('발인') or labels.get('발인일시') or '';match=re.search(r'(20\d{2})[.년/\-]\s*(\d{1,2})[.월/\-]\s*(\d{1,2})',departure);date=''
 if match:
  try:date=dt.date(*map(int,match.groups())).isoformat()
  except ValueError:pass
 time='';m=re.search(r'(오전|오후)?\s*(\d{1,2})\s*[:시]\s*(\d{2})',departure)
 if m:
  h=int(m[2]);minute=int(m[3]);h=h%12+(12 if m[1]=='오후' else 0) if m[1] else h
  if 0<=h<24 and 0<=minute<60:time=f'{h:02}:{minute:02}'
 venue=labels.get('장례식장','');room=labels.get('빈소','');address=labels.get('주소') or labels.get('장례식장주소') or ''
 if not address:
  road=[x for x in lines if re.search(r'(?:서울|경기|인천|부산|대구|대전|광주|울산|세종|강원|충남|충북|충청|전남|전북|전라|경남|경북|경상|제주).*(?:로|길|동|읍|면)\s*\d',x)]
  if len(set(road))==1:address=road[0]
 if not venue and room:venue=room
 values={'name':('故 '+deceased+' 부고') if deceased else '', 'deceasedName':deceased,'date':date,'time':time,'venue':venue,'funeralRoom':room,'address':address,'host':labels.get('대표상주',''),'mourners':labels.get('상주',''),'departure':departure,'admission':labels.get('입관') or labels.get('입관일시') or '','burial':labels.get('장지',''),'sourceUrl':url,'greeting':'삼가 고인의 명복을 빕니다.','note':''}
 warnings.append('발인 일시는 주문 마감 기준이며 실제 배송 일시와 다릅니다. 원문과 빈소·상주 정보를 확인하세요.')
 return values,warnings

def banner(renderer,v):
 canvas=Image.new('RGB',(1200,760),'#f3f0e8');draw=ImageDraw.Draw(canvas);draw.rectangle((28,28,1172,732),outline='#46433d',width=3)
 boxes=[renderer.fit_text(canvas,'쌀빛그림화환',(90,90,1110,195),72,fill='#252722'),renderer.fit_text(canvas,'삼가 고인의 명복을 빕니다.',(90,240,1110,335),46,fill='#33352f'),renderer.fit_text(canvas,'故 '+v['deceasedName'],(90,370,1110,470),62,fill='#252722'),renderer.fit_text(canvas,'조문의 마음을 쌀빛그림화환으로 전합니다',(90,520,1110,605),40,fill='#33352f'),renderer.fit_text(canvas,'BELLAVITA',(90,646,1110,695),29,fill='#67614f')]
 return canvas,boxes

def page(renderer,v,price,order_url,sample,og,url,preview=False):
 e=lambda value:escape_html.escape(str(value),quote=True);when,date,clock=renderer.date_words(v);source=f'<a class="original" href="{e(v.get("sourceUrl",""))}">원본 부고장 보기</a>' if v.get('sourceUrl') else ''
 return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{e(v['name'])}</title><meta property="og:title" content="{e(v['name'])}"><meta property="og:image" content="{e(og)}"><meta property="og:url" content="{e(url)}"><style>*{{box-sizing:border-box}}body{{margin:0;background:#e9e5db;color:#24271f;font:18px/1.8 system-ui,sans-serif}}main{{max-width:590px;margin:auto;background:#f9f7ef}}header,section,footer{{padding:30px 24px;text-align:center}}header{{border-top:8px solid #292c28;border-bottom:1px solid #d6d0c2}}h1{{font-size:32px}}h2{{font-size:25px}}p{{white-space:pre-line;overflow-wrap:anywhere}}dl{{text-align:left}}dt{{font-size:16px;color:#686558}}dd{{margin:0 0 20px;font-size:21px;white-space:pre-line;overflow-wrap:anywhere}}img{{width:100%;height:auto}}a{{color:inherit}}.action{{display:block;padding:18px;background:#303e35;color:white;border-radius:7px;text-decoration:none;font-weight:bold}}.preview{{background:#fff2cd;font-size:15px;padding:10px}}.products{{background:#efeee5}}.notice{{font-size:15px}}button{{font:inherit}}</style></head><body><main>{'<p class="preview">미리보기 · 결제 연결 비활성</p>' if preview else ''}<header><small>BELLAVITA · 부고 안내</small><h1>故 {e(v['deceasedName'])}</h1><p>{e(v.get('greeting') or '삼가 고인의 명복을 빕니다.')}</p></header><section><h2>장례 안내</h2><dl><dt>빈소</dt><dd>{e(v['venue'])} {e(v.get('funeralRoom','') if v.get('funeralRoom')!=v['venue'] else '')}</dd><dt>주소</dt><dd>{e(v['address'])}</dd><dt>상주</dt><dd>{e(v.get('mourners') or v.get('host') or '원본 부고장을 확인해 주세요.')}</dd><dt>발인</dt><dd>{e(date)} {e(clock)}</dd>{('<dt>입관</dt><dd>'+e(v['admission'])+'</dd>') if v.get('admission') else ''}{('<dt>장지</dt><dd>'+e(v['burial'])+'</dd>') if v.get('burial') else ''}</dl>{source}</section><section class="products"><h2>쌀빛그림화환</h2><p>조문의 마음을 쌀빛그림화환으로 전해 주세요.</p><img src="{e(sample)}" alt="고인 추모 쌀빛그림화환 안내 배너">{('<span class="action">주문 미리보기 · '+f'{price:,}'+'원</span>') if preview else ('<a class="action" href="'+e(order_url)+'">쌀빛그림화환 주문하기 · '+f'{price:,}'+'원</a>')}<p class="notice">발인 시각은 배송 시각이 아닙니다. 실제 배송 일시와 빈소는 주문 접수 후 확인합니다.</p></section><footer>친환경 액자화환 Bellavita<br><a href="tel:03180678580">문의 031-8067-8580</a></footer></main></body></html>'''
