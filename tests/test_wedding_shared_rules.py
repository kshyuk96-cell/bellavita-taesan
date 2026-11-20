import unittest,io,base64,json,tempfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
import erp_invitation_auto as auto
import erp_invitation_render as render
import erp_wedding_photos as geometry
class SharedRules(unittest.TestCase):
 def fixture(self):
  base=Image.new('RGBA',(716,1100),(0,0,0,0));ImageDraw.Draw(base).rectangle((185,410,552,930),fill='tan');return base
 def test_portrait_is_rotation_not_shrink_and_body_center_is_measured(self):
  base=self.fixture();base.putpixel((80,500),(255,255,255,2));g=auto.banner_geometry(base,Image.new('RGB',(1440,1920)))
  self.assertEqual(g['outer'][2:],[371,550]);self.assertEqual(g['height'],1279);self.assertEqual(g['center'],369)
  self.assertAlmostEqual(g['outer'][0]+g['outer'][2]/2,g['center'])
  im=auto.portrait_base(self.fixture(),g);self.assertEqual(im.size,(716,1279));self.assertEqual(im.getpixel((0,0))[3],0)
 def test_landscape_does_not_extend_banner(self):
  g=auto.banner_geometry(self.fixture(),Image.new('RGB',(1900,1200)));self.assertEqual(g['height'],1100);self.assertEqual(g['extra'],0)
 def test_full_bleed_crop_contains_both_faces_with_no_letterbox(self):
  im=Image.new('RGB',(1000,1600),'red');faces=[(100,100,300,400),(600,250,850,550)]
  with patch.object(geometry,'detect_faces',return_value=faces):result=geometry.crop(im,(600,400))
  self.assertEqual(result.size,(600,400));self.assertEqual(result.getpixel((0,0)),(255,0,0))
  b=geometry.face_crop_bounds(im.size,(600,400),faces)
  for f in faces:self.assertLessEqual(b[0],f[0]);self.assertLessEqual(b[1],f[1]);self.assertGreaterEqual(b[2],f[2]);self.assertGreaterEqual(b[3],f[3])
 def test_impossible_face_crop_is_rejected_instead_of_clipping(self):
  with patch.object(geometry,'detect_faces',return_value=[(20,20,200,300),(200,1400,350,1550)]):
   with self.assertRaisesRegex(ValueError,'두 얼굴을 보존'):geometry.crop(Image.new('RGB',(1000,1600)),(600,400))
 def test_solo_rejected_for_scroll(self):
  with patch.object(geometry,'detect_faces',return_value=[]):
   with self.assertRaisesRegex(ValueError,'신랑신부 두 얼굴'):geometry.crop(Image.new('RGB',(600,800)),(600,400))
 def test_same_photo_under_different_urls_is_rejected(self):
  photos={k:Image.new('RGB',(600,800),'red') for k in auto.ROLES}
  with self.assertRaisesRegex(ValueError,'사진 중복'):auto.validate_photos(photos,checker=lambda im:True)
 def test_shared_photo_geometry_is_bottom_aligned_and_transparency_survives(self):
  g=auto.banner_geometry(self.fixture(),Image.new('RGB',(1400,1900)))
  def data(im):return render.data_image(render.image_bytes(im))
  parts={'BASE':data(self.fixture()),'BASE_LEFT':data(auto.portrait_base(self.fixture(),g)),'FRAME':data(Image.new('RGBA',(359,538),'red')),'SCROLL_LEFT':data(Image.new('RGBA',(660,934),'blue')),'SCROLL_RIGHT':data(Image.new('RGBA',(660,934),'green')),'_geometry':g}
  im,_=auto.preview_composite(render,parts);self.assertEqual(im.size,(1432,1279));self.assertEqual(im.getpixel((0,0))[3],0)
  # Lower body of right and left samples has same vertical bounds after bottom alignment.
  left=im.getpixel((220,1100));right=im.getpixel((936,1100));self.assertTrue(all(abs(a-b)<=3 for a,b in zip(left,right)));self.assertEqual(left[3],255)
  self.assertIn('align-items:end',auto.geometry_css(parts))
 def test_single_parent_title_and_signature(self):
  v={'hostSide':'bride','brideParents':'김영숙','brideName':'이다온','brideRelation':'장녀'}
  self.assertEqual(render.words(v,'wedding')[2:],["김영숙 올림","김영숙의 장녀 이다온 예식"] if False else ('김영숙 올림','김영숙의 장녀 이다온 예식'))
if __name__=='__main__':unittest.main()
