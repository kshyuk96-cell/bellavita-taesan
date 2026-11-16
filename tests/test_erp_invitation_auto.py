import hashlib
import re
import types
import unittest
from PIL import Image
import erp_invitation_auto as auto
import erp_invitation_render as render

class AutomaticTests(unittest.TestCase):
    def test_three_verified_photos_fill_slots_without_merging_couples_or_using_rejected_images(self):
        calls=[]
        def image(url):calls.append(url);return Image.new('RGB',(40,30),(int(url[-1])*30,10,20))
        worker=types.SimpleNamespace(image=image)
        values={'images':['https://card.test/'+str(i) for i in range(8)]}
        result,warnings=auto.select_photos(worker,values,checker=lambda im:im.getpixel((0,0))[0]>=60)
        self.assertEqual(calls,['https://card.test/'+str(i) for i in range(5)])
        self.assertEqual(result['frame'],'https://card.test/2');self.assertEqual(result['scrollLeft'],'https://card.test/3');self.assertEqual(result['scrollRight'],'https://card.test/4');self.assertFalse(warnings)
    def test_incomplete_source_leaves_only_missing_fields_unfilled(self):
        v,w=auto.select_photos(types.SimpleNamespace(image=lambda u:Image.new('RGB',(40,30),'red')),{'images':['https://card.test/1']},checker=lambda im:True)
        self.assertNotIn('frame',v);self.assertTrue(w)
    def test_approved_reference_css_and_dom_are_preserved_and_dynamic_data_escaped(self):
        values={'name':'가상 예식','groomName':'가상남','brideName':'가상녀','groomParents':'가상父·가상母','brideParents':'가상父·가상母','groomRelation':'장남','brideRelation':'장녀','date':'2099-12-12','time':'14:30','venue':'검증홀 <script>','address':'가상 주소','sourceUrl':'https://card.test/1'}
        components={k:'data:image/webp;base64,eA==' for k in ['BASE','FRAME','SCROLL_LEFT','SCROLL_RIGHT']}
        p=auto.page(render,values,99000,'https://bp2.kr/test',components,'https://card.test/photo','https://card.test/og','https://public.test/1')
        source=(__import__('pathlib').Path(auto.__file__).parent/'erp_invitation_assets/wedding-reference.html').read_text()
        self.assertEqual(re.search('<style>(.*?)</style>',p,re.S)[1],re.search('<style>(.*?)</style>',source,re.S)[1]);self.assertNotIn('{{',p)
        self.assertIn('class="wreath-promotion"',p);self.assertEqual(p.count('class="product"'),2);self.assertIn('검증홀 &lt;script&gt;',p);self.assertIn('https://bp2.kr/test',p);self.assertNotIn('박건욱',p)

if __name__=='__main__':unittest.main()
