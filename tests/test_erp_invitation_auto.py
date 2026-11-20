import hashlib
import re
import types
import unittest
from PIL import Image
import erp_invitation_auto as auto
import erp_invitation_render as render

class AutomaticTests(unittest.TestCase):
    def test_font_loading_does_not_import_optional_matplotlib_runtime(self):
        import builtins
        from unittest.mock import patch
        original=builtins.__import__
        def imported(name,*args,**kwargs):
            if name in ('koreanize_matplotlib','matplotlib'):raise ModuleNotFoundError('optional plotting runtime unavailable')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=imported):
            self.assertEqual(render.font(20).size,20)
            self.assertEqual(render.font(20,True).size,20)

    def test_four_verified_photos_fill_slots_without_merging_couples_or_using_rejected_images(self):
        calls=[];progress=[]
        def image(url):calls.append(url);return Image.new('RGB',(40,30),(int(url[-1])*30,10,20))
        worker=types.SimpleNamespace(image=image)
        values={'images':['https://card.test/'+str(i) for i in range(8)]}
        from unittest.mock import patch
        faces=[(1,1,5,5),(8,1,12,5)]
        with patch.object(auto.geometry,'detect_faces',side_effect=lambda im:faces if im.getpixel((0,0))[0]>=60 else []):
            result,warnings=auto.select_photos(worker,values,checker=lambda im:(_ for _ in ()).throw(AssertionError('local result must skip checker')),progress=lambda done,total:progress.append((done,total)))
        self.assertEqual(calls,['https://card.test/'+str(i) for i in range(6)])
        self.assertEqual(result['hero'],'https://card.test/2');self.assertEqual(result['frame'],'https://card.test/3');self.assertEqual(result['scrollLeft'],'https://card.test/4');self.assertEqual(result['scrollRight'],'https://card.test/5');self.assertFalse(warnings)
        self.assertEqual(progress,[(0,4),(1,4),(2,4),(3,4),(4,4)])
    def test_one_verified_source_fills_all_roles_without_pausing(self):
        progress=[];checks=[]
        from unittest.mock import patch
        with patch.object(auto.geometry,'detect_faces',return_value=[]):
            v,w=auto.select_photos(types.SimpleNamespace(image=lambda u:Image.new('RGB',(40,30),'red')),{'images':['https://card.test/1']},checker=lambda im:checks.append(im) or True,progress=lambda done,total:progress.append((done,total)))
        self.assertEqual([v[k] for k in auto.ROLES],['https://card.test/1']*4)
        self.assertTrue(w);self.assertEqual(progress[-1],(4,4));self.assertLessEqual(len(checks),2)
    def test_non_couple_hero_stays_main_but_verified_design_source_fills_production_roles(self):
        from unittest.mock import patch
        def image(url):return Image.new('RGB',(40,30),(10 if url.endswith('hero') else 80,20,30))
        values={'hero':'https://card.test/hero','images':['https://card.test/hero','https://card.test/couple']}
        checks=[]
        with patch.object(auto.geometry,'detect_faces',return_value=[]),patch.object(auto.geometry,'automatic_crop',side_effect=AssertionError('selection must not crop')):
            result,warnings=auto.select_photos(types.SimpleNamespace(image=image),values,checker=lambda im:checks.append(im) or im.getpixel((0,0))[0]>50)
        self.assertEqual(result['hero'],'https://card.test/hero')
        self.assertEqual([result[k] for k in auto.ROLES[1:]],['https://card.test/couple']*3)
        self.assertTrue(warnings);self.assertLessEqual(len(checks),2)
    def test_one_local_two_face_candidate_skips_remote_checker_and_reaches_four_of_four(self):
        from unittest.mock import patch
        progress=[];faces=[(1,1,6,7),(10,1,16,7)]
        def image(url):return Image.new('RGB',(40,30),(80 if url.endswith('couple') else 20,30,40))
        values={'hero':'https://card.test/hero','images':['https://card.test/hero','https://card.test/couple']}
        with patch.object(auto.geometry,'detect_faces',side_effect=lambda im:faces if im.getpixel((0,0))[0]>50 else []):
            result,warnings=auto.select_photos(types.SimpleNamespace(image=image),values,checker=lambda im:(_ for _ in ()).throw(AssertionError('checker must not run')),progress=lambda done,total:progress.append((done,total)))
        self.assertEqual([result[k] for k in auto.ROLES[1:]],['https://card.test/couple']*3)
        self.assertEqual(progress[-1],(4,4));self.assertTrue(warnings)
    def test_approved_reference_css_and_dom_are_preserved_and_dynamic_data_escaped(self):
        values={'name':'가상 예식','groomName':'가상남','brideName':'가상녀','groomParents':'가상父·가상母','brideParents':'가상父·가상母','groomRelation':'장남','brideRelation':'장녀','date':'2099-12-12','time':'14:30','venue':'검증홀 <script>','address':'가상 주소','sourceUrl':'https://card.test/1'}
        components={k:'data:image/webp;base64,eA==' for k in ['BASE','FRAME','SCROLL_LEFT','SCROLL_RIGHT']}
        p=auto.page(render,values,99000,'https://bp2.kr/test',components,'https://card.test/photo','https://card.test/og','https://public.test/1')
        source=(__import__('pathlib').Path(auto.__file__).parent/'erp_invitation_assets/wedding-reference.html').read_text()
        self.assertTrue(re.search('<style>(.*?)</style>',p,re.S)[1].startswith(re.search('<style>(.*?)</style>',source,re.S)[1]));self.assertNotIn('{{',p)
        self.assertIn('class="wreath-promotion"',p);self.assertEqual(p.count('class="product"'),2);self.assertIn('검증홀 &lt;script&gt;',p);self.assertIn('https://bp2.kr/test',p);self.assertNotIn('박건욱',p)

if __name__=='__main__':unittest.main()
