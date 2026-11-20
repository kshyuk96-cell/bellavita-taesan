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
 def test_relink_one_price_changes_only_its_button_and_replays_with_audit(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'BLOGPAY_SHOP_ID':'bellavita1004'}):
   root=Path(tmp);key='d'*32;folder=root/'pages';folder.mkdir()
   old={price:f'https://erp.example/i/{"e"*32}/{price}/order' for price in m.PRICES}
   for price in m.PRICES:
    (folder/f'{price}.html').write_text(f'<html><head></head><body><h1>{price}원</h1><a class="order" href="{old[price]}">주문하기</a><p>본문 고정</p></body></html>')
   for name,raw in [('thumbnail.jpg',b'thumbnail'),('sample.webp',b'sample'),('hero.webp',b'hero')]:
    (folder/name).write_bytes(raw)
   links=[{'price':price,'productId':str(price),'orderUrl':old[price],'url':f'https://erp.example/erp/i/{key}/{price}.html'} for price in m.PRICES]
   row={'id':key,'kind':'wedding','createdAt':m.now().isoformat(),'date':'2099-01-01','folder':str(folder),'title':'제목','links':links,'revision':1,'salesStopped':False}
   m.save(root,{key:row});before_files={p.name:p.read_bytes() for p in folder.iterdir()};before={price:m.current_html(root,row,price) for price in m.PRICES}
   target='https://bellavita1004.shop.blogpay.co.kr/v/g/dsCd4'
   request={'action':'relink-order','revision':1,'price':99000,'expectedOrderUrl':old[99000],'orderUrl':target}
   result=m.update(root,key,request,'검증 직원');updated=m.state(root)[key]
   self.assertEqual((result['revision'],result['price'],result['orderUrl']),(2,99000,target))
   self.assertEqual(m.current_html(root,updated,93500),before[93500])
   self.assertEqual(m.current_html(root,updated,99000),before[99000].replace(old[99000],target,1))
   self.assertEqual({p.name:p.read_bytes() for p in folder.iterdir()},before_files)
   self.assertEqual([x['orderUrl'] for x in updated['links']],[old[93500],target])
   event=updated['history'][-1]
   self.assertEqual({k:event[k] for k in ('action','previousRevision','revision','price','fromOrderUrl','toOrderUrl','by')},{'action':'relink-order','previousRevision':1,'revision':2,'price':99000,'fromOrderUrl':old[99000],'toOrderUrl':target,'by':'검증 직원'})
   replay=m.update(root,key,request,'검증 직원');self.assertTrue(replay['replayed']);self.assertEqual(replay['revision'],2)
   self.assertEqual(len(m.state(root)[key]['history']),1)
   import contextlib,sqlite3
   from types import SimpleNamespace
   dbpath=root/'jobs.sqlite';c=sqlite3.connect(dbpath);c.execute('CREATE TABLE jobs(input,result,public_id,created_at,action,state)')
   job={'kind':'wedding','values':{'name':'원 발행','date':'2099-01-01'}};published={'jobId':key,'publishedAt':m.now().isoformat(),'items':[{'price':p,'orderUrl':old[p]} for p in m.PRICES]}
   c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(json.dumps(job),json.dumps(published),key,m.now().isoformat(),'publish','done'));c.commit();c.close()
   @contextlib.contextmanager
   def db():
    c=sqlite3.connect(dbpath);c.row_factory=sqlite3.Row
    try:yield c
    finally:c.close()
   state_before=(m.base(root)/'state.json').read_bytes();m.register(SimpleNamespace(db=db,root=root/'engine',origin='https://erp.example'),root)
   self.assertEqual((m.base(root)/'state.json').read_bytes(),state_before)
   self.assertEqual(next(x for x in m.state(root)[key]['links'] if x['price']==99000)['orderUrl'],target)
 def test_relink_rejects_bad_host_price_revision_and_nonmanaged_rows(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'BLOGPAY_SHOP_ID':'bellavita1004'}):
   root=Path(tmp);key='f'*32;folder=root/'pages';folder.mkdir();old={}
   for price in m.PRICES:
    old[price]=f'https://erp.example/i/{"1"*32}/{price}/order';(folder/f'{price}.html').write_text(f'<a href="{old[price]}">주문</a>')
   row={'id':key,'kind':'wedding','createdAt':m.now().isoformat(),'date':'2099-01-01','folder':str(folder),'title':'제목','links':[{'price':p,'orderUrl':old[p]} for p in m.PRICES],'revision':1,'salesStopped':False}
   m.save(root,{key:row});valid={'action':'relink-order','revision':1,'price':99000,'expectedOrderUrl':old[99000],'orderUrl':'https://bellavita1004.shop.blogpay.co.kr/v/g/dsCd4'};before=(m.base(root)/'state.json').read_bytes()
   for change,message in [({'orderUrl':'https://other.shop.blogpay.co.kr/v/g/dsCd4'},'현재 블로그페이'),({'price':1000},'가격'),({'revision':0},'다른 직원')]:
    with self.subTest(change=change),self.assertRaisesRegex(ValueError,message):m.update(root,key,{**valid,**change},'staff')
    self.assertEqual((m.base(root)/'state.json').read_bytes(),before)
   state=m.state(root);state[key]['external']=True;m.save(root,state);external_before=(m.base(root)/'state.json').read_bytes()
   with self.assertRaisesRegex(ValueError,'외부 발행'):m.update(root,key,valid,'staff')
   self.assertEqual((m.base(root)/'state.json').read_bytes(),external_before)
 def test_existing_external_pair_migrates_to_own_checkout_once(self):
  import contextlib,sqlite3
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);engine_root=root/'erp-invitations';key='c'*32;folder=engine_root/'public'/key;folder.mkdir(parents=True)
   template=root/'invitations'/'7f494a310ca9371914b8863b45732bd5';template.mkdir(parents=True)
   olditems=[]
   for price in m.PRICES:
    (template/f'{price}.order.json').write_text(json.dumps({'groups':[{'label':label,'choices':{'1':{'label':'기본','amount':0,'custom':False}}} for label in ['축하 문구','받으시는 분','상품 선택']],'images':[]}))
    (folder/f'{price}.html').write_text(f'<html><head></head><body><h1>가상 예식</h1><a href="https://bp2.kr/test{price}">주문</a></body></html>')
    olditems.append({'price':price,'productId':str(price),'orderUrl':f'https://bp2.kr/test{price}','url':f'https://test.example/erp/i/{key}/{price}.html'})
   result={'jobId':key,'fingerprint':'d'*64,'items':olditems};(folder/'result.json').write_text(json.dumps(result))
   job={'invitationId':'IV-migration-fixture','version':1,'kind':'wedding','values':{'name':'가상 예식','date':'2099-01-01','time':'12:00','venue':'가상홀','address':'가상주소'}}
   dbpath=root/'jobs.sqlite';c=sqlite3.connect(dbpath);c.execute('CREATE TABLE jobs(input,result,public_id,created_at,action,state)');c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(json.dumps(job),json.dumps(result),key,m.now().isoformat(),'publish','done'));c.commit();c.close()
   @contextlib.contextmanager
   def db():
    c=sqlite3.connect(dbpath);c.row_factory=sqlite3.Row
    try:yield c
    finally:c.close()
   worker=SimpleNamespace(root=engine_root,origin='https://test.example',db=db)
   rows=m.catalog(worker,root)['items'];self.assertEqual(len(rows),1)
   self.assertTrue(all('/i/' in l['orderUrl'] for l in rows[0]['links']))
   self.assertNotIn('bp2.kr',m.current_html(root,m.state(root)[key],93500))
   self.assertEqual(m.catalog(worker,root)['items'][0]['revision'],1)
   m.update(root,key,{'action':'sales','revision':1,'stopped':True},'staff')
   for link in rows[0]['links']:
    localkey=link['orderUrl'].split('/i/')[1].split('/')[0]
    product=checkout.read(root,localkey,str(link['price']));self.assertIsNotNone(product);self.assertFalse(checkout.available(product))
