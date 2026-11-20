"""Standalone memorial-link extraction and funeral invitation/banner rendering."""
import datetime as dt
import html as escape_html
import json
import re
from urllib.parse import urlsplit,urljoin
from lxml import html
from PIL import Image,ImageDraw

FIELDS={'productPrefix':100,'deceasedName':80,'mourners':800,'departure':100,'admission':100,'funeralRoom':120,'burial':200,'contactPhone':30,'deceasedDescription':160,'deathDate':100,'funeralPhone':30,'portrait':1000,'entrance':100,'originalNotice':700}
MOURNER_ROLE_WORDS=frozenset({'상주','대표이사','대표','회장','사장','이사장','원장','목사','고객'})
MOURNING_RELATION_WORDS=('외조모상','외조부상','배우자상','장모상','장인상','빙모상','빙부상','모친상','부친상','조모상','조부상','자녀상','부군상','형제상','자매상','아들상','딸상')
MOURNING_RELATION_RE=re.compile(r'('+'|'.join(map(re.escape,MOURNING_RELATION_WORDS))+r')\s*$')

def fetch_source(url,bridge,method='GET',data=None):
 from urllib.parse import urlencode
 if method not in ('GET','POST'):raise ValueError('부고장 조회 요청을 확인하세요.')
 if method=='POST' and ((urlsplit(url).hostname or '').removeprefix('www.')!='wooribugo4.com' or urlsplit(url).path!='/page/ajax/funeral/ajax_funeral_account_load.php'):raise ValueError('계좌안내 조회 주소를 확인하세요.')
 for _ in range(4):
  if not bridge.safe_https(url):raise ValueError('공개 HTTPS 부고장 주소를 확인하세요.')
  p=urlsplit(url);conn=bridge.PublicHTTPSConnection(p.hostname,443,timeout=10)
  try:
   conn.request(method,(p.path or '/')+('?' + p.query if p.query else ''),body=urlencode(data or {}).encode() if method=='POST' else None,headers={'User-Agent':'Mozilla/5.0 (compatible; Bellavita/1.0)','Accept':'text/html,application/json','Content-Type':'application/x-www-form-urlencoded'})
   r=conn.getresponse()
   if r.status in (301,302,303,307,308):
    if method!='GET':raise ValueError('계좌안내 조회 주소가 변경되었습니다.')
    url=urljoin(url,r.getheader('Location',''));continue
   if r.status!=200:raise ValueError('부고장 서버 응답 '+str(r.status))
   raw=r.read(2_000_001)
   if len(raw)>2_000_000:raise ValueError('부고장 용량 초과')
   return raw.decode('utf-8','replace')
  finally:conn.close()
 raise ValueError('부고장 주소 이동이 너무 많습니다.')

def normalize_mourner_name(value):
 from unicodedata import normalize
 text=normalize('NFKC',str(value or '')).strip()
 text=re.sub(r'\s*(?:'+'|'.join(map(re.escape,MOURNING_RELATION_WORDS))+r'|별세|부고)\s*$','',text)
 text=re.sub(r'\s*님\s*$','',text).strip()
 roles=r'(?:상주|대표이사|대표|회장|사장|이사장|원장|목사|고객)(?:님)?|님'
 for _ in range(3):
  compact=re.sub(r'\s+','',text)
  if re.fullmatch(r'[가-힣]{2,4}',compact) and compact not in MOURNER_ROLE_WORDS:break
  updated=re.sub(r'\s*(?:[\[(（]\s*(?:'+roles+r')\s*[\])）]|(?:'+roles+r'))\s*$','',text).strip()
  if updated==text:break
  text=updated
 return re.sub(r'\s+','',text)

def clean_mourner_accounts(rows,target_mourner=''):
 """Only explicitly associated account data for one exact chosen customer."""
 target=normalize_mourner_name(target_mourner)
 if not target or not isinstance(rows,list):return []
 limits={'name':80,'relation':80,'bank':80,'account':100,'holder':80,'phone':30};selected={}
 for row in rows:
  if not isinstance(row,dict):continue
  if normalize_mourner_name(row.get('name'))!=target:continue
  if not isinstance(row.get('account'),str) or not row['account'].strip():continue
  if any(key in row and (not isinstance(row[key],str) or len(row[key])>limit) for key,limit in limits.items()):raise ValueError('선택한 상주의 계좌 정보 형식과 길이를 확인하세요.')
  item={k:row[k].strip() for k in limits if isinstance(row.get(k),str)}
  if not item.get('name'):continue
  identity=(re.sub(r'\s+','',item.get('bank','')),re.sub(r'\s+','',item['account']))
  if identity not in selected:selected[identity]=item
 return list(selected.values()) if len(selected)==1 else []

def _text(element):return ' '.join(element.text_content().split()) if element is not None else ''
def _visible(element):
 for node in [element,*element.iterancestors()]:
  style=re.sub(r'\s+','',node.get('style','')).lower()
  if node.get('hidden') is not None or node.get('aria-hidden','').lower()=='true' or 'display:none' in style:return False
 return True

def account_rows(raw):
 """Read the bank field or the same-row official public copy-button value."""
 try:tree=html.fromstring(raw)
 except (ValueError,html.etree.ParserError):return []
 result=[]
 for node in tree.xpath('//*[@copybank-mourner_name]'):
  boxes=node.xpath('ancestor::*[contains(concat(" ",normalize-space(@class)," ")," user-list ")][1]')
  if not boxes:continue
  box=boxes[0];numbers=box.xpath('.//*[contains(concat(" ",normalize-space(@class)," ")," bank_num ")]')
  numbers=[entry for entry in numbers if _visible(entry)]
  if len(numbers)!=1:continue
  name=_text(node);account=_text(numbers[0])
  copy_id=node.get('copybank-mourner_name')
  copy_actions=box.xpath('.//*[@copybank=$key]',key=copy_id)
  copy_values=box.xpath('.//*[@copybank-num=$key]',key=copy_id)
  if copy_actions and len(copy_values)==1 and _text(copy_values[0]):account=_text(copy_values[0])
  role=box.xpath('.//*[contains(concat(" ",normalize-space(@class)," ")," item0 ")]//b[contains(@class,"name")]')
  banks=box.xpath('.//*[@copybank-bank]');holders=box.xpath('.//*[@copybank-name]')
  if name and account:result.append({'name':name,'relation':_text(role[0]) if role else '', 'bank':_text(banks[0]) if banks else '', 'account':account,'holder':_text(holders[0]) if holders else ''})
 for node in tree.xpath('//*[@data-account or @data-account-number]'):
  if not _visible(node):continue
  name=node.get('data-name') or node.get('data-mourner') or '';account=node.get('data-account') or node.get('data-account-number') or ''
  if name and account:result.append({'name':name,'relation':node.get('data-relation',''),'bank':node.get('data-bank',''),'account':account,'holder':node.get('data-holder',''),'phone':node.get('data-phone','')})
 return result

def extract(source,fetcher=None,target_mourner='',account_fetcher=None):
 from urllib.parse import parse_qs
 urls=list(dict.fromkeys(re.findall(r'https://[^\s<>"\]]+',source)));url=urls[0].rstrip(').,') if len(urls)==1 else '';warnings=[];text=source
 if len(urls)>1:raise ValueError('부고장 링크는 한 개씩 입력하세요.')
 if url:
  try:text=(fetcher or (lambda _:''))(url)+'\n'+source.replace(url,'')
  except (ValueError,OSError) as e:text=source.replace(url,'');warnings.append(str(e)+' · 원문 내용을 붙여넣어 보완하세요.')
 accounts=account_rows(text) if '<' in text else [];tree=None;public={};family_lines=[];phones={}
 if '<' in text:
  tree=html.fromstring(text)
  # This is the source page's existing read-only '계좌안내' button endpoint.
  # Its separate send-SMS/payment endpoints are never called.
  if normalize_mourner_name(target_mourner) and url and account_fetcher and (urlsplit(url).hostname or '').removeprefix('www.')=='wooribugo4.com' and 'ajax_funeral_account_load.php' in text:
   query=parse_qs(urlsplit(url).query);view=(query.get('_') or [''])[0]
   if not view:
    m=re.search(r'view\s*:\s*[\'"]([A-Za-z0-9+/=]{1,200})[\'"]',text);view=m[1] if m else ''
   if view:
    try:
     endpoint=urljoin(url,'/page/ajax/funeral/ajax_funeral_account_load.php')
     accounts.extend(account_rows(account_fetcher(endpoint,{'view':view,'url':urlsplit(url).query[:2000]})))
    except (ValueError,OSError):warnings.append('선택한 상주의 계좌안내를 읽지 못했습니다. 계좌를 임의로 추가하지 않았습니다.')
  for item in tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," name-wrap ")]/li'):
   labels=item.xpath('./*[contains(concat(" ",normalize-space(@class)," ")," title ")]');names=item.xpath('.//*[contains(concat(" ",normalize-space(@class)," ")," names ")]//b')
   if labels and names:family_lines.append(_text(labels[0])+': '+', '.join(_text(name).strip(' ,') for name in names if _text(name).strip(' ,')))
  intro=tree.xpath('//section[contains(concat(" ",normalize-space(@class)," ")," intro ")]//h1')
  if intro:
   description=_text(intro[0]);adjacent=intro[0].xpath('following-sibling::p[1]')
   if description in ('故','') and adjacent:description=(description+' '+_text(adjacent[0])).strip()
   public['deceasedDescription']=description[:160]
  addr=tree.xpath('//*[@id="copytarget_addr"]')
  if addr:public['address']=_text(addr[0])[:250]
  hall=tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," map-wrap ")]//a[starts-with(@href,"tel:")]/@href')
  if hall:public['funeralPhone']=hall[0].removeprefix('tel:').strip()[:30]
  pics=tree.xpath('//section[contains(concat(" ",normalize-space(@class)," ")," intro ")]//img/@src|//img[contains(@class,"deceased") or contains(@class,"portrait")]/@src')
  if pics:
   candidate=urljoin(url,pics[0])
   if candidate.startswith('https://'):public['portrait']=candidate[:1000]
  for box in tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," user-list ")]'):
   numbers=box.xpath('.//a[starts-with(@href,"tel:")]/@href');names=box.xpath('.//*[contains(concat(" ",normalize-space(@class)," ")," name ")]')
   if len(numbers)==1:
    for node in names:
     name=normalize_mourner_name(_text(node))
     if name:phones[name]=numbers[0].removeprefix('tel:').strip()[:30]
  # Bank popups/inline copy payloads are not obituary prose. Remove those
  # containers before the general label reader so another family's number can
  # never become an address, notice, or fallback mourner value.
  bank_boxes=[]
  for node in tree.xpath('//*[@copybank-mourner_name]'):
   boxes=node.xpath('ancestor::*[contains(concat(" ",normalize-space(@class)," ")," user-list ")][1]')
   if boxes and boxes[0] not in bank_boxes:bank_boxes.append(boxes[0])
  for node in [*bank_boxes,*tree.xpath('//*[@data-account or @data-account-number]')]:
   if node.getparent() is not None:node.drop_tree()
  for e in tree.xpath('//script|//style|//noscript'):e.drop_tree()
  for e in tree.xpath('//br'):e.tail='\n'+(e.tail or '')
  for e in tree.xpath('//div|//p|//tr|//li|//dt|//dd|//h1|//h2|//section'):e.tail='\n'+(e.tail or '')
  text=tree.text_content()
 lines=[' '.join(x.split()) for x in text.splitlines() if x.strip()];labels={}
 names=['고인','고인명','성함','빈소','장례식장','주소','장례식장주소','상주','대표상주','발인','발인일시','입관','입관일시','입관일','발인일','장지','1차 장지','2차 장지','임종일','별세일','입실일','알림글']
 for i,line in enumerate(lines):
  m=re.match(r'^('+ '|'.join(names)+r')\s*[:：]\s*(.+)$',line)
  if m:labels.setdefault(m[1],m[2])
  elif line in names and i+1<len(lines):labels.setdefault(line,lines[i+1])
 joined='\n'.join(lines);deceased=labels.get('고인') or labels.get('고인명') or labels.get('성함') or ''
 if not deceased:
  m=re.search(r'故\s*([가-힣]{2,10})(?:\s*님|\s*[（(]|\s*(?:별세|소천|영면)|\n|$)',joined);deceased=m[1] if m else ''
 deceased=re.sub(r'^故\s*','',deceased);deceased=re.split(r'님|[（(]|별세|소천|영면',deceased)[0].strip()[:80]
 departure=labels.get('발인') or labels.get('발인일시') or labels.get('발인일') or '';match=re.search(r'(20\d{2})[.년/\-]\s*(\d{1,2})[.월/\-]\s*(\d{1,2})',departure);date=''
 if match:
  try:date=dt.date(*map(int,match.groups())).isoformat()
  except ValueError:pass
 time='';m=re.search(r'(오전|오후)?\s*(\d{1,2})\s*[:시]\s*(\d{2})',departure)
 if m:
  h=int(m[2]);minute=int(m[3]);h=h%12+(12 if m[1]=='오후' else 0) if m[1] else h
  if 0<=h<24 and 0<=minute<60:time=f'{h:02}:{minute:02}'
 venue=labels.get('장례식장','');room=labels.get('빈소','');address=public.get('address') or labels.get('주소') or labels.get('장례식장주소') or ''
 if not address:
  road=[x for x in lines if re.search(r'(?:서울|경기|인천|부산|대구|대전|광주|울산|세종|강원|충남|충북|충청|전남|전북|전라|경남|경북|경상|제주).*(?:로|길|동|읍|면)\s*\d',x)]
  if len(set(road))==1:address=road[0]
 if not room:
  found=re.search(r'((?:(?:지하|특|VIP)\s*)?\d+(?:층\s*)?(?:특\s*)?\d*호실|(?:특|VIP)\s*\d+호)',venue,re.I)
  if found:
   room=found[0].strip()
   if venue.endswith(room):venue=venue[:-len(room)].strip(' /·,')
 if not venue and room:venue=room
 target=normalize_mourner_name(target_mourner);selected=clean_mourner_accounts(accounts,target_mourner)
 if not target:warnings.append('고객 상주를 직접 지정하면 해당 상주의 계좌만 가져옵니다. 다른 상주 계좌는 저장하지 않았습니다.')
 elif not selected:warnings.append('지정한 고객 상주의 계좌를 정확히 하나로 확인하지 못했습니다. 다른 상주 계좌를 대신 가져오지 않았습니다.')
 if selected and target in phones:selected[0]['phone']=phones[target]
 if selected and re.search(r'[*•●×xX]',selected[0]['account']):warnings.append('원본에서 계좌 일부가 가려져 있습니다. 가려진 번호를 복원하지 않았습니다.')
 notice=labels.get('알림글','')
 notice_digits=re.sub(r'\D','',notice)
 if re.search(r'(?:계좌|은행|예금주).*\d[\d -]{4,}',notice) or any(re.sub(r'\D','',row.get('account','')) in notice_digits for row in accounts if len(re.sub(r'\D','',row.get('account','')))>=5):notice=''
 values={'productPrefix':'','name':('故 '+deceased+' 부고') if deceased else '', 'deceasedName':deceased,'date':date,'time':time,'venue':venue,'funeralRoom':room,'address':address,'host':target_mourner.strip() or labels.get('대표상주',''),'mourners':'\n'.join(family_lines) or labels.get('상주',''),'departure':departure,'admission':labels.get('입관') or labels.get('입관일시') or labels.get('입관일') or '','burial':labels.get('장지') or ' / '.join(x for x in [labels.get('1차 장지'),labels.get('2차 장지')] if x),'sourceUrl':url,'greeting':'삼가 고인의 명복을 빕니다.','note':'','mournerAccounts':selected,'deathDate':labels.get('임종일') or labels.get('별세일') or '', 'entrance':labels.get('입실일',''),'originalNotice':notice,'contactPhone':phones.get(target,'')}
 values.update({key:value for key,value in public.items() if key!='address'})
 warnings.append('발인 일시는 실제 배송 일시와 다릅니다. 초대장은 링크 발행 후 72시간 운영합니다. 원문과 빈소·상주 정보를 확인하세요.')
 return values,warnings

def product_title(values):
 """Canonical public title: mourner name + 님, relationship, venue and room."""
 prefix=values.get('productPrefix','').strip()
 if not prefix:raise ValueError('상품명 앞부분을 직접 입력하세요. 예: 홍길동 모친상')
 # A title such as "정명천 회장님 모친상" is staff-facing source data.
 # Public product names must not expose the job title: "정명천님 모친상".
 # Prefer the explicitly selected customer mourner, but safely fall back to the
 # prefix for older records that predate a separate host field.
 mourner=normalize_mourner_name(values.get('host')) or normalize_mourner_name(prefix)
 if not mourner:raise ValueError('상품명에 넣을 상주 이름을 확인하세요.')
 relation=MOURNING_RELATION_RE.search(prefix)
 customer=mourner+'님'+((' '+relation.group(1)) if relation else '')
 venue=values.get('venue','').strip();room=values.get('funeralRoom','').strip()
 if not venue:raise ValueError('상품명에 넣을 장례식장을 확인하세요.')
 compact=lambda s:re.sub(r'\s+','',s)
 suffix=venue if not room or compact(room) in compact(venue) else venue+' '+room
 return customer+' · '+suffix


def canonical_product_title(value):
 """Upgrade an already-published memorial title to the public name format."""
 title=str(value or '').strip()
 prefix,separator,suffix=title.partition('·')
 relation=MOURNING_RELATION_RE.search(prefix.strip())
 # Only canonicalize the staff-entered customer-prefix form.  Obituary page
 # titles such as "故 오금순 부고" are deliberately left alone.
 if not relation:return title
 mourner=normalize_mourner_name(prefix)
 if not mourner:return title
 canonical=mourner+'님 '+relation.group(1)
 return canonical+((" · "+suffix.strip()) if separator and suffix.strip() else '')


def banner(renderer,v):
 canvas=Image.new('RGB',(1200,760),'#f3f0e8');draw=ImageDraw.Draw(canvas);draw.rectangle((28,28,1172,732),outline='#46433d',width=3)
 boxes=[renderer.fit_text(canvas,'쌀빛그림화환',(90,90,1110,195),72,fill='#252722'),renderer.fit_text(canvas,'삼가 고인의 명복을 빕니다.',(90,240,1110,335),46,fill='#33352f'),renderer.fit_text(canvas,'故 '+v['deceasedName'],(90,370,1110,470),62,fill='#252722'),renderer.fit_text(canvas,'조문의 마음을 쌀빛그림화환으로 전합니다',(90,520,1110,605),40,fill='#33352f'),renderer.fit_text(canvas,'BELLAVITA',(90,646,1110,695),29,fill='#67614f')]
 return canvas,boxes

def page(renderer,v,price,order_url,sample,og,url,preview=False):
 e=lambda value:escape_html.escape(str(value),quote=True);when,date,clock=renderer.date_words(v);source=f'<a class="original" href="{e(v.get("sourceUrl",""))}">원본 부고장 보기</a>' if v.get('sourceUrl') else ''
 return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{e(v['name'])}</title><meta property="og:title" content="{e(v['name'])}"><meta property="og:image" content="{e(og)}"><meta property="og:url" content="{e(url)}"><style>*{{box-sizing:border-box}}body{{margin:0;background:#e9e5db;color:#24271f;font:18px/1.8 system-ui,sans-serif}}main{{max-width:590px;margin:auto;background:#f9f7ef}}header,section,footer{{padding:30px 24px;text-align:center}}header{{border-top:8px solid #292c28;border-bottom:1px solid #d6d0c2}}h1{{font-size:32px}}h2{{font-size:25px}}p{{white-space:pre-line;overflow-wrap:anywhere}}dl{{text-align:left}}dt{{font-size:16px;color:#686558}}dd{{margin:0 0 20px;font-size:21px;white-space:pre-line;overflow-wrap:anywhere}}img{{width:100%;height:auto}}a{{color:inherit}}.action{{display:block;padding:18px;background:#303e35;color:white;border-radius:7px;text-decoration:none;font-weight:bold}}.preview{{background:#fff2cd;font-size:15px;padding:10px}}.products{{background:#efeee5}}.notice{{font-size:15px}}button{{font:inherit}}</style></head><body><main>{'<p class="preview">미리보기 · 결제 연결 비활성</p>' if preview else ''}<header><small>BELLAVITA · 부고 안내</small><h1>故 {e(v['deceasedName'])}</h1><p>{e(v.get('greeting') or '삼가 고인의 명복을 빕니다.')}</p></header><section><h2>장례 안내</h2><dl><dt>빈소</dt><dd>{e(v['venue'])} {e(v.get('funeralRoom','') if v.get('funeralRoom')!=v['venue'] else '')}</dd><dt>주소</dt><dd>{e(v['address'])}</dd><dt>상주</dt><dd>{e(v.get('mourners') or v.get('host') or '원본 부고장을 확인해 주세요.')}</dd><dt>발인</dt><dd>{e(date)} {e(clock)}</dd>{('<dt>입관</dt><dd>'+e(v['admission'])+'</dd>') if v.get('admission') else ''}{('<dt>장지</dt><dd>'+e(v['burial'])+'</dd>') if v.get('burial') else ''}</dl>{source}</section><section class="products"><h2>쌀빛그림화환</h2><p>조문의 마음을 쌀빛그림화환으로 전해 주세요.</p><img src="{e(sample)}" alt="고인 추모 쌀빛그림화환 안내 배너">{('<span class="action">주문 미리보기 · '+f'{price:,}'+'원</span>') if preview else ('<a class="action" href="'+e(order_url)+'">쌀빛그림화환 주문하기 · '+f'{price:,}'+'원</a>')}<p class="notice">발인 시각은 배송 시각이 아닙니다. 실제 배송 일시와 빈소는 주문 접수 후 확인합니다.</p></section><footer>친환경 액자화환 Bellavita<br><a href="tel:03180678580">문의 031-8067-8580</a></footer></main></body></html>'''
