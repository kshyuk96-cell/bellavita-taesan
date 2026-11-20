"""Native order-form photo fix uses approved scroll crops without changing orders."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

import checkout
import erp_wedding_photos as geometry


class CheckoutPhoto(unittest.TestCase):
    def product(self):
        return {'localCheckout':True,'publicationId':'a'*32,
                'wedding_photo':'https://example.org/erp/i/'+'a'*32+'/hero.webp',
                'wedding_crop':95,'price':93500,'number':'12345'}
    def test_existing_and_new_native_snapshots_map_to_safe_public_asset_without_mutation(self):
        product=self.product();before=json.dumps(product,sort_keys=True)
        preview=checkout.wedding_preview(product)
        self.assertEqual(preview,{'url':'https://example.org/erp/i/'+'a'*32+'/checkout-photo.webp','position':None})
        self.assertEqual(json.dumps(product,sort_keys=True),before)
        markup=checkout.wedding_image(preview)
        self.assertIn('object-fit:fill',markup)
        self.assertNotIn('object-position',markup)
        self.assertNotIn('object-fit:cover',markup)
        self.assertNotIn('object-fit:contain',markup)
    def test_mapping_requires_exact_publication_and_own_checkout_snapshot(self):
        for change in ({'publicationId':'b'*32},{'localCheckout':False},{'wedding_photo':'https://example.org/hero.webp'},
                       {'wedding_photo':'https://example.org/erp/i/'+'a'*32+'/hero.webp?crop=1'}):
            with self.subTest(change=change):
                p={**self.product(),**change};result=checkout.wedding_preview(p)
                self.assertEqual(result['url'],p['wedding_photo']);self.assertEqual(result['position'],95)
    def test_erp_private_preview_placeholder_is_not_rewritten_to_missing_public_asset(self):
        p={**self.product(),'wedding_photo':'https://preview.invalid/hero.webp'}
        self.assertEqual(checkout.wedding_preview(p)['url'],'https://preview.invalid/hero.webp')
    def test_safe_crop_fills_exact_checkout_header_and_contains_both_expanded_face_boxes(self):
        image=Image.new('RGB',(120,200),'#376b8c');draw=ImageDraw.Draw(image)
        faces=[(15,10,43,52),(74,28,104,75)]
        for face in faces:draw.rectangle(face,fill='#e7a675')
        target=(1380,915);bounds=geometry.face_crop_bounds(image.size,target,faces)
        self.assertIsNotNone(bounds)
        for left,top,right,bottom in faces:
            self.assertLessEqual(bounds[0],left);self.assertLessEqual(bounds[1],top)
            self.assertGreaterEqual(bounds[2],right);self.assertGreaterEqual(bounds[3],bottom)
        with patch.object(geometry,'detect_faces',return_value=faces):result=geometry.crop(image,target)
        self.assertEqual(result.size,target)
        for point in [(0,0),(result.width-1,0),(0,result.height-1),(result.width-1,result.height-1)]:
            self.assertEqual(result.getpixel(point),(55,107,140))  # real source background; no padding
        self.assertLess(abs(target[0]/target[1]-(500*.385*.896)/(768*.352*.423)),.001)
    def test_unsafe_photo_fails_instead_of_blindly_cutting_a_face(self):
        source=Image.new('RGB',(120,200),'blue')
        with patch.object(geometry,'detect_faces',return_value=[(5,0,40,50),(70,150,110,200)]):
            with self.assertRaisesRegex(ValueError,'두 얼굴을 보존'):geometry.crop(source,(1380,915))
        with patch.object(geometry,'detect_faces',return_value=[]):
            with self.assertRaisesRegex(ValueError,'두 얼굴'):geometry.crop(source,(1380,915))

if __name__=='__main__':unittest.main()
