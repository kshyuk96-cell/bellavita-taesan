"""Reuse approved invitation layouts and select source photos for one-click ERP creation."""
import base64
import html
import io
import json
import os
import re
from pathlib import Path
from PIL import ImageOps


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


def components(renderer,v,kind,photos,composite):
    if kind!='wedding':return None
    # Keep the reference's separate base/frame/scroll layers and positions.
    width=composite.width//2
    left=composite.crop((234,479,507,867))
    right=composite.crop((width+234,479,width+507,867))
    return {'BASE':renderer.data_image((renderer.ASSETS/'rice-wreath-10kg.webp').read_bytes()),
            'FRAME':renderer.data_image(renderer.image_bytes(photos['frame'])),
            'SCROLL_LEFT':renderer.data_image(renderer.image_bytes(left)),
            'SCROLL_RIGHT':renderer.data_image(renderer.image_bytes(right))}


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
