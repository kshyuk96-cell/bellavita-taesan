import unittest,tempfile,sqlite3,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import erp_invitation_library as library
class Refresh(unittest.TestCase):
 def test_refresh_keeps_links_and_versions_only_thumbnail(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);folder=root/'pub';folder.mkdir();key='a'*32
   (folder/'thumbnail.jpg').write_bytes(b'old')
   for price in [93500,99000]:(folder/f'{price}.html').write_text('<meta property="og:image" content="https://example.test/thumbnail.jpg"><a href="https://example.test/order">주문</a>')
   library.save(root,{key:{'id':key,'folder':str(folder),'revision':1,'links':[{'price':93500,'orderUrl':'https://example.test/order'}]}})
   def db():
    c=sqlite3.connect(root/'journal');c.row_factory=sqlite3.Row;return c
   with db() as c:c.execute('CREATE TABLE jobs (public_id TEXT,action TEXT,state TEXT,created_at TEXT,input TEXT)');c.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',(key,'publish','done','2026',json.dumps({'kind':'wedding','values':{}})))
   with patch('erp_invitation_render.thumbnail',return_value=b'new'):
    result=library.refresh_thumbnail(SimpleNamespace(db=db),root,key,'test')
   row=library.state(root)[key];html=library.current_html(root,row,93500)
   self.assertIn('thumbnail.jpg?v='+result['thumbnailVersion'],html);self.assertIn('https://example.test/order',html)
   self.assertEqual((folder/'thumbnail.previous.jpg').read_bytes(),b'old');self.assertEqual((folder/'thumbnail.jpg').read_bytes(),b'new');self.assertEqual(row['links'][0]['orderUrl'],'https://example.test/order')
if __name__=='__main__':unittest.main()
