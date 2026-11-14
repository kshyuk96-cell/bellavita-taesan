import csv
import hashlib
import http.client
import io
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from PIL import Image
from test_shop import ShopFixture,notification,ORIGIN,SECRET
import agent
import assignment_batches
import order_amendments
import order_handoff
import order_messages
import production_files
import shop_receiver
import shop_sales
import shop_service
import scheduled_backups
import manage_data


def photo_bytes():
    output=io.BytesIO();Image.new('RGB',(300,200),'#a5b59a').save(output,'JPEG');return output.getvalue()


class GrowthTest(ShopFixture):
    def pay(self,attempt='a'*32,number='1234',**fields):
        with agent.db() as c:
            product=json.loads(c.execute('SELECT details FROM shop_offers WHERE id=?',(self.offer,)).fetchone()[0]);product.pop('wedding_photo',None)
            c.execute('UPDATE shop_offers SET details=? WHERE id=?',(json.dumps(product),self.offer))
        self.mock_payment.return_value=(number,'https://payapp.kr/test')
        with patch.dict('os.environ',{'SHOP_PRODUCTION_REVIEW_REQUIRED':'1'}):self.assertEqual(303,self.place(attempt,**fields)[0])
        self.notify(notification(attempt,number,amount=json.loads(self.order(attempt)['details'])['amount']))

    def raw(self,path,body=None,content_type=None):
        client=http.client.HTTPConnection(*self.server.server_address,timeout=10);headers={'Origin':ORIGIN}
        if content_type:headers['Content-Type']=content_type
        client.request('POST' if body is not None else 'GET',path,body,headers);response=client.getresponse();result=(response.status,response.read());client.close();return result

    def multipart(self,fields,name,content):
        boundary='GROWTH-BOUNDARY';body=b''
        for key,value in fields.items():body+=f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        body+=f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="sample.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+content+f'\r\n--{boundary}--\r\n'.encode()
        return self.raw(self.admin,body,'multipart/form-data; boundary='+boundary)

    def artifact(self):
        self.post_admin(action='generate_artifact',order_id='a'*32,width_mm='100',height_mm='300',dpi='150')
        with agent.db() as c:
            artifact=production_files.latest(c,'a'*32);check=production_files.latest_review(c,'a'*32)
        self.post_admin(action='review_artifact',order_id='a'*32,artifact_version=str(artifact['version']),artifact_hash=artifact['source_hash'],artifact_review_revision=str(check['id'] if check else 0),artifact_decision='approved',artifact_reviewer='검수자',artifact_note='정정 문구·중량·규격 검수',artifact_compared='yes')
        return artifact

    def request(self,key='c'*32):
        with agent.db() as c:revision=order_amendments.revision(c,'a'*32)
        fields=dict(action='request_amendment',order_id='a'*32,content_revision=str(revision),amendment_attempt=key,new_sender='정정 보내는 분',new_message='승인한 새 축하 문구',amendment_note='고객 요청 대조')
        self.post_admin(**fields);return fields

    def decide(self,key='c'*32,decision='approved'):
        with agent.db() as c:revision=order_amendments.revision(c,'a'*32)
        self.post_admin(action='review_amendment',order_id='a'*32,amendment_id=key,content_revision=str(revision),amendment_decision=decision,amendment_reviewer='정정 검토자',amendment_review_note='원문과 변경 내용을 확인',amendment_checked='yes')

    def transport(self,action,fields):
        with agent.db() as c:
            if action=='claim':return order_handoff.claim(c,fields)
            result,status=order_handoff.acknowledge(c,fields)
            if status!=200:raise shop_receiver.AckConflict(result['error'])
            return result

    def file(self,path):
        # Same endpoint and auth used by the production receiver; local HTTP only.
        client=http.client.HTTPConnection(*self.server.server_address,timeout=10);client.request('GET',path,headers={'Authorization':'Bearer '+('growth-key-'*4)})
        reply=client.getresponse();data=reply.read();status=reply.status;client.close();self.assertEqual(200,status,data[:100]);return data

    def drain(self,root):
        for _ in range(15):
            if not shop_receiver.run_once(root,'factory',self.transport,self.file):return
        self.fail('outbox did not drain')

    def test_amendment_keeps_original_money_requires_new_review_and_receiver_snapshot(self):
        self.pay();before=self.order();self.artifact();root=Path(self.tmp.name)/'factory'
        with patch.dict('os.environ',{'SHOP_HANDOFF_ENABLED':'1','SHOP_HANDOFF_SECRET':'growth-key-'*4}):
            self.drain(root);self.request()
            with agent.db() as c:self.assertFalse(production_files.allowed(c,self.order()))
            self.drain(root)
            with sqlite3.connect(root/'receiver.sqlite3') as c:self.assertEqual('held',c.execute('SELECT state FROM production_queue').fetchone()[0])
            self.decide();self.assertEqual(before['details'],self.order()['details']);self.assertEqual(before['payment_number'],self.order()['payment_number'])
            self.assertEqual(422,self.http(self.admin,dict(action='change_stage',order_id='a'*32,stage='shipping',version='1'))[0])
            artifact=self.artifact();self.assertEqual(2,artifact['version']);self.drain(root)
        with sqlite3.connect(root/'receiver.sqlite3') as c:
            production=json.loads(c.execute('SELECT details FROM production_queue').fetchone()[0]);ledger=json.loads(c.execute('SELECT details FROM order_ledger').fetchone()[0])
            self.assertEqual('승인한 새 축하 문구',production['message']);self.assertEqual(json.loads(before['details'])['message'],ledger['message']);self.assertEqual(1,c.execute('SELECT count(*) FROM production_queue').fetchone()[0])
            self.assertEqual('pending',c.execute('SELECT state FROM production_queue').fetchone()[0])

    def test_rejection_releases_prior_approved_artifact_and_old_consumer_skips_new_schema(self):
        self.pay();self.artifact();root=Path(self.tmp.name)/'factory'
        with patch.dict('os.environ',{'SHOP_HANDOFF_ENABLED':'1','SHOP_HANDOFF_SECRET':'growth-key-'*4}):
            self.drain(root);self.request();self.drain(root);self.decide(decision='rejected')
            with agent.db() as c:self.assertIsNone(order_handoff.claim(c,{'consumer':'old','capabilities':['production-artifact-v1']})['job'])
            self.drain(root)
        with sqlite3.connect(root/'receiver.sqlite3') as c:self.assertEqual('pending',c.execute('SELECT state FROM production_queue').fetchone()[0])
        with agent.db() as c:self.assertTrue(production_files.allowed(c,self.order()))

    def test_request_retries_stale_decisions_and_shipping_restriction(self):
        self.pay();fields=self.request();self.post_admin(**fields)
        with agent.db() as c:self.assertEqual(1,c.execute('SELECT count(*) FROM shop_amendments').fetchone()[0])
        self.assertEqual(422,self.http(self.admin,dict(fields,new_message='같은 요청번호 내용 변조'))[0])
        self.assertEqual(422,self.http(self.admin,dict(action='review_amendment',order_id='a'*32,amendment_id='c'*32,content_revision='0',amendment_decision='approved',amendment_reviewer='검토',amendment_review_note='대조',amendment_checked='yes'))[0])
        self.decide();self.artifact();self.post_admin(action='change_stage',order_id='a'*32,stage='shipping',version='1')
        self.assertEqual(422,self.http(self.admin,dict(fields,amendment_attempt='d'*32))[0])

    def test_content_request_during_message_preparation_prevents_send(self):
        self.pay();calls=[]
        def api(path,body=None):
            calls.append(path)
            if path.startswith('/storage/'):
                self.request();return {'fileId':'MOCK_IMAGE'}
            self.fail('message was sent after content request')
        env={'SHOP_MESSAGES_ENABLED':'1','SOLAPI_API_KEY':'mock','SOLAPI_API_SECRET':'mock','SOLAPI_SENDER':'0212345678'}
        with patch.dict('os.environ',env),patch.object(order_messages,'api',side_effect=api):order_messages.process_one(agent.db,self.root)
        self.assertEqual(1,len(calls))
        with agent.db() as c:self.assertEqual('held',c.execute('SELECT state FROM shop_messages').fetchone()[0])

    def test_service_case_progression_photo_private_and_no_payment_or_production_side_effect(self):
        self.pay();before=self.order()['details']
        fields=dict(action='create_service_case',order_id='a'*32,case_attempt='e'*32,case_kind='redelivery',case_reason='설치 위치 확인 요청')
        self.post_admin(**fields);self.post_admin(**fields)
        with agent.db() as c:handoffs=c.execute('SELECT count(*) FROM shop_handoff').fetchone()[0]
        for version,state in [(1,'assigned'),(2,'in_progress'),(3,'resolved'),(4,'closed')]:
            if version==3:
                self.assertEqual(303,self.multipart(dict(action='upload_service_photo',order_id='a'*32,case_id='e'*32,photo_note='AS 확인 사진'),'photo',photo_bytes())[0])
            self.post_admin(action='update_service_case',order_id='a'*32,case_id='e'*32,case_version=str(version),case_state=state,case_assignee='staff',case_note='처리 근거')
        self.assertEqual(422,self.http(self.admin,dict(action='update_service_case',order_id='a'*32,case_id='e'*32,case_version='5',case_state='in_progress',case_assignee='staff',case_note='종결 변경'))[0])
        with agent.db() as c:
            self.assertEqual(1,c.execute('SELECT count(*) FROM shop_service_cases').fetchone()[0]);self.assertEqual(5,c.execute('SELECT count(*) FROM shop_service_events').fetchone()[0]);photo=c.execute('SELECT * FROM shop_service_photos').fetchone()
            self.assertEqual(handoffs,c.execute('SELECT count(*) FROM shop_handoff').fetchone()[0])
        self.assertEqual(200,self.raw(shop_service.photo_link(photo))[0]);self.assertEqual(before,self.order()['details']);self.assertEqual(1,self.mock_payment.call_count)

    def csv(self,rows,key='f'*32):
        output=io.StringIO();writer=csv.writer(output);writer.writerow(['order_id','event_time','assignee','assignment_version','note']);writer.writerows(rows)
        status,_=self.multipart({'action':'preview_assignment_csv','batch_attempt':key},'csv',output.getvalue().encode())
        self.assertEqual(303,status)
        with agent.db() as c:return c.execute('SELECT * FROM shop_assignment_batches WHERE id=?',(key,)).fetchone()

    def test_csv_preview_explicit_selection_invalid_duplicate_and_stale(self):
        self.pay();self.pay('b'*32,'1235');when=json.loads(self.order()['details'])['event_time']
        batch=self.csv([['a'*32,when,'staff','0','확인'],['b'*32,'2030-01-01T12:00:00+09:00','staff','0','잘못된 날짜']])
        plan=json.loads(batch['plan']);self.assertFalse(plan[0]['errors']);self.assertTrue(plan[1]['errors'])
        with agent.db() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM shop_assignments').fetchone()[0])
        self.post_admin(action='apply_assignment_csv',batch_id='f'*32,batch_checked='yes',batch_row_1='yes',batch_row_2='yes')
        self.post_admin(action='apply_assignment_csv',batch_id='f'*32,batch_checked='yes',batch_row_1='yes')
        with agent.db() as c:self.assertEqual(1,c.execute('SELECT count(*) FROM shop_assignment_history').fetchone()[0])
        batch=self.csv([['b'*32,when,'staff','0','확인']],key='1'*32)
        self.post_admin(action='assign_delivery',order_id='b'*32,assignee='admin',assignment_version='0',assignment_note='다른 관리자 배정')
        self.post_admin(action='apply_assignment_csv',batch_id='1'*32,batch_checked='yes',batch_row_1='yes')
        with agent.db() as c:self.assertEqual('stale',json.loads(c.execute('SELECT result FROM shop_assignment_batches WHERE id=?',('1'*32,)).fetchone()[0])[0]['status'])
        batch=self.csv([['a'*32,when,'staff','1',''],['a'*32,when,'staff','1','']],key='2'*32)
        self.assertTrue(all('중복 주문 행' in p['errors'] for p in json.loads(batch['plan'])))
        self.assertEqual(200,self.raw(self.admin+'/assignment-batch.csv?batch='+('f'*32))[0])

    def test_csv_report_formula_safety_and_oversize_rejection(self):
        self.pay();when=json.loads(self.order()['details'])['event_time'];batch=self.csv([['a'*32,when,'=DANGEROUS()','0','']])
        with agent.db() as c:report=assignment_batches.report(c,batch['id']).decode('utf-8-sig')
        self.assertIn("'=DANGEROUS()",report)
        with agent.db() as c:
            with self.assertRaises(ValueError):assignment_batches.preview(c,{'batch_attempt':'d'*32,'_csv_bytes':b'x'*131073})

    def test_sales_aggregates_reconcile_partial_full_cancellations_and_snapshot_metadata(self):
        self.pay();self.pay('b'*32,'1235');self.notify(notification('a'*32,'9001','70',10000,orig_mul_no='1234',orig_price='93500'));self.notify(notification('b'*32,'1235','9'))
        with agent.db() as c:
            report=shop_sales.aggregate(c,{});content,_=shop_sales.workbook(c,{})
        self.assertTrue(report['reconciled']);self.assertEqual([187000,187000,103500,83500,0],report['totals'][3:])
        for grouped in report['groups'].values():self.assertEqual(83500,sum(v[6] for v in grouped.values()))
        self.assertEqual(4,len(load_workbook(io.BytesIO(content)).sheetnames));self.assertEqual(200,self.raw(self.admin+'/sales.xlsx')[0])

    def test_new_audits_and_service_photo_survive_backup(self):
        self.pay();self.request();self.decide();self.artifact()
        self.post_admin(action='create_service_case',order_id='a'*32,case_attempt='e'*32,case_kind='as',case_reason='점검')
        self.multipart(dict(action='upload_service_photo',order_id='a'*32,case_id='e'*32,photo_note='보존 확인'),'photo',photo_bytes())
        when=json.loads(self.order()['details'])['event_time'];self.csv([['a'*32,when,'staff','0','확인']])
        target=Path(self.tmp.name)/'snapshot';scheduled_backups.snapshot(self.root,target);restored=Path(self.tmp.name)/'restored';manage_data.transfer(target,restored,restore=True)
        with sqlite3.connect(restored/'state.sqlite3') as c:
            for table in ('shop_amendments','shop_service_cases','shop_service_photos','shop_assignment_batches'):self.assertEqual(1,c.execute('SELECT count(*) FROM '+table).fetchone()[0])

    def test_reissuing_legacy_offer_metadata_does_not_create_new_links(self):
        with agent.db() as c:
            for row in c.execute('SELECT id,details FROM shop_offers').fetchall():
                data=json.loads(row['details']);data.pop('event_name',None);data.pop('product_title',None)
                c.execute('UPDATE shop_offers SET details=? WHERE id=?',(json.dumps(data),row['id']))
        self.post_admin(**self.event)
        with agent.db() as c:self.assertEqual(2,c.execute('SELECT count(*) FROM shop_offers').fetchone()[0])

    def test_pending_content_does_not_suppress_cancellation_notice_and_can_be_rejected(self):
        self.pay();self.request();self.notify(notification('a'*32,state='9'));sent=[]
        def api(path,body=None):
            if path.startswith('/storage/'):return {'fileId':'MOCK_IMAGE'}
            sent.append(body['messages'][0]);return {'groupInfo':{'groupId':'G'},'messageList':[{'messageId':'M'}]}
        with patch.dict('os.environ',{'SHOP_MESSAGES_ENABLED':'1','SOLAPI_API_KEY':'mock','SOLAPI_API_SECRET':'mock','SOLAPI_SENDER':'0212345678'}),patch.object(order_messages,'api',side_effect=api):order_messages.process_one(agent.db,self.root)
        self.assertEqual(1,len(sent));self.assertIn('결제 취소',sent[0]['text']);self.decide(decision='rejected')
        with agent.db() as c:self.assertIsNone(order_amendments.pending(c,'a'*32))
        self.assertEqual('cancelled',self.order()['state'])

    def test_weight_and_second_price_sales_match_server_computed_order_amounts(self):
        self.pay();first=self.offer;self.offer=self.offers[1]['id'];self.pay('b'*32,'1235',choice0='2');self.offer=first
        self.notify(notification('b'*32,'9001','70',10000,orig_mul_no='1235',orig_price='109000'))
        with agent.db() as c:report=shop_sales.aggregate(c,{})
        self.assertEqual([202500,202500,10000,192500,0],report['totals'][3:]);self.assertEqual({'10kg','20kg'},set(report['groups']['weight']))
