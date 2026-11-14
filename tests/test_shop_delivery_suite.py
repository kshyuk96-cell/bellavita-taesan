"""Assignment, scheduling, photographs, settlement, diagnostics: all isolated."""
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from datetime import datetime,timedelta
from unittest.mock import patch

from openpyxl import load_workbook
from PIL import Image
from test_shop import ShopFixture,ORIGIN,SECRET,notification
import agent
import delivery_photos
import shop_dispatch
import shop_settlement
import shop_diagnostics
import scheduled_backups
import manage_data
import order_ops
import alimtalk


def photograph():
    image=Image.new('RGB',(800,600),'#91a483');exif=Image.Exif();exif[270]='private-metadata';output=io.BytesIO();image.save(output,'JPEG',exif=exif);return output.getvalue()


class DeliverySuiteTest(ShopFixture):
    def pay(self,attempt='a'*32,number='1234',**fields):
        self.mock_payment.return_value=(number,'https://payapp.kr/test')
        self.assertEqual(303,self.place(attempt,**fields)[0]);self.notify(notification(attempt,number))

    def delivered(self):
        self.pay();self.post_admin(action='change_stage',order_id='a'*32,stage='shipping',version='1')
        self.post_admin(action='change_stage',order_id='a'*32,stage='delivered',version='2')

    def assign(self,owner='staff',version='0',note='행사장 담당 배정'):
        self.post_admin(action='assign_delivery',order_id='a'*32,assignee=owner,assignment_version=version,assignment_note=note)
        with agent.db() as c:return shop_dispatch.link(c,'a'*32,SECRET)

    def raw(self,path,body=None,content_type=None,origin=ORIGIN):
        client=http.client.HTTPConnection(*self.server.server_address,timeout=10)
        headers={'Origin':origin}
        if content_type:headers['Content-Type']=content_type
        client.request('POST' if body is not None else 'GET',path,body,headers);response=client.getresponse()
        result=response.status,dict(response.getheaders()),response.read();client.close();return result

    def upload(self,path=None,content=None,extra=None):
        fields=dict(action='upload_delivery_photo',order_id='a'*32,photo_note='검증용 배송 사진',**(extra or {}));boundary='BELLAVITA-TEST-BOUNDARY'
        body=b''
        for name,value in fields.items():body+=f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        body+=f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="../../untrusted.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()+(content if content is not None else photograph())+f'\r\n--{boundary}--\r\n'.encode()
        return self.raw(path or self.admin,body,'multipart/form-data; boundary='+boundary)

    def photo(self):
        with agent.db() as c:return c.execute('SELECT * FROM shop_delivery_photos').fetchone()

    def publish(self,state='public'):
        photo=self.photo()
        self.post_admin(action='publish_delivery_photo',order_id='a'*32,photo_id=photo['id'],photo_version=str(photo['version']),photo_state=state,photo_review_note='배송 장소·공개 범위 검수',photo_checked='yes')
        return self.photo()

    def test_assignment_stale_link_revocation_handover_once_and_staff_scope(self):
        self.pay();old=order_ops.signed_path('delivery','a'*32,SECRET);link=self.assign()
        self.assertEqual(403,self.http(old)[0]);self.assertEqual(200,self.http(link)[0])
        fields=dict(action='accept_assignment',order_id='a'*32,assignment_version='1',handover_note='문구·장소·시각 확인',handover_checked='yes')
        self.assertEqual(303,self.http(link,fields)[0]);self.assertEqual(303,self.http(link,fields)[0])
        self.assertEqual(422,self.http(link,dict(fields,action='assign_delivery'))[0])
        new=self.assign('admin','1','교대 인수인계')
        self.assertEqual(403,self.http(link)[0]);self.assertEqual(200,self.http(new)[0])
        self.assertEqual(422,self.http(self.admin,dict(action='assign_delivery',order_id='a'*32,assignee='unknown',assignment_version='2',assignment_note='미등록'))[0])
        self.assertEqual(422,self.http(self.admin,dict(action='assign_delivery',order_id='a'*32,assignee='staff',assignment_version='1',assignment_note='오래된 화면'))[0])
        with agent.db() as c:
            self.assertEqual(2,c.execute('SELECT count(*) FROM shop_assignment_history').fetchone()[0])
            self.assertEqual(1,c.execute('SELECT count(*) FROM shop_assignment_accepts').fetchone()[0])

    def test_revoked_staff_assignment_link_and_customer_ata_never_get_staff_capability(self):
        self.pay()
        with agent.db() as c:c.execute("INSERT INTO staff VALUES ('registered-driver','staff',?)",(time.time(),))
        link=self.assign('registered-driver')
        with agent.db() as c:c.execute("DELETE FROM staff WHERE owner='registered-driver'")
        self.assertEqual(403,self.http(link)[0])
        settings={'TAESAN_PUBLIC_URL':ORIGIN,'SOLAPI_KAKAO_PFID':'mock','SHOP_ALIMTALK_TEMPLATE_ID':'mock'}
        item=alimtalk.message({'id':'m','order_id':'a'*32,'recipient':'01000000000','event':'confirmed','audience':'customer'},self.order(),settings=settings)
        self.assertEqual(ORIGIN+'/invitation-orders/'+('a'*32)+'/receipt',item['kakaoOptions']['variables']['#{주문링크}'])

    def test_schedule_sorted_urgent_assignee_and_completed_filters(self):
        self.pay();self.pay('b'*32,'1235');link=self.assign();now=datetime.now(shop_dispatch.SEOUL)
        with agent.db() as c:
            for key,delta in (('a'*32,3),('b'*32,-2)):
                row=c.execute('SELECT details FROM invitation_orders WHERE id=?',(key,)).fetchone();data=json.loads(row[0]);data['event_time']=(now+timedelta(hours=delta)).isoformat();c.execute('UPDATE invitation_orders SET details=? WHERE id=?',(json.dumps(data),key))
        fields={'schedule_from':(now-timedelta(days=1)).date().isoformat(),'schedule_to':(now+timedelta(days=1)).date().isoformat()}
        with agent.db() as c:
            rows=shop_dispatch.schedule(c,fields,now);self.assertEqual(['b'*32,'a'*32],[r['id'] for r in rows]);self.assertEqual('기한 경과',rows[0]['urgency']);self.assertEqual('24시간 이내',rows[1]['urgency'])
            self.assertEqual(1,len(shop_dispatch.schedule(c,dict(fields,schedule_assignee='staff'),now)))
        self.assertEqual(422,self.http(self.admin+'?schedule_from=bad')[0])

    def test_photo_upload_normalizes_metadata_deduplicates_and_private_until_public(self):
        self.delivered()
        self.assertEqual(303,self.upload()[0]);self.assertEqual(303,self.upload()[0]);photo=self.photo()
        with agent.db() as c:self.assertEqual(1,c.execute('SELECT count(*) FROM shop_delivery_photos').fetchone()[0])
        content=delivery_photos.photo_path(self.root,photo['order_id'],photo['id']).read_bytes()
        with Image.open(io.BytesIO(content)) as image:self.assertEqual('JPEG',image.format);self.assertFalse(image.getexif())
        self.assertNotIn(b'private-metadata',content)
        self.assertEqual(403,self.raw(delivery_photos.link(photo))[0]);self.assertEqual(200,self.raw(delivery_photos.link(photo,'admin'))[0])
        public=self.publish();self.assertEqual(200,self.raw(delivery_photos.link(public))[0])
        self.assertIn('배송 완료 사진',self.http('/invitation-orders/'+('a'*32)+'/receipt')[2])
        self.publish('hidden');self.assertEqual(403,self.raw(delivery_photos.link(public))[0])

    def test_customer_confirmation_is_order_version_origin_bound_and_idempotent(self):
        self.delivered();self.upload();photo=self.publish()
        page=self.http('/invitation-orders/'+('a'*32)+'/receipt')[2]
        fields={name:re.search('name="'+name+'" value="([^"]+)"',page)[1] for name in ('photo_id','photo_version','photo_expiry','photo_signature')};fields['photo_confirmed']='yes'
        path='/invitation-orders/'+('a'*32)+'/delivery-confirm'
        self.assertEqual(403,self.http(path,fields,origin='https://evil.test')[0])
        self.assertEqual(422,self.http('/invitation-orders/'+('b'*32)+'/delivery-confirm',fields)[0])
        self.assertEqual(303,self.http(path,fields)[0]);self.assertEqual(303,self.http(path,fields)[0])
        with agent.db() as c:self.assertEqual(1,c.execute('SELECT count(*) FROM shop_delivery_confirms').fetchone()[0])
        self.publish('hidden');self.assertEqual(422,self.http(path,fields)[0])
        self.assertEqual('paid',self.order()['state'])

    def test_staff_can_upload_but_not_publish_and_cancel_hides_customer_photo(self):
        self.pay();link=self.assign()
        self.assertEqual(303,self.http(link,dict(action='change_stage',order_id='a'*32,version='1',stage='shipping'))[0])
        self.assertEqual(303,self.http(link,dict(action='change_stage',order_id='a'*32,version='2',stage='delivered'))[0])
        self.assertEqual(303,self.upload(link)[0]);photo=self.photo()
        self.assertEqual(422,self.http(link,dict(action='publish_delivery_photo',order_id='a'*32))[0])
        photo=self.publish();self.notify(notification('a'*32,state='9'))
        self.assertEqual(403,self.raw(delivery_photos.link(photo))[0])
        self.assertNotIn('name="photo_confirmed"',self.http('/invitation-orders/'+('a'*32)+'/receipt')[2])
        self.assertEqual(200,self.raw(delivery_photos.link(photo,'admin'))[0])

    def test_invalid_oversized_animated_photo_and_before_delivery_are_rejected(self):
        self.pay();self.assertEqual(422,self.upload()[0])
        self.post_admin(action='change_stage',order_id='a'*32,stage='shipping',version='1');self.post_admin(action='change_stage',order_id='a'*32,stage='delivered',version='2')
        self.assertEqual(422,self.upload(content=b'<svg onload="alert(1)"></svg>')[0])
        with self.assertRaises(ValueError):delivery_photos.normalized(b'x'*(delivery_photos.MAX_UPLOAD+1))
        output=io.BytesIO();frames=[Image.new('RGB',(5,5),'red'),Image.new('RGB',(5,5),'blue')];frames[0].save(output,'WEBP',save_all=True,append_images=frames[1:])
        self.assertEqual(422,self.upload(content=output.getvalue())[0])
        self.assertEqual(403,self.raw(delivery_photos.link({'id':'c'*32,'order_id':'a'*32},lifetime=-1))[0])

    def test_settlement_partial_full_request_cancel_and_formula_safety(self):
        self.pay(sender='=HYPERLINK("https://example.test")');self.notify(notification('a'*32,'9001','70',10000,orig_mul_no='1234',orig_price='93500'))
        with agent.db() as c:content,name=shop_settlement.workbook(c,{})
        book=load_workbook(io.BytesIO(content));row=list(book['주문정산'].values)[1]
        self.assertEqual((93500,93500,10000,83500,0),tuple(row[9:14]));self.assertEqual('s',book['주문정산']['F2'].data_type)
        self.notify(notification('a'*32,state='9'))
        with agent.db() as c:_,_,rows=shop_settlement.entries(c,{})
        self.assertEqual((93500,0),tuple(rows[0][11:13]))
        self.mock_payment.return_value=('1235','https://payapp.kr/test')
        self.assertEqual(303,self.place('b'*32)[0]);self.notify(notification('b'*32,'1235','8'))
        with agent.db() as c:_,_,rows=shop_settlement.entries(c,{})
        pending=next(r for r in rows if r[0]=='b'*32);self.assertEqual((0,0,0),tuple(pending[10:13]))
        self.assertEqual(403,self.raw(self.admin[:-1]+('0' if self.admin[-1]!='0' else '1')+'/settlement.xlsx')[0])
        self.assertEqual(200,self.raw(self.admin+'/settlement.xlsx')[0])

    def test_settlement_unique_ledger_refs_and_legacy_invitation_exclusion(self):
        self.pay();self.pay('b'*32,'1235')
        with agent.db() as c:
            c.execute("UPDATE shop_handoff SET state='acked',receipt=?",(json.dumps({'ledger':'shared-ledger','production':'shared'}),))
            # ACK updates never create additional settlement rows for one order.
            _,_,rows=shop_settlement.entries(c,{})
            self.assertEqual(2,len(rows));self.assertTrue(all(r[17]=='확인 필요' for r in rows))
            c.execute('UPDATE invitation_orders SET job=? WHERE id=?',('legacy-invitation','b'*32))
            _,_,rows=shop_settlement.entries(c,{})
            self.assertEqual(1,len(rows))

    def test_diagnostics_redacts_secrets_uses_local_mock_only_and_download_protected(self):
        self.pay();sentinel='DO-NOT-LEAK-merchant-secret'
        with patch.dict(os.environ,{'SOLAPI_API_KEY':sentinel,'SOLAPI_API_SECRET':sentinel,'INVITATION_PAYAPP_REQUEST_KEY':sentinel}),patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('outbound forbidden')):
            self.post_admin(action='run_shop_diagnostics')
            with agent.db() as c:row=c.execute('SELECT * FROM shop_diagnostic_runs').fetchone()
            report=json.loads(row['report']);self.assertEqual('passed',report['result'],report);self.assertEqual(0,report['external_requests'])
            self.assertEqual('not_run',report['live_payment']);self.assertNotIn(sentinel,row['report'])
            status,_,content=self.raw(self.admin+'/diagnostics.json?run='+str(row['id']));self.assertEqual(200,status);self.assertNotIn(sentinel.encode(),content)
        self.assertEqual('paid',self.order()['state'])

    def test_assignment_photo_confirmation_diagnostics_survive_backup_restore(self):
        self.pay();link=self.assign();self.post_admin(action='change_stage',order_id='a'*32,stage='shipping',version='1');self.post_admin(action='change_stage',order_id='a'*32,stage='delivered',version='2')
        self.upload(link);photo=self.publish();self.post_admin(action='run_shop_diagnostics')
        target=Path(self.tmp.name)/'backup';scheduled_backups.snapshot(self.root,target);restored=Path(self.tmp.name)/'restored';manage_data.transfer(target,restored,restore=True)
        self.assertEqual(delivery_photos.photo_path(self.root,'a'*32,photo['id']).read_bytes(),delivery_photos.photo_path(restored,'a'*32,photo['id']).read_bytes())
        with sqlite3.connect(restored/'state.sqlite3') as c:
            self.assertEqual(1,c.execute('SELECT count(*) FROM shop_assignments').fetchone()[0]);self.assertEqual('public',c.execute('SELECT state FROM shop_delivery_photos').fetchone()[0]);self.assertEqual(1,c.execute('SELECT count(*) FROM shop_diagnostic_runs').fetchone()[0])

    def test_database_role_restriction_overrides_environment_staff_id(self):
        self.pay();link=self.assign()
        with agent.db() as c:c.execute("INSERT INTO staff VALUES ('staff','blocked',?)",(time.time(),))
        self.assertEqual(403,self.http(link)[0])
        with agent.db() as c:self.assertNotIn('staff',shop_dispatch.roster(c))

    def test_photo_tampering_backup_failure_and_invalid_confirmation_token(self):
        self.delivered();self.upload();photo=self.publish()
        page=self.http('/invitation-orders/'+('a'*32)+'/receipt')[2]
        fields={name:re.search('name="'+name+'" value="([^"]+)"',page)[1] for name in ('photo_id','photo_version','photo_expiry','photo_signature')}
        fields.update(photo_confirmed='yes',photo_signature='잘못된서명')
        self.assertEqual(422,self.http('/invitation-orders/'+('a'*32)+'/delivery-confirm',fields)[0])
        delivery_photos.photo_path(self.root,'a'*32,photo['id']).write_bytes(b'corrupt')
        self.assertEqual(503,self.raw(delivery_photos.link(photo))[0])
        with self.assertRaises(ValueError):scheduled_backups.snapshot(self.root,Path(self.tmp.name)/'corrupt-backup')

    def test_cancelled_assignment_cannot_be_accepted(self):
        self.pay();link=self.assign();self.notify(notification('a'*32,state='9'))
        self.assertEqual(422,self.http(link,dict(action='accept_assignment',order_id='a'*32,assignment_version='1',handover_note='인수 확인',handover_checked='yes'))[0])

    def test_schedule_can_open_older_order_outside_recent_200(self):
        self.pay()
        original=self.order()
        with agent.db() as c:
            for index in range(201):
                order_id=format(index+1000,'032x')
                c.execute('INSERT INTO invitation_orders VALUES (?,?,?,?,?,?,?,?)',(order_id,self.offer,93500,'2099-01-01T00:00:00+09:00',original['details'],'waiting',None,None))
        selected=self.http(self.admin+'?order='+('a'*32))
        self.assertEqual(200,selected[0]);self.assertIn('data-shop-order="'+('a'*32)+'"',selected[2])
        self.assertIn('?order='+('a'*32)+'#order-',selected[2])
        self.assertEqual(422,self.http(self.admin+'?order=bad')[0])
