import erp_memorial_invitation
"""Approved-layout renderer. All dynamic text is escaped; images are embedded, not fetched by customers."""
import base64
import datetime as dt
import html
import io
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps

ASSETS = Path(os.getenv('ERP_INVITATION_ASSETS') or Path(__file__).with_name('assets'))
SEOUL = dt.timezone(dt.timedelta(hours=9))
BUNDLED_SERIF_DIR = Path(__file__).with_name('erp_invitation_assets')

def font(size, bold=False):
    # Match the approved notice even on Render, which lacks system Noto fonts.
    bundled = BUNDLED_SERIF_DIR / ('NotoSerifKR-Bold.otf' if bold else 'NotoSerifKR-Regular.otf')
    if bundled.is_file():
        return ImageFont.truetype(str(bundled), size)
    path = Path('/usr/share/fonts/opentype/noto/NotoSerifCJK-' + ('Bold' if bold else 'Regular') + '.ttc')
    if path.exists():
        return ImageFont.truetype(str(path), size, index=1)
    regular = BUNDLED_SERIF_DIR / 'NotoSerifKR-Regular.otf'
    if regular.is_file():
        return ImageFont.truetype(str(regular), size)
    from importlib.util import find_spec
    package=find_spec('koreanize_matplotlib')
    if package is None or not package.origin:raise ValueError('한글 글꼴 파일 연결을 확인하세요.')
    root = Path(package.origin).parent / 'fonts'
    return ImageFont.truetype(str(root / ('NanumGothicBold.ttf' if bold else 'NanumGothic.ttf')), size)


def fit_text(canvas, text, box, size=40, fill='#192d43', bold=True, max_lines=4):
    """Fit every glyph inside a fixed box; never truncate a sender or change its content."""
    draw = ImageDraw.Draw(canvas)
    x,y,right,bottom = box
    for points in range(size, 7, -1):
        face = font(points, bold)
        lines = []
        for paragraph in str(text).split('\n'):
            line = ''
            for char in paragraph:
                if line and draw.textlength(line+char, font=face) > right-x:
                    lines.append(line); line = ''
                line += char
            lines.append(line)
        spacing = max(3, points//5)
        if len(lines) <= max_lines and len(lines)*(points+spacing) <= bottom-y:
            break
    else:
        raise ValueError('문구가 너무 깁니다. 축하 문구 또는 발신자 이름을 확인하세요.')
    height = len(lines)*(points+spacing)
    top = y+(bottom-y-height)/2
    for line in lines:
        draw.text((x+(right-x)/2,top), line, font=face, fill=fill, anchor='mt')
        top += points+spacing
    return {'text':str(text),'lines':lines,'size':points,'box':list(box)}


def paste_photo(canvas, photo, box):
    x,y,r,b = box
    draw = ImageDraw.Draw(canvas);draw.rectangle(box,fill='#f8f4ec')
    if photo:
        source = ImageOps.exif_transpose(photo).convert('RGB')
        fitted = ImageOps.contain(source, (r-x,b-y), Image.Resampling.LANCZOS)
        canvas.paste(fitted, (x+(r-x-fitted.width)//2,y+(b-y-fitted.height)//2))


def signature(v,kind):
    if kind in ('event','memorial'): return v.get('host','').rstrip('.。')
    return v.get(('bride' if v.get('hostSide')=='bride' else 'groom')+'Parents','').rstrip('.。')


def words(v,kind):
    if kind=='memorial':return v['name'],v.get('greeting') or '삼가 고인의 명복을 빕니다.',v.get('host') or v.get('mourners',''),v['name']
    parents=signature(v,kind)
    if kind=='event':
        return v['name'],v.get('greeting') or '귀한 걸음으로 함께해 주시면 감사하겠습니다.',parents,v['name']
    side='bride' if v.get('hostSide')=='bride' else 'groom'
    name=v[side+'Name'];short=name[1:] if len(name)==3 else name
    relation=v[side+'Relation']
    has_final=bool((ord(short[-1])-0xac00)%28) if short and 0xac00<=ord(short[-1])<=0xd7a3 else False
    particle='이가' if has_final else '가'
    greeting=v.get('greeting') or (f'사랑하는 제 {relation} {short}{particle} 사랑과 믿음으로 한 가정을 이루는 소중한 첫걸음을 내딛습니다.\n'
                                 '두 사람의 뜻 깊은 출발을 따뜻한 마음으로 축복해 주시면 더없는 기쁨이 되겠습니다')
    return '결혼식에 초대드립니다.',greeting,parents+' 올림',f'{parents}의 {relation} {name} 예식'


def composite(v,kind,photos):
    if kind=='memorial':
        import sys
        return erp_memorial_invitation.banner(sys.modules[__name__],v)
    boxes=[]
    if kind=='event':
        canvas=Image.open(ASSETS/'event-wreath-catalog.webp').convert('RGBA')
        for offset,sender in ((0,'쌀빛그림화환'),(840,signature(v,kind))):
            paste_photo(canvas,photos.get('logo'),(273+offset,719,616+offset,945))
            if not photos.get('logo'):
                boxes.append(fit_text(canvas,v['name'],(285+offset,760,602+offset,913),30,fill='#514329'))
            draw=ImageDraw.Draw(canvas);draw.rectangle((359+offset,952,616+offset,1009),fill='#d9c9b7')
            boxes.append(fit_text(canvas,v.get('congrats') or '뜻깊은 행사를\n진심으로 축하드립니다.',(365+offset,954,605+offset,1008),22,fill='#17140f',max_lines=2))
            draw.rectangle((276+offset,1040,615+offset,1184),fill='#d9c9b7')
            boxes.append(fit_text(canvas,sender,(283+offset,1050,608+offset,1177),46))
        return canvas,boxes
    base=Image.open(ASSETS/'rice-wreath-10kg.webp').convert('RGBA')
    canvas=Image.new('RGBA',(base.width*2,base.height),(0,0,0,0))
    for n,sender in enumerate(('쌀빛그림화환',signature(v,kind))):
        one=base.copy()
        if n==0:paste_photo(one,photos.get('frame'),(94,34,634,391))
        draw=ImageDraw.Draw(one);draw.rectangle((234,479,507,867),fill='#f4e3c9',outline='#cfb887',width=2)
        boxes.append(fit_text(one,'행사 후 쌀로 교환해 기부합니다.',(242,485,499,513),13,fill='#413923',bold=False,max_lines=1))
        paste_photo(one,photos.get('scrollLeft' if n==0 else 'scrollRight'),(247,521,494,681))
        boxes.append(fit_text(one,'쌀 10kg · 결혼을\n진심으로 축하드립니다.',(246,691,495,752),22,fill='#302b20',max_lines=2))
        boxes.append(fit_text(one,sender,(246,776,495,849),32))
        canvas.alpha_composite(one,(n*base.width,0))
    return canvas,boxes


def image_bytes(image,fmt='WEBP'):
    stream=io.BytesIO();image.save(stream,format=fmt,quality=91);return stream.getvalue()


def data_image(raw,mime='image/webp'):
    return 'data:'+mime+';base64,'+base64.b64encode(raw).decode()


def thumbnail(v,kind):
    if kind=='wedding':
        canvas=Image.new('RGB',(1200,630),'#eee7d7');draw=ImageDraw.Draw(canvas)
        draw.rectangle((55,43,1145,588),fill='#fffdf7',outline='#e3d4bb',width=2)
        draw.line((553,109,648,109),fill='#a58a5b',width=2)
        side='bride' if v.get('hostSide')=='bride' else 'groom'
        parents=v.get(side+'Parents','');name=v[side+'Name'];short=name[1:] if len(name)==3 else name
        parent_count=len([part for part in __import__('re').split(r'[·ㆍ,]',parents) if part.strip()])
        possessive='제' if parent_count==1 else '저희'
        tail='이' if short and 0xac00<=ord(short[-1])<=0xd7a3 and (ord(short[-1])-0xac00)%28 else ''
        fit_text(canvas,f"{possessive} {v[side+'Relation']} {short}{tail}의",(110,150,1090,243),78,fill='#24271f',bold=False,max_lines=1)
        fit_text(canvas,'결혼식에 초대드립니다.',(110,240,1090,338),78,fill='#24271f',bold=False,max_lines=1)
        fit_text(canvas,parents.replace('·',' ')+' 올림',(105,405,1095,534),104,fill='#24271f',bold=False,max_lines=1)
        return image_bytes(canvas,'JPEG')
    canvas=Image.new('RGB',(1200,630),'#eee6d7');draw=ImageDraw.Draw(canvas)
    draw.rectangle((56,39,1144,591),fill='#fffdf7',outline='#e3d4bb',width=2)
    title,_,sign,_=words(v,kind)
    if kind=='wedding':
        side='bride' if v.get('hostSide')=='bride' else 'groom';name=v[side+'Name'];short=name[1:] if len(name)==3 else name
        title=f"저희 {v[side+'Relation']} {short}{'이' if short and (ord(short[-1])-0xac00)%28 else ''}의\n결혼식에 초대드립니다."
    fit_text(canvas,title,(110,155,1090,367),64,fill='#24271f',bold=False,max_lines=3)
    fit_text(canvas,sign,(110,430,1090,540),82,fill='#24271f',max_lines=2)
    return image_bytes(canvas,'JPEG')



def date_words(v):
    when=dt.datetime.fromisoformat(v['date']+'T'+v['time']).replace(tzinfo=SEOUL)
    date=f"{when.year}년 {when.month}월 {when.day}일({'월화수목금토일'[when.weekday()]})"
    clock=f"{'오전' if when.hour<12 else '오후'} {when.hour%12 or 12}시"+(f' {when.minute}분' if when.minute else '')
    return when,date,clock


def product_name(v,kind):
    when,_,clock=date_words(v)
    name=f"{v['groomName']} {v['brideName']} 결혼" if kind=='wedding' else v['name']
    return f"{name} 축하 쌀빛그림화환 ({when.month}월 {when.day}일 {'월화수목금토일'[when.weekday()]} {clock} {v['venue']})"


def message(v,kind,price,url):
    if kind=='memorial':
        _,date,clock=date_words(v)
        return '\n\n'.join([v['name'],'삼가 고인의 명복을 빕니다.',f"빈소: {v['venue']} {v.get('funeralRoom','')}\n주소: {v['address']}\n발인: {date} {clock}",'상주: '+(v.get('mourners') or v.get('host') or '원본 확인'),url,f'쌀빛그림화환 안내 · {price:,}원','문의 031-8067-8580'])
    _,greeting,sign,_=words(v,kind);_,date,clock=date_words(v)
    hello=[line.strip() for line in greeting.splitlines() if line.strip()]
    greeting=(hello[0] if hello else '초대합니다.')+'\n'+(' '.join(hello[1:]) if len(hello)>1 else '귀한 걸음으로 함께해 주시면 감사하겠습니다.')
    lines=[greeting,f"🗓일시: {date} {clock}\n📍장소: {v['venue']}",sign,url,
           f'🌾 화환 안내 · {price:,}원\n'+('본 예식홀은 3단화환 반입 불가합니다. ' if v.get('wreathRestriction') else '')+'축하의 마음을 쌀빛그림화환으로 전해 주세요.',
           '행사 후 쌀로 교환해 도움이 필요한 곳에 기부하는 친환경 · ESG 화환입니다.','화환 문의 ☎ 031-8067-8580']
    import re
    return '\n\n'.join(re.sub(r'https?://[^\s]+','',line).strip() if line!=url else url for line in lines if line)


def page(v,kind,price,order_url,sample,hero,og,url,preview=False):
    if kind=='memorial':
        import sys
        return erp_memorial_invitation.page(sys.modules[__name__],v,price,order_url,sample,og,url,preview)
    if kind=='event':return event_page(v,price,order_url,sample,og,url,preview)
    if isinstance(sample,dict):
        import erp_invitation_auto
        return erp_invitation_auto.page(__import__(__name__),v,price,order_url,sample,hero,og,url)
    e=lambda s:html.escape(str(s),quote=True)
    title,greeting,sign,ogtitle=words(v,kind);when,date,clock=date_words(v)
    original=f'<a class="action original" href="{e(v["sourceUrl"])}">{"모바일 청첩장" if kind=="wedding" else "원본 초대장"} 보러가기</a>' if v.get('sourceUrl') else ''
    letter=''.join(f'<p>{e(p)}</p>' for p in greeting.splitlines() if p.strip())
    family=''.join(f'<p>{e(v[s+"Parents"])}의 {e(v[s+"Relation"])} <strong>{e(v[s+"Name"])}</strong></p>' for s in ('groom','bride')) if kind=='wedding' else ''
    image=f'<img class="portrait" src="{e(hero)}" alt="신랑신부 대표 사진">' if hero else ''
    details=f'<section class="details"><h2>{"예식" if kind=="wedding" else "행사"} 안내</h2><div class="family">{family}</div><dl><div><dt>일시</dt><dd><time datetime="{when.isoformat()}">{e(date)}<span class="clock">{e(clock)}</span></time></dd></div><div><dt>장소</dt><dd>{e(v["venue"])}<small>{e(v["address"])}</small></dd></div></dl><p class="note">{e(v.get("note", ""))}</p>{original}</section>'
    if kind=='wedding':
        opening=f'<section class="invitation"><article class="letter"><h1>{e(title)}</h1>{letter}<p class="signature">{e(sign)}</p></article></section>{image}{details}'
    else:
        opening=f'<header class="masthead">BELLAVITA</header><section class="event-invite"><h1>{e(title)}</h1><p class="host">{e(sign)}</p><div class="greeting">{letter}</div>{details}</section>'
    donation=('신랑신부를 위한 웨딩액자 설치 후 쌀로 교환하여 혼주님 명의로' if kind=='wedding' else '행사장에 축하 액자를 전시하고, 행사 후 쌀로 교환하여 주최 측 명의로')+' 도움이 필요한 곳에 쌀을 기부해드리는 친환경 기부화환입니다.'
    button=f'<span class="action">쌀빛그림화환 {"주문" if kind=="wedding" else "신청"}하기 · {price:,}원</span>'
    sample_tag=f'<img class="sample" src="{e(sample)}" alt="두 쌀빛그림화환 설치 예시">'
    # Wedding promotion is one link; event image opens a zoom dialog and CTA is a separate order link.
    if kind=='wedding': shop=f'<a class="shop" href="{e(order_url)}"><section><h2>쌀빛그림화환 안내</h2><p>{e(donation)}</p>{sample_tag}{button}</section></a>'
    else: shop=f'<section class="shop"><h2>쌀빛그림화환</h2><p>{e(donation)}</p><button class="sample-button" onclick="document.getElementById(\'zoom\').showModal()" aria-label="설치 예시 크게 보기">{sample_tag}</button><a class="order-link" href="{e(order_url)}">{button}</a></section>'
    if kind=='wedding':shop+='''<section style="padding:12px 20px"><button class="sample-button" onclick="document.getElementById('zoom').showModal()">설치 예시 크게 보기</button></section>'''
    zoom=f'<dialog id="zoom"><form method="dialog"><button>닫기</button></form><div class="zoom-scroll"><img src="{e(sample)}" alt="설치 예시 확대"></div></dialog>'
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{e(title)}</title><meta property="og:type" content="website"><meta property="og:url" content="{e(url)}"><meta property="og:title" content="{e(ogtitle)}"><meta property="og:description" content="{e(date+' '+clock)}"><meta property="og:image" content="{e(og)}"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630"><meta property="og:image:type" content="image/jpeg"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#e9e5db;color:#24271f;font:19px/1.8 system-ui,sans-serif}}main{{max-width:590px;margin:auto;background:#f9f7ef}}h1,h2,p{{margin:0}}h1,h2,.letter,.signature{{font-family:"Noto Serif KR",serif;word-break:keep-all;overflow-wrap:anywhere}}section{{padding:38px clamp(19px,6vw,39px);text-align:center}}h1{{font-size:clamp(25px,6vw,34px);line-height:1.4;margin-bottom:22px}}h2{{font-size:clamp(27px,6.5vw,36px);margin-bottom:22px}}.invitation{{background:#eee6d7;padding:clamp(20px,6vw,38px)}}.letter{{background:#fffdf7;border:1px solid #e3d4bb;text-align:left;padding:clamp(23px,6vw,42px)}}.letter p{{font-size:clamp(17px,4.7vw,22px);margin:0 0 17px;line-height:1.9}}.letter .signature{{font-size:clamp(24px,5.8vw,32px);text-align:right;font-weight:bold;padding-top:20px;margin:0}}.portrait{{width:100%;height:auto;display:block}}.family{{font-size:clamp(18px,4.8vw,23px);margin-bottom:24px;overflow-wrap:anywhere}}dl{{margin:0}}dl>div{{padding:14px 0;border-bottom:1px solid #e1dace}}dt{{font-size:17px;color:#62634f}}dd{{margin:0;font-size:clamp(22px,5.6vw,29px);font-family:serif}}dd small{{display:block;font:600 clamp(17px,4.6vw,21px)/1.7 system-ui}}.clock{{display:block;white-space:nowrap}}.action{{display:block;max-width:100%;margin:20px auto 0;padding:19px 15px;border-radius:7px;background:#304737;color:white;font-weight:700;font-size:clamp(17px,4.5vw,23px);line-height:1.5}}a{{color:inherit;text-decoration:none}}.shop{{display:block;background:#edf0e5}}.shop p{{font-size:clamp(16px,4.3vw,20px);line-height:1.95}}.sample{{display:block;width:100%;height:auto;margin:22px auto}}.sample-button{{padding:0;width:100%;border:0;background:none;cursor:zoom-in}}.note{{white-space:pre-line}}.event-invite{{background:#f6f1e7}}.event-invite .details{{padding:24px 0 0}}.event-invite .details h2{{display:none}}.host{{font-weight:bold;margin-bottom:20px}}.masthead{{padding:22px 30px;font:13px/1.5 serif;letter-spacing:.18em;border-bottom:1px solid #e1dace}}dialog{{max-width:96vw;max-height:95dvh;border:0;padding:15px}}.zoom-scroll{{max-width:90vw;overflow:auto}}.zoom-scroll img{{width:1720px;max-width:none;height:auto}}button{{font:inherit}}a:focus-visible,button:focus-visible{{outline:3px solid #a68b5d;outline-offset:4px}}
</style></head><body><main>{'<p style="text-align:center">미리보기 · 주문 연결 비활성</p>' if preview else ''}{opening}{shop}</main>{zoom}</body></html>'''


def event_page(v,price,order_url,sample,og,url,preview=False):
    """General event follows the approved v5 masthead, paper invite and product section."""
    e=lambda s:html.escape(str(s),quote=True)
    when,date,clock=date_words(v)
    css=(ASSETS/'event-v5.css').read_text()
    original=f'<a class="apply original" href="{e(v["sourceUrl"])}">원본 초대장 보러가기</a>' if v.get('sourceUrl') else ''
    greeting='<br>'.join(e(line) for line in (v.get('greeting') or '귀한 걸음으로 함께해 주시면 감사하겠습니다.').splitlines())
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{e(v['name'])}</title><meta property="og:type" content="website"><meta property="og:url" content="{e(url)}"><meta property="og:title" content="{e(v['name'])}"><meta property="og:description" content="{e(date+' '+clock)}"><meta property="og:image" content="{e(og)}"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630"><meta property="og:image:type" content="image/jpeg"><style>{css}
.clock{{display:block;white-space:nowrap}}.invite h1{{overflow-wrap:anywhere}}.note{{white-space:pre-line}}.original{{margin-top:20px}}.preview-note{{text-align:center;padding:10px;background:#fff3d8}}.apply{{text-decoration:none;text-align:center}}.event-address{{font-size:15px;line-height:1.7;margin-top:5px}}.sample-scroll img{{max-width:none}}
</style></head><body><div class="page"><header class="masthead"><p class="wordmark">BELLAVITA</p><span class="mast-note">행사 초대장</span></header>{'<p class="preview-note">미리보기 · 주문 연결 비활성</p>' if preview else ''}<main><section class="invite"><div class="invite-inner"><div class="seal" aria-hidden="true"><i></i><i></i><i></i></div><h1>{e(v['name'])}</h1><p class="eyebrow">{e(v['host'])}</p><p class="welcome">{greeting}</p><div class="event-info"><div><span>일시</span><time datetime="{when.isoformat()}">{e(date)}<span class="clock">{e(clock)}</span></time></div><div><span>장소</span><div><p>{e(v['venue'])}</p><p class="event-address">{e(v['address'])}</p></div></div></div><p class="note">{e(v.get('note',''))}</p>{original}</div></section><section class="products"><div class="section-intro"><span class="shortline" aria-hidden="true"></span><h2>쌀빛그림화환</h2><p>행사장에 축하 액자를 전시하고, 행사 후 쌀로 교환해 주최 측 명의로 기부합니다.</p></div><button class="sample-image" type="button" onclick="document.getElementById('sample-preview').showModal()" aria-label="액자화환 설치 예시 크게 보기" aria-haspopup="dialog"><img src="{e(sample)}" width="1720" height="1518" alt="덕유산 향적봉 일출과 해바라기 쌀빛그림화환 설치 예시"></button><a class="apply order-link" href="{e(order_url)}">쌀빛그림화환 신청하기 · {price:,}원</a></section></main><footer><span class="wordmark">BELLAVITA</span><a href="tel:03180678580">문의 031-8067-8580</a></footer></div><dialog class="sample-preview" id="sample-preview"><div class="dialog-body"><form method="dialog"><button class="close" aria-label="미리보기 닫기">×</button></form><h2>액자화환 설치 예시</h2><p class="swipe-note">좌우로 밀어 두 샘플을 확인할 수 있습니다.</p><div class="sample-scroll" tabindex="0"><img src="{e(sample)}" width="1720" height="1518" alt="설치 예시 확대"></div></div></dialog></body></html>'''
