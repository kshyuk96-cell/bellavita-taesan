import base64,io,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import fitz
import erp_invitation_render as renderer
import erp_memorial_auto as memorial

class MemorialApprovedSamples(unittest.TestCase):
 def test_actual_general_and_rice_a3_preserve_senders_font_and_rollback(self):
  from wedding_scroll import render as wedding
  prior=wedding.FONT_NAME
  with tempfile.TemporaryDirectory() as tmp:
   results=memorial.render_scrolls(tmp,'김병섭 상주님')
   sizes={};texts={}
   for side in ['left','right']:
    with fitz.open(results[side]/'scroll.pdf') as doc:
     self.assertEqual(len(doc),1);self.assertAlmostEqual(doc[0].rect.height/doc[0].rect.width,2**.5,places=2)
     text=doc[0].get_text();self.assertNotIn('화환 보내신 분',text)
     self.assertIn('쌀빛그림화환' if side=='left' else '김병섭',text)
     self.assertNotIn('상주님',doc[0].get_textbox(memorial.scroll_engine().memorial_sender_box(doc[0])))
     self.assertTrue(any('HYgsrB' in f[3] for f in doc[0].get_fonts()))
     box=memorial.scroll_engine().memorial_sender_box(doc[0]);texts[side]=doc[0].get_textbox(box)
     spans=[span for block in doc[0].get_text('dict')['blocks'] if 'lines' in block for line in block['lines'] for span in line['spans'] if fitz.Rect(span['bbox']).intersects(box)]
     sizes[side]=max(span['size'] for span in spans)
     bounds=fitz.Rect(spans[0]['bbox'])
     for span in spans[1:]:bounds|=fitz.Rect(span['bbox'])
     self.assertGreaterEqual(bounds.x0,box.x0-.1);self.assertLessEqual(bounds.x1,box.x1+.1)
     self.assertLess(abs((bounds.y0-box.y0)-(box.y1-bounds.y1)),4)
   self.assertEqual(texts['left'].strip(),'쌀빛그림화환\n임직원 일동')
   self.assertGreater(sizes['right'],sizes['left']*1.8)
   self.assertGreater(sizes['right'],200)
   self.assertEqual(wedding.FONT_NAME,prior)
  self.assertEqual(memorial.scroll_sender_name('정명천 회장님'),'정명천 회장')
  self.assertEqual(memorial.scroll_sender_name('김병섭상주님'),'김병섭')
  self.assertEqual(memorial.scroll_sender_name('김상주'),'김상주')

 def test_birch23_and_star44_with_original_memorial_backing_and_distinct_scrolls(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(renderer,'ASSETS',Path(tmp)):
   parts=memorial.components(renderer,{'host':'김병섭 상주님'})
   self.assertNotEqual(parts['FRAME_LEFT'],parts['FRAME_RIGHT'])
   self.assertNotEqual(parts['SCROLL_LEFT'],parts['SCROLL_RIGHT'])
   self.assertEqual(parts['senderLeft'],'쌀빛그림화환\n임직원 일동');self.assertEqual(parts['senderRight'],'김병섭')
   self.assertEqual(parts['frameOriginals'],['23 인제 원대리 자작나무숲 5','44 설악산 국립공원 울산바위 은하수'])
   self.assertEqual(parts['backingOriginal'],'easel.zip/memorial.png')
   base=Image.open(io.BytesIO(base64.b64decode(parts['BASE'].split(',',1)[1]))).convert('RGB')
   # The real memorial sheet's dark top is present where celebration text was.
   self.assertLess(max(base.getpixel((369,451))),70)
   canvas,_=memorial.preview_composite(renderer,parts)
   self.assertEqual(canvas.mode,'RGBA');self.assertEqual(canvas.size,(1432,1100));self.assertEqual(canvas.getpixel((0,0))[3],0)

 def test_promotion_follows_obituary_and_has_two_real_frames_and_order_target(self):
  v={'name':'故 가상고인 부고','deceasedName':'가상고인','date':'2099-11-21','time':'09:30','venue':'가상장례식장','address':'가상로 1','host':'김병섭 상주님','mourners':'김병섭','funeralRoom':'특1호실','sourceUrl':'https://source.test/funeral'}
  parts={k:'data:image/webp;base64,eA==' for k in ['BASE','FRAME_LEFT','FRAME_RIGHT','SCROLL_LEFT','SCROLL_RIGHT']};parts['senderRight']='김병섭'
  page=memorial.page(renderer,v,93500,'https://order.test/93500',parts,'https://notice.test/og','https://notice.test/93500')
  self.assertLess(page.index('장례 안내'),page.index('wreath-promotion memorial-promotion'))
  self.assertEqual(page.count('class="frame-photo"'),2);self.assertEqual(page.count('class="scroll-render"'),2)
  self.assertIn('href="https://order.test/93500"',page);self.assertIn('align-items:end',page)
  self.assertNotIn('웨딩',page);self.assertNotIn('신랑신부',page);self.assertNotIn('결혼',page)
  preview=memorial.page(renderer,v,93500,'https://order.test/93500',parts,'https://notice.test/og','https://notice.test/93500',True)
  self.assertNotIn('href="https://order.test/93500"',preview)


 def test_engine_prepare_and_public_page_use_actual_memorial_components(self):
  import json
  from types import SimpleNamespace
  import erp_invitations as bridge
  v={'name':'故 가상고인 부고','deceasedName':'가상고인','date':'2099-11-21','time':'09:30','venue':'가상장례식장','address':'가상로 1','host':'김병섭 상주님','productPrefix':'김병섭 상주님 모친상','mourners':'김병섭','funeralRoom':'특1호실','sourceUrl':'https://source.test/funeral'}
  with tempfile.TemporaryDirectory() as tmp,patch.object(renderer,'ASSETS',Path(tmp)),patch('erp_local_checkout.build',return_value=('fixture',[{'price':p,'productId':str(p),'orderUrl':'https://order.test/'+str(p),'verified':True} for p in [93500,99000]],{})):
   engine=SimpleNamespace(progress=lambda *a,**k:None,checkout_root=Path(tmp),origin='https://notice.test',image=lambda url:None)
   result,assets=bridge.Engine.prepare(engine,{'requestId':'memorial-fixture-render','values':v,'kind':'memorial','orderMode':'invitation-checkout'})
   self.assertEqual(result['status'],'done');parts=json.loads(assets['reference-components.json'])
   self.assertEqual(parts['renderVersion'],memorial.RENDER_VERSION);self.assertEqual(parts['senderRight'],'김병섭')
   self.assertEqual(assets['hero.webp'],b'')
   page=renderer.page(v,'memorial',93500,'https://order.test/93500',parts,'','https://notice.test/og','https://notice.test/93500')
   self.assertEqual(page.count('class="frame-photo"'),2);self.assertIn('일반 근조 A3 족자',page);self.assertIn('쌀빛 10kg 근조 A3 족자',page)

if __name__=='__main__':unittest.main()
