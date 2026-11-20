"""Approved funeral backing, catalogue frames and real A3 general/rice scrolls."""
import base64
import hashlib
import html
import importlib.util
import io
import re
import sys
import tempfile
import threading
from pathlib import Path
from PIL import Image, ImageOps

RENDER_VERSION='memorial-approved-backing-birch23-stars44-name-only-v5'
ASSETS=Path(__file__).with_name('erp_invitation_assets')
SCROLL_ASSETS=Path(__file__).with_name('wedding_scroll')/'assets'
HY_GUNGSEO_SHA256='b41cef3386d034040704b1a143ef13a63c5b9f1ec4222a7579c1796ea05517f6'
CONDOLENCE='하늘의 별이 되신 고인을 추모하며\n유가족 분들께 깊은 위로의 마음을 전합니다'
LEFT_SENDER='쌀빛그림화환\n임직원 일동'
RIGHT_NAME_SCALE=1.5
# Preserve the approved landscape frame/easel geometry.  The actual memorial
# backing is 1124x1690; its white A3 opening is x134..984/y319..1509.
FRAME_BOX=(.128,.026,.765,.333)
BACKING_BOX=(184,405,370,round(370*1690/1124))
SCROLL_BOX=((184+134*370/1124)/716,(405+319*370/1124)/1100,
            (984-134)*370/1124/716,(1509-319)*370/1124/1100)
_ENGINE=None
_ENGINE_LOCK=threading.RLock()


def scroll_engine():
    """Keep the approved funeral font isolated from simultaneous wedding jobs."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            import wedding_scroll
            name='wedding_scroll._approved_memorial_engine'
            spec=importlib.util.spec_from_file_location(name,Path(wedding_scroll.__file__).with_name('render.py'))
            engine=importlib.util.module_from_spec(spec)
            sys.modules[name]=engine
            spec.loader.exec_module(engine)
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            font=SCROLL_ASSETS/'hy-gungseo.ttf'
            if hashlib.sha256(font.read_bytes()).hexdigest()!=HY_GUNGSEO_SHA256:
                raise ValueError('확정된 HY궁서 글꼴 파일을 확인하세요.')
            face='BellavitaApprovedMemorialHY'
            if face not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(face,str(font)))
            engine.FONT_NAME=face
            engine.ASSETS=SCROLL_ASSETS
            original_write=engine.write_text
            def invitation_sender(page,text,box,maximum,color,sender=False,**kwargs):
                if sender and text==LEFT_SENDER:
                    # This invitation's approved two-line identity is explicit;
                    # the shared production parser still handles all other names.
                    lines=LEFT_SENDER.splitlines();leading=1.16
                    points=min(maximum,box.width/max(pdfmetrics.stringWidth(line,face,1) for line in lines),box.height/(len(lines)*leading))
                    engine.draw_text_rows(page,box,color,[[(line,points)] for line in lines],leading,'',center_ink=True)
                    return
                return original_write(page,text,box,maximum,color,sender=sender,**kwargs)
            engine.write_text=invitation_sender
            _ENGINE=engine
        return _ENGINE


def scroll_sender_name(host):
    from erp_memorial_invitation import normalize_mourner_name
    source=str(host or '').strip()
    # 상주 can be the given name itself; do not turn 김상주 into 김.
    without_courtesy=re.sub(r'\s*상주님\s*$','',source)
    if re.fullmatch(r'[가-힣]상주',without_courtesy):return without_courtesy
    return normalize_mourner_name(source)


def render_scrolls(output,sender):
    """Use the very same v21 production functions, with the rollback respected."""
    sender=scroll_sender_name(sender)
    if not sender:raise ValueError('오른쪽 쌀빛 근조 족자에 넣을 이름을 입력하세요.')
    engine=scroll_engine();root=Path(output);result={}
    for side,name,layout in [('left',LEFT_SENDER,'general'),('right',sender,'rice')]:
        folder=root/side
        engine.make_scroll(folder,name,CONDOLENCE,kind='memorial',paper='A3',layout=layout,rice_kg=10,name_scale=RIGHT_NAME_SCALE if side=='right' else 1)
        result[side]=folder
    return result


def components(renderer,v):
    sender=str(v.get('host') or '').strip()
    if not sender:
        prefix=str(v.get('productPrefix') or '').strip()
        sender=re.sub(r'\s+(?:빙모상|빙부상|모친상|부친상|조모상|조부상|외조모상|외조부상|배우자상|부군상|형제상|자매상)$','',prefix) if prefix else ''
        if sender==prefix:sender=''
    if not sender:raise ValueError('상주 이름·호칭을 확인하세요.')
    # Bundled from the approved easel and the user's exact memorial.png backing.
    # A funeral render must not depend on a persistent wedding-asset directory
    # being present, or silently fall back to the celebration backing.
    base=Image.open(ASSETS/'memorial-easel-base.webp').convert('RGBA')
    size=(round(base.width*FRAME_BOX[2]),round(base.height*FRAME_BOX[3]))
    stars=[]
    for name in ['memorial-birch-left.jpg','memorial-frame-right.jpg']:
        with Image.open(ASSETS/name) as source:
            stars.append(ImageOps.fit(ImageOps.exif_transpose(source).convert('RGB'),size,Image.Resampling.LANCZOS))
    if hashlib.sha256(stars[0].tobytes()).digest()==hashlib.sha256(stars[1].tobytes()).digest():
        raise ValueError('서로 다른 두 작품을 확인하세요.')
    data=lambda image:renderer.data_image(renderer.image_bytes(image))
    with tempfile.TemporaryDirectory() as temp:
        scrolls=render_scrolls(temp,sender)
        scroll_images={side:Image.open(path/'scroll.png').convert('RGB') for side,path in scrolls.items()}
    return {'BASE':data(base),'FRAME_LEFT':data(stars[0]),'FRAME_RIGHT':data(stars[1]),
            'SCROLL_LEFT':data(scroll_images['left']),'SCROLL_RIGHT':data(scroll_images['right']),
            'renderVersion':RENDER_VERSION,
            'senderLeft':LEFT_SENDER,'senderRight':scroll_sender_name(sender),
            'senderLayout':{'leftLines':LEFT_SENDER.splitlines(),'rightNameScale':RIGHT_NAME_SCALE},
            'frameOriginals':['23 인제 원대리 자작나무숲 5','44 설악산 국립공원 울산바위 은하수'],
            'backingOriginal':'easel.zip/memorial.png'}


def preview_composite(renderer,parts):
    def read(value):return Image.open(io.BytesIO(base64.b64decode(value.split(',',1)[1]))).convert('RGBA')
    base=read(parts['BASE']);width,height=base.size
    canvas=Image.new('RGBA',(width*2,height),(0,0,0,0))
    for n,side in enumerate(['LEFT','RIGHT']):
        tile=base.copy()
        for key,box in [('FRAME_'+side,FRAME_BOX),('SCROLL_'+side,SCROLL_BOX)]:
            x,y,w,h=box;image=read(parts[key]);image=image.resize((round(width*w),round(height*h)),Image.Resampling.LANCZOS)
            tile.alpha_composite(image,(round(width*x),round(height*y)))
        canvas.alpha_composite(tile,(n*width,0))
    return canvas,[]


# Scoped to the promotional block: obituary details keep their existing format.
PROMOTION_CSS='''
.memorial-promotion{display:block;color:inherit;text-decoration:none}.memorial-promotion:focus-visible{outline:3px solid #877147;outline-offset:-4px}.memorial-promotion .wreath{background:#edf0e5;padding:37px clamp(22px,7vw,48px);text-align:center;border-top:1px solid #e1dace}.memorial-promotion h2{font-family:"Noto Serif KR",serif;font-size:clamp(24px,6vw,34px);font-weight:500;margin:0 0 23px}.memorial-promotion .wreath-letter{font-family:"Noto Serif KR",serif;font-size:clamp(12px,3.25vw,16px);line-height:1.95;color:#303a2e;word-break:keep-all;overflow-wrap:break-word}.memorial-promotion .wreath-letter span{display:block}.memorial-promotion .wreath-letter p{margin:0;white-space:normal}.memorial-promotion .wreath-letter p+p{margin-top:14px}.memorial-promotion .samples{display:grid;grid-template-columns:1fr 1fr;gap:clamp(4px,3vw,18px);margin-top:24px;align-items:end}.memorial-promotion figure{width:100%;margin:0}.memorial-promotion .product{position:relative}.memorial-promotion .base{display:block;width:100%;height:auto}.memorial-promotion .frame-photo{position:absolute;left:12.8%;top:2.6%;width:76.5%;height:33.3%;object-fit:cover}.memorial-promotion .scroll-render{position:absolute;left:31.85898328%;top:46.36444516%;width:39.07880872%;height:35.61145260%;object-fit:fill}.memorial-promotion .order-action{display:table;min-height:43px;padding:8px 19px;margin:17px auto 0;border:1px solid #304737;border-radius:4px;background:#304737;color:white;font:600 14px/1.45 "Noto Sans KR",system-ui,sans-serif}.memorial-promotion:hover .order-action{background:#435d49}
'''


def promotional_html(parts,order_url,preview=False):
    e=lambda value:html.escape(str(value),quote=True)
    start='<div class="wreath-promotion memorial-promotion">' if preview else '<a class="wreath-promotion memorial-promotion" href="'+e(order_url)+'" target="_blank" rel="noopener noreferrer" aria-label="쌀빛그림화환 주문하기">'
    end='</div>' if preview else '</a>'
    samples=''.join('<figure><div class="product"><img class="base" src="'+e(parts['BASE'])+'" alt=""><img class="frame-photo" src="'+e(parts['FRAME_'+side])+'" alt="'+('23번 자작나무 작품 액자' if side=='LEFT' else '44번 설악산 국립공원 울산바위 은하수 작품 액자')+'"><img class="scroll-render" src="'+e(parts['SCROLL_'+side])+'" alt="'+('일반 근조 A3 족자 · 쌀빛그림화환' if side=='LEFT' else '쌀빛 10kg 근조 A3 족자 · '+e(parts['senderRight']))+'"></div></figure>' for side in ['LEFT','RIGHT'])
    return start+'<section class="wreath"><h2>쌀빛그림화환 안내</h2><div class="wreath-letter"><p><span>쌀빛그림화환은 고인을 추모하는 액자 설치 후</span><span>쌀로 교환하여 상주님 명의로 도움이 필요한 곳에</span><span>쌀을 기부해드리는 친환경 기부화환입니다.</span></p><p><span>화환으로 조문의 마음을 전하고자 하시는 분께서는</span><span>쌀빛그림화환으로 함께해 주시면 감사하겠습니다.</span></p></div><div class="samples">'+samples+'</div><span class="action order-action">쌀빛그림화환 주문하기</span></section>'+end


def page(renderer,v,price,order_url,parts,og,url,preview=False):
    import erp_memorial_page
    # Keep the association template's complete obituary and selected account.
    source=erp_memorial_page.page(renderer,v,price,order_url,'',og,url,preview)
    promotion=promotional_html(parts,order_url,preview)
    source,count=re.subn(r'<section class="products">.*?</section>',lambda _:promotion,source,count=1,flags=re.S)
    if count!=1:raise ValueError('부고 안내 아래의 쌀빛그림화환 영역을 확인하세요.')
    font_link='<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;700&amp;family=Noto+Serif+KR:wght@400;500;600&amp;display=swap">'
    return source.replace('</style>',PROMOTION_CSS+'</style>').replace('</head>',font_link+'</head>')
