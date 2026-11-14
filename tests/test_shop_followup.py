"""Five approved follow-ups: isolated HTTP + SQLite + mock providers only."""
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from PIL import Image
from test_shop import ShopFixture, SECRET, notification
import agent
import order_ops
import order_messages
import production_files
import scheduled_backups
import manage_data
import shop_dashboard
import shop_receiver
import order_handoff


class FollowupTest(ShopFixture):
    def pay(self,photo=False):
        product=dict(self.product)
        if not photo:product.pop('wedding_photo',None)
        with agent.db() as c:c.execute('UPDATE shop_offers SET details=? WHERE id=?',(json.dumps(product),self.offer))
        with patch.dict(os.environ,{'SHOP_PRODUCTION_REVIEW_REQUIRED':'1'}):self.place()
        self.notify(notification('a'*32))

    def row(self):
        with agent.db() as c:return c.execute('SELECT * FROM invitation_orders WHERE id=?',('a'*32,)).fetchone()

    def generate(self,width='100',height='300'):
        self.post_admin(action='generate_artifact',order_id='a'*32,width_mm=width,height_mm=height,dpi='150')
        with agent.db() as c:return production_files.latest(c,'a'*32)

    def review_fields(self,decision='approved'):
        with agent.db() as c:
            artifact=production_files.latest(c,'a'*32);review=production_files.latest_review(c,'a'*32)
        return dict(action='review_artifact',order_id='a'*32,artifact_version=str(artifact['version']),artifact_hash=artifact['source_hash'],
                    artifact_review_revision=str(review['id'] if review else 0),artifact_decision=decision,
                    artifact_reviewer='검증 담당자',artifact_note='두 파일의 주문번호·문구·중량·규격 대조',artifact_compared='yes')

    def factory_env(self):return patch.dict(os.environ,{'SHOP_HANDOFF_ENABLED':'1','SHOP_HANDOFF_SECRET':'factory-key-'*4})

    def transport(self,action,fields):
        with agent.db() as c:
            if action=='claim':return order_handoff.claim(c,fields)
            result,status=order_handoff.acknowledge(c,fields)
            if status!=200:raise shop_receiver.AckConflict(result['error'])
            return result

    def download(self,path,authorized=True):
        client=http.client.HTTPConnection(*self.server.server_address)
        client.request('GET',path,headers={'Authorization':'Bearer '+('factory-key-'*4 if authorized else 'wrong')})
        response=client.getresponse();data=response.read();status=response.status;client.close()
        if status!=200:raise ValueError('file_http_'+str(status))
        return data

    def test_file_review_gate_download_integrity_and_one_factory_row(self):
        self.pay()
        with agent.db() as c:self.assertIsNone(order_handoff.claim(c,{'consumer':'old-factory'})['job'])
        artifact=self.generate();self.assertEqual('pending',artifact['state'])
        same=self.generate();self.assertEqual(artifact['version'],same['version'])
        self.assertEqual(422,self.http(self.admin,dict(action='change_stage',order_id='a'*32,version='1',stage='shipping'))[0])
        fields=self.review_fields();self.post_admin(**fields);self.post_admin(**fields)
        with agent.db() as c:
            self.assertEqual(1,c.execute('SELECT count(*) FROM shop_artifact_reviews').fetchone()[0])
            self.assertIsNone(order_handoff.claim(c,{'consumer':'old-factory'})['job'])
        target=Path(self.tmp.name)/'receiver'
        with self.factory_env():
            self.assertTrue(shop_receiver.run_once(target,'new-factory',self.transport,self.download))
            self.assertFalse(shop_receiver.run_once(target,'new-factory',self.transport,self.download))
        with sqlite3.connect(target/'receiver.sqlite3') as c:
            state,details=c.execute('SELECT state,details FROM production_queue').fetchone()
            self.assertEqual('pending',state);self.assertEqual(1,json.loads(details)['artifact']['version'])
            self.assertEqual(1,c.execute('SELECT count(*) FROM production_queue').fetchone()[0])
        with Image.open(target/'production-files'/('a'*32)/'1.png') as image:self.assertEqual((591,1772),image.size)
        self.assertTrue((target/'production-files'/('a'*32)/'1.pdf').read_bytes().startswith(b'%PDF-'))

    def test_revised_artifact_requires_reapproval_and_old_screen_cannot_approve(self):
        self.pay();first=self.generate();fields=self.review_fields();self.post_admin(**fields)
        target=Path(self.tmp.name)/'factory'
        with self.factory_env():shop_receiver.run_once(target,'factory',self.transport,self.download)
        new=self.generate(width='110');self.assertEqual(2,new['version'])
        self.assertEqual(422,self.http(self.admin,fields)[0])
        with self.factory_env():
            with self.assertRaises(ValueError):self.download('/shop/handoff/file/'+('a'*32)+'/1.pdf')
            shop_receiver.run_once(target,'factory',self.transport,self.download)
        with sqlite3.connect(target/'receiver.sqlite3') as c:self.assertEqual('held',c.execute('SELECT state FROM production_queue').fetchone()[0])
        self.post_admin(**self.review_fields())
        with self.factory_env():shop_receiver.run_once(target,'factory',self.transport,self.download)
        with sqlite3.connect(target/'receiver.sqlite3') as c:
            state,details=c.execute('SELECT state,details FROM production_queue').fetchone()
            self.assertEqual('pending',state);self.assertEqual(2,json.loads(details)['artifact']['version'])

    def test_tampered_file_never_releases_factory_queue_or_approval(self):
        self.pay();self.generate();fields=self.review_fields()
        path=production_files.artifact_path(self.root,'a'*32,1,'png');original=path.read_bytes();path.write_bytes(b'changed')
        self.assertEqual(422,self.http(self.admin,fields)[0]);path.write_bytes(original);self.post_admin(**fields)
        job=self.transport('claim',{'consumer':'factory','capabilities':['production-artifact-v1']})['job']
        target=Path(self.tmp.name)/'tampered'
        with self.assertRaises(ValueError):shop_receiver.ingest(target,job,lambda path:b'wrong-file')
        self.assertFalse((target/'receiver.sqlite3').exists())
        with self.factory_env():
            with self.assertRaises(ValueError):self.download('/shop/handoff/file/'+('a'*32)+'/1.pdf',authorized=False)
        signature=order_ops.signed_path('production-1-pdf','a'*32,SECRET,lifetime=-1)
        self.assertEqual(403,self.http(signature)[0])

    def test_cancel_during_file_delivery_and_rejection_keep_queue_held(self):
        self.pay();self.generate();self.post_admin(**self.review_fields())
        job=self.transport('claim',{'consumer':'factory','capabilities':['production-artifact-v1']})['job']
        target=Path(self.tmp.name)/'cancel-factory'
        with self.factory_env():shop_receiver.ingest(target,job,self.download)
        self.notify(notification('a'*32,state='9'))
        shop_receiver.acknowledge_pending(target,self.transport)
        with sqlite3.connect(target/'receiver.sqlite3') as c:self.assertEqual('held',c.execute('SELECT state FROM production_queue').fetchone()[0])
        with self.factory_env():
            with self.assertRaises(ValueError):self.download('/shop/handoff/file/'+('a'*32)+'/1.pdf')

    def test_long_text_layout_png_pdf_and_invalid_size(self):
        self.pay();data=json.loads(self.row()['details']);data['message']='축하문구'*50;data['sender']='보내는분'*35
        with agent.db() as c:c.execute('UPDATE invitation_orders SET details=? WHERE id=?',(json.dumps(data),'a'*32))
        artifact=self.generate();self.assertEqual('pending',artifact['state'])
        self.assertEqual(422,self.http(self.admin,dict(action='generate_artifact',order_id='a'*32,width_mm='600',height_mm='1800',dpi='300'))[0])
        signed=order_ops.signed_path('production-1-pdf','a'*32,SECRET)
        with self.factory_env():content=self.download(signed)
        self.assertTrue(content.startswith(b'%PDF-'))

    def test_alimtalk_template_links_poll_and_no_duplicate_on_payment(self):
        with patch.dict(os.environ,{'SHOP_MESSAGE_CHANNEL':'alimtalk'}):self.pay()
        self.notify(notification('a'*32));sent=[]
        def api(path,body=None):
            if '/send-' in path:
                sent.append(body['messages'][0]);return {'groupInfo':{'groupId':'G'},'messageList':[{'messageId':'M'}]}
            return {'messageList':{'M':{'to':'01012345678','groupId':'G','statusCode':'4000'}}}
        env={'SHOP_MESSAGES_ENABLED':'1','SHOP_ALIMTALK_ENABLED':'1','SOLAPI_API_KEY':'test','SOLAPI_API_SECRET':'test','SOLAPI_KAKAO_PFID':'PF_TEST','SHOP_ALIMTALK_TEMPLATE_ID':'TPL_TEST'}
        with patch.dict(os.environ,env),patch.object(order_messages,'api',side_effect=api) as mock:
            order_messages.process_one(agent.db,self.root);order_messages.process_one(agent.db,self.root)
            self.assertEqual(1,len(sent));item=sent[0];self.assertEqual('ATA',item['type']);self.assertNotIn('imageId',item)
            self.assertTrue(item['kakaoOptions']['disableSms']);self.assertIn('/receipt#sample',item['kakaoOptions']['variables']['#{샘플링크}'])
            self.assertFalse(any('/storage/' in call.args[0] for call in mock.call_args_list))
            with agent.db() as c:c.execute('UPDATE shop_messages SET checked=0')
            order_messages.process_one(agent.db,self.root)
        with agent.db() as c:self.assertEqual('delivered',c.execute('SELECT state FROM shop_messages').fetchone()[0])

    def test_alimtalk_timeout_no_fallback_and_channel_snapshot(self):
        with patch.dict(os.environ,{'SHOP_MESSAGE_CHANNEL':'alimtalk'}):self.pay()
        env={'SHOP_MESSAGES_ENABLED':'1','SHOP_ALIMTALK_ENABLED':'1','SOLAPI_API_KEY':'test','SOLAPI_API_SECRET':'test','SOLAPI_KAKAO_PFID':'PF','SHOP_ALIMTALK_TEMPLATE_ID':'TPL'}
        with patch.dict(os.environ,env),patch.object(order_messages,'api',side_effect=TimeoutError) as mock:
            order_messages.process_one(agent.db,self.root);order_messages.process_one(agent.db,self.root)
            self.assertEqual(1,mock.call_count)
        with agent.db() as c:
            job=c.execute('SELECT * FROM shop_messages').fetchone();self.assertEqual('uncertain',job['state']);self.assertEqual('alimtalk',job['channel'])
            self.assertEqual('ATA',json.loads(job['request_json'])['type'])
        self.assertEqual(422,self.http(self.admin,dict(action='retry_message',message_id=job['id'],checked_failure='yes'))[0])

    def test_dashboard_filters_counts_and_backup_restore_drill(self):
        self.pay();self.generate();self.post_admin(**self.review_fields())
        with agent.db() as c:c.execute("UPDATE shop_messages SET state='uncertain'")
        with agent.db() as c:
            categories=shop_dashboard.categories(c,self.row());self.assertIn('alerts',categories);self.assertIn('delivery',categories)
        target=Path(self.tmp.name)/'automatic-backups'
        env={'SHOP_BACKUP_ENABLED':'1','SHOP_BACKUP_DIRECTORY':str(target),'SHOP_BACKUP_INTERVAL_SECONDS':'60'}
        with patch.dict(os.environ,env):
            first=scheduled_backups.process_one(agent.db,self.root,lambda:1000)
            self.assertEqual('verified',first['state'],first)
            self.assertIsNone(scheduled_backups.process_one(agent.db,self.root,lambda:1001))
            second=scheduled_backups.process_one(agent.db,self.root,lambda:1061);self.assertEqual('verified',second['state'],second)
        restored=Path(self.tmp.name)/'restored';manage_data.transfer(first['snapshot'],restored,restore=True)
        with sqlite3.connect(restored/'state.sqlite3') as c:
            self.assertEqual('approved',c.execute('SELECT state FROM shop_artifacts').fetchone()[0])
            self.assertEqual(1,c.execute('SELECT count(*) FROM shop_artifact_reviews').fetchone()[0])
            self.assertEqual('uncertain',c.execute('SELECT state FROM shop_messages').fetchone()[0])
        self.assertTrue((restored/'shop-production'/('a'*32)/'1.pdf').exists())
        html=self.http(self.admin)[2];self.assertIn('운영 현황판',html);self.assertIn('복구 검증 통과',html)
        self.assertIn('data-shop-filter="alerts"',html)

    def test_failed_or_unstable_backup_is_not_verified_and_prior_backup_survives(self):
        self.pay();target=Path(self.tmp.name)/'backups'
        env={'SHOP_BACKUP_ENABLED':'1','SHOP_BACKUP_DIRECTORY':str(target),'SHOP_BACKUP_INTERVAL_SECONDS':'60'}
        with patch.dict(os.environ,env):
            first=scheduled_backups.process_one(agent.db,self.root,lambda:1000);self.assertEqual('verified',first['state'])
            with patch.object(scheduled_backups,'snapshot',side_effect=ValueError('unstable')):
                result=scheduled_backups.process_one(agent.db,self.root,lambda:1061)
            self.assertEqual('failed',result['state']);self.assertTrue(Path(first['snapshot']).exists())
        with patch.dict(os.environ,dict(env,SHOP_BACKUP_DIRECTORY=str(self.root/'bad'))):
            with self.assertRaises(ValueError):scheduled_backups.process_one(agent.db,self.root)

    def test_restore_drill_rejects_tampered_artifact_and_concurrent_scheduler_runs_once(self):
        self.pay();self.generate();base=Path(self.tmp.name)/'scheduled'
        env={'SHOP_BACKUP_ENABLED':'1','SHOP_BACKUP_DIRECTORY':str(base),'SHOP_BACKUP_INTERVAL_SECONDS':'60'}
        with patch.dict(os.environ,env):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:scheduled_backups.process_one(agent.db,self.root,lambda:1000),range(2)))
            self.assertEqual(1,sum(result is not None for result in results))
            first=next(result for result in results if result);self.assertEqual('verified',first['state'])
            production_files.artifact_path(self.root,'a'*32,1,'png').write_bytes(b'corrupt')
            broken=scheduled_backups.process_one(agent.db,self.root,lambda:1061)
            self.assertEqual('failed',broken['state']);self.assertTrue(Path(first['snapshot']).exists())

    def test_pdf_physical_size_and_every_long_text_line_stays_on_page(self):
        self.pay();row=dict(self.row());data=json.loads(row['details']);data['message']='축하합니다'*40;data['sender']='보내는분'*35;row['details']=json.dumps(data)
        from PIL import ImageDraw
        original=ImageDraw.ImageDraw.text;bounds=[]
        def draw_text(draw,xy,text,*args,**kwargs):
            bounds.append(draw.textbbox(xy,text,font=kwargs['font'],anchor=kwargs['anchor']))
            return original(draw,xy,text,*args,**kwargs)
        with patch.object(ImageDraw.ImageDraw,'text',draw_text):contents=production_files.render(row,{'width_mm':100,'height_mm':300,'dpi':150})
        self.assertTrue(bounds);self.assertTrue(all(0<=left<=right<=591 and 0<=top<=bottom<=1772 for left,top,right,bottom in bounds))
        import re
        box=re.search(rb'/MediaBox\s*\[([^]]+)\]',contents['pdf'])
        self.assertIsNotNone(box);values=[float(v) for v in box[1].split()]
        self.assertAlmostEqual(100/25.4*72,values[2],delta=1)
        self.assertAlmostEqual(300/25.4*72,values[3],delta=1)

    def test_missing_output_returns_actionable_review_error(self):
        self.pay();self.generate();fields=self.review_fields()
        production_files.artifact_path(self.root,'a'*32,1,'pdf').unlink()
        status,_,body=self.http(self.admin,fields)
        self.assertEqual(422,status);self.assertIn('백업에서 복구',body)

    def test_partial_refund_resumption_keeps_same_approved_files_and_financial_state(self):
        self.pay();self.generate();self.post_admin(**self.review_fields());target=Path(self.tmp.name)/'partial-factory'
        with self.factory_env():shop_receiver.run_once(target,'factory',self.transport,self.download)
        self.notify(notification('a'*32,'9001','70',10000,orig_mul_no='1234',orig_price='93500'))
        with self.factory_env():shop_receiver.run_once(target,'factory',self.transport,self.download)
        with sqlite3.connect(target/'receiver.sqlite3') as c:self.assertEqual('held',c.execute('SELECT state FROM production_queue').fetchone()[0])
        import partial_reviews
        with agent.db() as c:fingerprint,_,remaining=partial_reviews.balance(c,self.row())
        self.post_admin(action='review_partial',order_id='a'*32,partial_fingerprint=fingerprint,partial_remaining=str(remaining),
                        partial_review_revision='0',partial_decision='resume',partial_reviewer='잔액 검증자',partial_note='모의 잔액 83500원 대조',partial_compared='yes')
        with self.factory_env():shop_receiver.run_once(target,'factory',self.transport,self.download)
        with sqlite3.connect(target/'receiver.sqlite3') as c:
            state,details=c.execute('SELECT state,details FROM production_queue').fetchone()
            self.assertEqual('pending',state);self.assertEqual(1,json.loads(details)['artifact']['version'])
            self.assertEqual(1,c.execute('SELECT count(*) FROM order_ledger').fetchone()[0])
            self.assertEqual(83500,json.loads(c.execute('SELECT payment FROM order_ledger').fetchone()[0])['remaining_amount'])
        self.assertEqual('partial_cancel',self.row()['state'])
