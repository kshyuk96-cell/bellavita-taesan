"""Reuse approved invitation layouts and select source photos for one-click ERP creation."""
import base64
import html
import io
import json
import os
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps


def photo_check(image):
    from openai import OpenAI
    thumb=ImageOps.exif_transpose(image).convert('RGB');thumb.thumbnail((900,900));stream=io.BytesIO();thumb.save(stream,'JPEG',quality=78)
    prompt=('Return JSON only: {"color":true,"groom_visible":true,"bride_visible":true}. '
            'Inspect this wedding photo. Both actual groom and bride must be clearly visible together. '
            'Reject monochrome, text-only artwork, logos, scenery-only images and photos of only one person. '
            'Do not infer names or other personal data.')
    response=OpenAI(timeout=45,max_retries=1).responses.create(model=os.getenv('TAESAN_MODEL','gpt-5-mini'),store=False,max_output_tokens=700,input=[{'role':'user','content':[{'type':'input_text','text':prompt},{'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(stream.getvalue()).decode(),'detail':'low'}]}])
    raw=response.output_text.strip();match=re.search(r'\{.*\}',raw,re.S)
    result=json.loads(match[0] if match else raw)
    return all(result.get(k) is True for k in ('color','groom_visible','bride_visible'))


def select_photos(worker,values,checker=photo_check):
    chosen=[];seen=set()
    for url in list(dict.fromkeys(values.get('images',[])))[:16]:
        try:
            image=worker.image(url)
            # Exact pixel duplicates from different image URLs count once.
            small=ImageOps.exif_transpose(image).convert('RGB');small.thumbnail((160,160))
            import hashlib
            fingerprint=hashlib.sha256(small.tobytes()).hexdigest()
            if fingerprint in seen:continue
            seen.add(fingerprint)
            if checker(image):chosen.append(url)
            if len(chosen)==3:break
        except (ValueError,OSError):continue
    if len(chosen)<3:
        values['images']=chosen
        return values,['컬러 신랑신부 사진 3장 확인 필요']
    values.update(hero=chosen[0],frame=chosen[0],scrollLeft=chosen[1],scrollRight=chosen[2])
    return values,[]


def components(renderer,v,kind,photos,composite=None):
    if kind!='wedding':return None
    font_path=renderer.ASSETS/'seoul.ttf'
    def font(size):return ImageFont.truetype(str(font_path),size) if font_path.is_file() else renderer.font(size,True)
    def scroll(photo,sender):
        canvas=Image.new('RGB',(660,934),'#f2e9da')
        picture=ImageOps.fit(ImageOps.exif_transpose(photo).convert('RGB'),(660,452),Image.Resampling.LANCZOS,centering=(.5,.2))
        canvas.paste(picture,(0,0));draw=ImageDraw.Draw(canvas)
        for line,y in [('결혼을 진심으로 축하 드리며 함께 하는 길이',505),('언제나 행복하고 기쁨이 가득하길 기원합니다',541)]:
            draw.text((330,y),line,font=font(30),fill='#111111',anchor='mt')
        draw.line((78,578,582,578),fill='#d2bd91',width=1)
        for size in range(88,15,-1):
            face=font(size)
            if draw.textlength(sender,font=face)<=590:break
        draw.text((330,715),sender,font=face,fill='#192846',anchor='mt')
        return renderer.data_image(renderer.image_bytes(canvas))
    return {'BASE':renderer.data_image((renderer.ASSETS/'rice-wreath-10kg.webp').read_bytes()),
            'FRAME':renderer.data_image(renderer.image_bytes(photos['frame'])),
            'SCROLL_LEFT':scroll(photos['scrollLeft'],'쌀빛그림화환'),
            'SCROLL_RIGHT':scroll(photos['scrollRight'],renderer.signature(v,'wedding'))}


def preview_composite(renderer,parts):
    def read(value):return Image.open(io.BytesIO(base64.b64decode(value.split(',',1)[1]))).convert('RGBA')
    base=read(parts['BASE']);width,height=base.size;canvas=Image.new('RGBA',(width*2,height),(0,0,0,0))
    for n,key in enumerate(('SCROLL_LEFT','SCROLL_RIGHT')):
        one=base.copy()
        if n==0:
            box=(round(width*.128),round(height*.026),round(width*.765),round(height*.333))
            one.alpha_composite(ImageOps.fit(read(parts['FRAME']),box[2:],Image.Resampling.LANCZOS,centering=(.5,.2)),box[:2])
        image=read(parts[key]);image=ImageOps.fit(image,(round(width*.385),round(height*.352)),Image.Resampling.LANCZOS)
        one.alpha_composite(image,(round(width*.325),round(height*.435)))
        canvas.alpha_composite(one,(n*width,0))
    return canvas,[]


def page(renderer,v,price,order_url,parts,hero,og,url):
    template=(Path(__file__).parent/'erp_invitation_assets/wedding-reference.html').read_text(encoding='utf-8')
    title,greeting,signature,ogtitle=renderer.words(v,'wedding');_,date,clock=renderer.date_words(v)
    if not v.get('greeting'):
        side='bride' if v.get('hostSide')=='bride' else 'groom';name=v[side+'Name'];short=name[1:] if len(name)==3 else name
        final=bool((ord(short[-1])-0xac00)%28) if short and 0xac00<=ord(short[-1])<=0xd7a3 else False
        greeting=f"사랑하는 저희 {v[side+'Relation']} {short}{'이가' if final else '가'} 사랑과 믿음으로 한 가정을 이루는 소중한 첫걸음을 내딛습니다.\n두 사람의 뜻 깊은 출발을 따뜻한 마음으로 축복해 주시면 더없는 기쁨이 되겠습니다."
    families=''.join('<p>'+html.escape(v[s+'Parents'])+'의 '+html.escape(v[s+'Relation'])+' <strong>'+html.escape(v[s+'Name'])+'</strong></p>' for s in ('groom','bride'))
    greeting=''.join('<p>'+html.escape(line)+'</p>' for line in greeting.splitlines() if line.strip())
    values={'TITLE':v['groomName']+'·'+v['brideName']+' 결혼식 안내','OG_TITLE':ogtitle,'OG_DESCRIPTION':date+' '+clock,'URL':url,'OG_IMAGE':og,'SIGNATURE':signature,'HERO':hero,'DATE':date,'CLOCK':clock,'VENUE':v['venue'],'ADDRESS':v['address'],'SOURCE_URL':v['sourceUrl'],'ORDER_URL':order_url,**parts}
    # Escape all data once; trusted structure slots are generated above from escaped fields.
    return re.sub(r'\{\{([A-Z_]+)\}\}',lambda m:greeting if m[1]=='GREETING' else families if m[1]=='FAMILIES' else html.escape(str(values[m[1]]),quote=True),template)
