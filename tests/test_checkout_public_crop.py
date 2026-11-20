import tempfile,json,unittest,io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
import erp_invitations as bridge
class PublicCrop(unittest.TestCase):
 def test_existing_publication_gets_best_effort_cached_header_without_rewriting_order(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);worker=bridge.Engine(root,'https://app.test',SimpleNamespace(),lambda x:{},async_jobs=False);key='a'*32;folder=worker.root/'public'/key;folder.mkdir(parents=True)
   (folder/'result.json').write_text(json.dumps({'expires':'2099-12-12'}))
   for n in ['93500.html','99000.html','sample.webp','thumbnail.jpg']:(folder/n).write_bytes(b'fixture')
   Image.new('RGB',(1000,1500),(180,20,80)).save(folder/'hero.webp')
   with patch('erp_wedding_photos.detect_faces',return_value=[]):status,mime,raw=worker.public(key,'checkout-photo.webp')
   self.assertEqual(status,200);self.assertEqual(mime,'image/webp');self.assertEqual(Image.open(io.BytesIO(raw)).size,(1380,915))
   with patch('erp_wedding_photos.detect_faces',side_effect=AssertionError('cached')):self.assertEqual(worker.public(key,'checkout-photo.webp')[2],raw)
   self.assertEqual((folder/'93500.html').read_bytes(),b'fixture')
 def test_unpublished_images_never_trigger_generation(self):
  with tempfile.TemporaryDirectory() as tmp:
   worker=bridge.Engine(Path(tmp),'https://app.test',SimpleNamespace(),lambda x:{},async_jobs=False)
   with patch('erp_wedding_photos.automatic_crop',side_effect=AssertionError('must validate manifest first')):self.assertEqual(worker.public('b'*32,'checkout-photo.webp')[0],404)
if __name__=='__main__':unittest.main()
