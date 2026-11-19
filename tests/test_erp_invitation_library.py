import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import erp_invitation_library as m
import checkout

class Library(unittest.TestCase):
 def test_memorial_72_hours_and_event_kst_midnight(self):
  row={'kind':'memorial','createdAt':'2026-11-19T10:00:00+00:00','date':'2026-11-20'}
  self.assertTrue(m.status(row,dt.datetime.fromisoformat('2026-11-22T09:59:59+00:00'))['operating'])
  self.assertFalse(m.status(row,dt.datetime.fromisoformat('2026-11-22T10:00:00+00:00'))['operating'])
  row['kind']='wedding'
  self.assertTrue(m.status(row,dt.datetime.fromisoformat('2026-11-20T14:59:59+00:00'))['operating'])
  self.assertFalse(m.status(row,dt.datetime.fromisoformat('2026-11-20T15:00:00+00:00'))['operating'])
 def test_shared_sales_gate_edit_revision_and_escaped_text(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);key='a'*32;folder=root/'pages';folder.mkdir()
   for p in m.PRICES:(folder/f'{p}.html').write_text(f'<html><head></head><body><h1>초대합니다</h1><p>기존 문구</p><h2>{p}원</h2></body></html>')
   row={'id':key,'kind':'memorial','createdAt':m.now().isoformat(),'date':'2000-01-01','folder':str(folder),'title':'제목','revision':1,'salesStopped':False}
   m.save(root,{key:row})
   self.assertEqual(len(m.detail(root,row)['fields']),2)
   for p in m.PRICES:self.assertTrue(checkout.available({'publicationId':key,'_libraryRoot':tmp,'event_time':'2000-01-01'}))
   m.update(root,key,{'action':'sales','revision':1,'stopped':True},'staff')
   for p in m.PRICES:self.assertFalse(checkout.available({'publicationId':key,'_libraryRoot':tmp,'event_time':'2099-01-01'}))
   self.assertTrue(m.status(m.state(root)[key])['operating'])
   with self.assertRaises(ValueError):m.update(root,key,{'action':'sales','revision':1,'stopped':False},'staff')
   p={'action':'edit','revision':2,'fields':{'0':'<script>alert(1)</script>'},'design':{'lineHeight':1.3}}
   m.update(root,key,p,'staff');updated=m.state(root)[key]
   for price in m.PRICES:
    raw=m.current_html(root,updated,price);self.assertIn('&lt;script&gt;',raw);self.assertIn(str(price)+'원',raw)
   self.assertEqual(updated['createdAt'],row['createdAt'])
   self.assertIn('기존 문구',(folder/'93500.html').read_text())
 def test_disallow_price_edit_and_css_injection(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);row={'id':'b'*32,'folder':tmp,'revision':1,'title':'x'}
   for p in m.PRICES:(root/f'{p}.html').write_text(f'<html><head></head><body><h1>{p}</h1></body></html>')
   with self.assertRaises(ValueError):m.edit_documents(root,row,{'fields':{'0':'무료'}})
   with self.assertRaises(ValueError):m.edit_documents(root,row,{'design':{'color':'red;display:none'}})
