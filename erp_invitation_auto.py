"""Shared approved wedding notice renderer, automatic scroll crops, and four-photo selection."""
import base64,html,io,json,os,re,hashlib,tempfile,threading
from pathlib import Path
from collections import OrderedDict
from PIL import Image,ImageDraw,ImageFont,ImageOps,ImageChops,ImageStat
import erp_wedding_photos as geometry
RENDER_VERSION='wedding-auto-scroll-photo-fallback-v7'
ROLES=('hero','frame','scrollLeft','scrollRight')
_VERDICTS=OrderedDict();_LOCK=threading.Lock()

def normalized(image):return ImageOps.exif_transpose(image).convert('RGB')
def photo_id(image):
    image=normalized(image);return hashlib.sha256(str(image.size).encode()+image.tobytes()).hexdigest()
def visual_id(image):
    p=list(normalized(image).convert('L').resize((9,8)).getdata())
    return sum((p[y*9+x]>p[y*9+x+1])<<(y*8+x) for y in range(8) for x in range(8))
def colored(image):
    r,g,b=normalized(image).resize((100,100)).split()
    return max(ImageStat.Stat(ImageChops.difference(r,g)).mean[0],ImageStat.Stat(ImageChops.difference(g,b)).mean[0])>1.0

def photo_check(image):
    key=photo_id(image)
    with _LOCK:
        if key in _VERDICTS:return _VERDICTS[key]
    if not colored(image):return False
    from openai import OpenAI
    thumb=normalized(image);thumb.thumbnail((900,900));stream=io.BytesIO();thumb.save(stream,'JPEG',quality=78)
    prompt=('Return JSON only: {"color":true,"groom_visible":true,"bride_visible":true}. '
            'Both actual wedding partners must appear together in this real color photograph. '
            'Reject solo portraits, monochrome, text, graphics, scenery-only images. Do not infer names.')
    response=OpenAI(timeout=40,max_retries=0).responses.create(model=os.getenv('TAESAN_MODEL','gpt-5-mini'),store=False,max_output_tokens=700,input=[{'role':'user','content':[{'type':'input_text','text':prompt},{'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(stream.getvalue()).decode(),'detail':'low'}]}])
    raw=response.output_text.strip();match=re.search(r'\{.*\}',raw,re.S);result=json.loads(match[0] if match else raw)
    ok=all(result.get(k) is True for k in ('color','groom_visible','bride_visible'))
    with _LOCK:
        _VERDICTS[key]=ok
        while len(_VERDICTS)>128:_VERDICTS.popitem(last=False)
    return ok

def select_photos(worker,values,checker=None,progress=None):
    checker=checker or photo_check;seen=set();hero_url='';first_readable='';local_verified=[];color_urls=[];reported=0
    if progress:progress(0,4)
    def report(done):
        nonlocal reported
        if progress and done>reported:
            progress(done,4);reported=done
    hero=values.get('hero','')
    urls=list(dict.fromkeys(([hero] if hero else [])+list(values.get('images',[]))))[:20]
    for url in urls:
        try:
            image=normalized(worker.image(url));identity=photo_id(image)
            if identity in seen:continue
            seen.add(identity)
            if not first_readable:first_readable=url
            # The parser's explicit hero is the card's main photograph. Keep
            # that original, but verify design candidates independently so a
            # logo or scenery hero cannot silently fill every production slot.
            if url==hero and hero:
                hero_url=url
                report(1)
            if not colored(image):continue
            color_urls.append(url)
            try:faces=geometry.detect_faces(image)
            except (ValueError,OSError):faces=[]
            if len(faces)==2:
                local_verified.append(url)
                if not hero and not hero_url:hero_url=url
                report(min(4,1+len({candidate for candidate in local_verified if candidate!=hero_url})))
            if hero_url and len([candidate for candidate in local_verified if candidate!=hero_url])>=3:break
            if not hero and len(local_verified)>=4:break
        except (ValueError,OSError):continue
    if not hero_url and (local_verified or first_readable):
        hero_url=local_verified[0] if local_verified else first_readable
        report(1)
    if not hero_url:
        return values,['컬러 신랑신부 커플 사진을 확인해 주세요.']
    design=[candidate for candidate in local_verified if candidate!=hero_url]
    if len(design)<3 and hero_url in local_verified:design.append(hero_url)
    fallback=False
    # A local two-face result is authoritative and fast. Only if none exists
    # may the remote couple checker inspect at most two originals, finding one
    # source which can be safely reused for all design roles.
    if not local_verified:
        candidates=list(dict.fromkeys([hero_url,*color_urls]))[:2]
        for url in candidates:
            try:approved=checker(normalized(worker.image(url)))
            except Exception:approved=False
            if approved:
                design=[url]
                break
    if not design:
        fallback=True;design=[hero_url]
    distinct_design=len(set(design))
    while len(design)<3:design.append(design[len(design)%distinct_design])
    chosen=[hero_url,*design[:3]]
    for done in range(2,5):report(done)
    values.update(dict(zip(ROLES,chosen)))
    warnings=[] if len(set(chosen))==4 else [
        ('제작용 커플 판정을 확인하지 못해 메인 원본을 '
         '자동족자 방식으로 안전 크롭했습니다.' if fallback else
         f'확인된 제작용 커플 원본이 {distinct_design}장이어서 액자·족자는 역할별 안전 크롭으로 구성했습니다.')]
    return values,warnings

def validate_photos(photos,checker=None):
    if any(k not in photos for k in ROLES):raise ValueError('사진 4장: 대표·액자·왼쪽 족자·오른쪽 족자를 모두 선택하세요.')
    for role in ROLES:
        im=photos[role]
        if not isinstance(im,Image.Image) or im.width<1 or im.height<1:
            raise ValueError('액자·족자에 사용할 사진 원본을 확인하세요.')

def banner_geometry(base,frame):
    w,h=base.size
    body=base.getchannel('A').point(lambda alpha:255 if alpha>128 else 0).crop((0,round(h*.45),w,round(h*.8))).getbbox()
    center=(body[0]+body[2])/2 if body else w/2
    # Original outer frame 550x371 in a716x1100 sample; rotate exactly, never reduce it.
    fw,fh=550*w/716,371*h/1100
    portrait=frame.height>frame.width
    extra=round(fw-fh) if portrait else 0
    height=h+extra
    if portrait:
        width,depth=fh,fw;left=center-width/2;top=28*h/1100;pad=6*w/716
        box=[left+pad,top+pad,width-2*pad,depth-2*pad]
    else:box=[w*.128,h*.026,w*.765,h*.333]
    return {'portrait':portrait,'width':w,'height':height,'baseHeight':h,'extra':extra,'center':center,'frame':box,'outer':[center-fh/2,28*h/1100,fh,fw] if portrait else None,'scroll':[w*.325,h*.435+extra,w*.385,h*.352]}

def portrait_base(base,g):
    if not g['portrait']:return base.copy()
    w,h=base.size;scale=2
    layer=Image.new('RGBA',(w*scale,g['height']*scale),(0,0,0,0));start=round(h*402/1100)
    body=base.crop((0,start,w,h));layer.alpha_composite(body.resize((w*scale,body.height*scale)),(0,(start+g['extra'])*scale))
    x,y,width,height=g['outer'];d=ImageDraw.Draw(layer)
    rect=(round(x*scale),round(y*scale),round((x+width)*scale)-1,round((y+height)*scale)-1);d.rectangle(rect,fill='#39352f');d.rectangle(tuple(v+(6 if i<2 else -6) for i,v in enumerate(rect)),fill='#827a69')
    return layer.resize((w,g['height']),Image.Resampling.LANCZOS)

def components(renderer,v,kind,photos,composite=None):
    if kind!='wedding':return None
    validate_photos(photos)
    from wedding_scroll import render as scroll
    base=Image.open(renderer.ASSETS/'rice-wreath-10kg.webp').convert('RGBA');g=banner_geometry(base,normalized(photos['frame']))
    frame=geometry.automatic_crop(photos['frame'],tuple(round(x) for x in g['frame'][2:]))
    def data(image):return renderer.data_image(renderer.image_bytes(image))
    def make(photo,sender,centering):
        safe=geometry.automatic_crop(photo,scroll.wedding_photo_size('A3'),centering=centering)
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);safe.save(folder/'photo.jpg',quality=98)
            scroll.make_scroll(folder,sender,wedding_photo=folder/'photo.jpg',message='결혼을 진심으로 축하 드리며 함께 하는 길이\n언제나 행복하고 기쁨이 가득하길 기원합니다')
            return data(Image.open(folder/'scroll.png').convert('RGB'))
    return {'BASE':data(base),'BASE_LEFT':data(portrait_base(base,g)),'FRAME':data(frame),'SCROLL_LEFT':make(photos['scrollLeft'],'쌀빛그림화환',(.42,.2)),'SCROLL_RIGHT':make(photos['scrollRight'],renderer.signature(v,'wedding'),(.58,.2)),'_geometry':g,'renderVersion':RENDER_VERSION}

def geometry_css(parts):
    g=parts.get('_geometry')
    rule='.wreath-promotion .samples{align-items:end}'
    if not g:return rule
    x,y,w,h=g['frame'];sx,sy,sw,sh=g['scroll'];W,H=g['width'],g['height']
    rule+=f'.wreath-promotion .samples figure:first-child .frame-photo{{left:{x/W*100:.8f}%;top:{y/H*100:.8f}%;width:{w/W*100:.8f}%;height:{h/H*100:.8f}%;object-fit:contain}}'
    rule+=f'.wreath-promotion .samples figure:first-child .scroll-render{{left:{sx/W*100:.8f}%;top:{sy/H*100:.8f}%;width:{sw/W*100:.8f}%;height:{sh/H*100:.8f}%;object-fit:contain}}'
    return rule

def preview_composite(renderer,parts):
    def read(value):return Image.open(io.BytesIO(base64.b64decode(value.split(',',1)[1]))).convert('RGBA')
    base=read(parts['BASE']);left=read(parts.get('BASE_LEFT',parts['BASE']));width=base.width;height=max(base.height,left.height)
    canvas=Image.new('RGBA',(width*2,height),(0,0,0,0));g=parts.get('_geometry') or {'frame':[width*.128,base.height*.026,width*.765,base.height*.333],'scroll':[width*.325,base.height*.435,width*.385,base.height*.352]}
    for n,key in enumerate(('SCROLL_LEFT','SCROLL_RIGHT')):
        tile=left.copy() if n==0 else base.copy()
        if n==0:
            x,y,w,h=g['frame'];image=read(parts['FRAME']);tile.alpha_composite(image.resize((round(w),round(h)),Image.Resampling.LANCZOS),(round(x),round(y)))
            x,y,w,h=g['scroll']
        else:x,y,w,h=width*.325,base.height*.435,width*.385,base.height*.352
        image=read(parts[key]);tile.alpha_composite(image.resize((round(w),round(h)),Image.Resampling.LANCZOS),(round(x),round(y)))
        canvas.alpha_composite(tile,(n*width,height-tile.height))
    return canvas,[]

def page(renderer,v,price,order_url,parts,hero,og,url):
    template=(Path(__file__).parent/'erp_invitation_assets/wedding-reference.html').read_text(encoding='utf-8')
    title,greeting,signature,ogtitle=renderer.words(v,'wedding');_,date,clock=renderer.date_words(v)
    families=''.join('<p>'+html.escape(v[s+'Parents'])+'의 '+html.escape(v[s+'Relation'])+' <strong>'+html.escape(v[s+'Name'])+'</strong></p>' for s in ('groom','bride'))
    greeting=''.join('<p>'+html.escape(line)+'</p>' for line in greeting.splitlines() if line.strip())
    values={'TITLE':v['groomName']+'·'+v['brideName']+' 결혼식 안내','OG_TITLE':ogtitle,'OG_DESCRIPTION':date+' '+clock,'URL':url,'OG_IMAGE':og,'SIGNATURE':signature,'HERO':hero,'DATE':date,'CLOCK':clock,'VENUE':v['venue'],'ADDRESS':v['address'],'SOURCE_URL':v['sourceUrl'],'ORDER_URL':order_url,**parts}
    values.setdefault('BASE_LEFT',parts['BASE'])
    # Escape all data once; trusted structure slots are generated above from escaped fields.
    result=re.sub(r'\{\{([A-Z_]+)\}\}',lambda m:greeting if m[1]=='GREETING' else families if m[1]=='FAMILIES' else html.escape(str(values[m[1]]),quote=True),template)
    return result.replace('</style>',geometry_css(parts)+'</style>')
