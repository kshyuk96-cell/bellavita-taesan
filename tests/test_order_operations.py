import datetime as dt
import http.client
import io
import json
import os
import time
import unittest
from urllib.parse import parse_qs
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from PIL import Image

from test_shop import ShopFixture, CONFIG, ORIGIN, SECRET, notification
import agent
import cash_receipts
import order_messages
import order_ops
import order_sample
import manage_data


class OrderOperationsTest(ShopFixture):
    def paid(self, cash=False, method='6', tax='TG02'):
        self.post_admin(action='save_notifications', staff_phones='01022223333,01033334444,01022223333')
        with agent.db() as c:
            product = dict(self.product, cash_tax_type=tax)
            c.execute('UPDATE shop_offers SET details=? WHERE id=?', (json.dumps(product), self.offer))
        self.assertEqual(303, self.place(**({'cash_receipt_type':'personal'} if cash else {}))[0])
        self.notify(notification('a'*32, pay_type=method, pay_date=dt.datetime.now(order_ops.SEOUL).strftime('%Y%m%d%H%M%S')))

    def rows(self, table):
        with agent.db() as c:
            return c.execute('SELECT * FROM '+table).fetchall()

    def message_env(self):
        return patch.dict(os.environ, {'SHOP_MESSAGES_ENABLED':'1', 'SOLAPI_API_KEY':'test-key',
                                      'SOLAPI_API_SECRET':'test-secret', 'SOLAPI_SENDER':'0212345678'})

    def receipt_env(self):
        return patch.dict(os.environ, {'SHOP_CASH_RECEIPT_ENABLED':'1','SHOP_CASH_RECEIPT_OWNER':'shop'})

    def test_paid_notifications_and_three_stages_are_idempotent(self):
        self.paid()
        self.notify(notification('a'*32))
        self.assertEqual(3,len(self.rows('shop_messages')))
        self.assertEqual('confirmed',self.rows('shop_fulfillment')[0]['stage'])
        self.assertEqual(422,self.http(self.admin,dict(action='change_stage',order_id='a'*32,version='1',stage='delivered'))[0])
        self.post_admin(action='change_stage',order_id='a'*32,version='1',stage='shipping')
        self.assertEqual(422,self.http(self.admin,dict(action='change_stage',order_id='a'*32,version='1',stage='shipping'))[0])
        self.post_admin(action='change_stage',order_id='a'*32,version='2',stage='delivered')
        self.post_admin(action='change_stage',order_id='a'*32,version='3',stage='delivered')
        self.assertEqual(9,len(self.rows('shop_messages')))
        self.assertEqual(3,len(self.rows('shop_stage_history')))
        self.assertIn('배송 완료',self.http('/invitation-orders/'+'a'*32+'/receipt')[2])

    def test_staff_delivery_link_is_scoped_and_origin_checked(self):
        self.paid()
        path=order_ops.signed_path('delivery','a'*32,SECRET)
        self.assertEqual(200,self.http(path)[0])
        fields=dict(action='change_stage',order_id='a'*32,version='1',stage='shipping')
        self.assertEqual(403,self.http(path,fields,origin='https://evil.test')[0])
        self.assertEqual(422,self.http(path,dict(fields,order_id='b'*32))[0])
        self.assertEqual(303,self.http(path,fields)[0])
        self.assertEqual(403,self.http(path[:-1]+('a' if path[-1]!='a' else 'b'))[0])
        expired=order_ops.signed_path('delivery','a'*32,SECRET,lifetime=-1)
        self.assertEqual(403,self.http(expired)[0])

    def test_cancellation_holds_dispatch_and_deduplicates_cancel_notice(self):
        self.paid()
        cancel=notification('a'*32,state='9')
        self.notify(cancel);self.notify(cancel)
        rows=self.rows('shop_messages')
        self.assertEqual(3,sum(row['state']=='held' for row in rows))
        self.assertEqual(3,sum(row['state']=='queued' for row in rows))
        self.assertEqual(422,self.http(self.admin,dict(action='change_stage',order_id='a'*32,version='1',stage='shipping'))[0])

    def test_sample_reflects_saved_text_and_private_signature(self):
        self.paid()
        row=dict(self.order());data=json.loads(row['details'])
        data['sender']='보내는분'*28;data['message']='축하합니다'*40
        row['details']=json.dumps(data)
        path=order_sample.sample(self.root,row)
        content=path.read_bytes()
        self.assertLessEqual(len(content),200000)
        with Image.open(io.BytesIO(content)) as image:
            self.assertEqual('JPEG',image.format);self.assertEqual((900,1200),image.size)
        self.assertEqual(path,order_sample.sample(self.root,row))
        changed=json.loads(row['details']);changed['message']='다른 문구';row['details']=json.dumps(changed)
        self.assertNotEqual(content,order_sample.sample(self.root,row).read_bytes())
        signed=order_ops.signed_path('sample','a'*32,SECRET)
        client=http.client.HTTPConnection(*self.server.server_address)
        client.request('GET',signed);reply=client.getresponse()
        self.assertEqual(200,reply.status);self.assertEqual('image/jpeg',reply.getheader('Content-Type'))
        self.assertTrue(reply.read().startswith(b'\xff\xd8'));client.close()
        self.assertEqual(403,self.http(order_ops.signed_path('sample','a'*32,SECRET,lifetime=-1))[0])

    def test_mms_attachments_acceptance_and_handset_delivery(self):
        self.paid();uploaded=[];sent=[]
        def api(path,body=None):
            if path.startswith('/storage/'):
                uploaded.append(body['file']);return {'fileId':'TEST_IMAGE'}
            if path.startswith('/messages/v4/send'):
                item=body['messages'][0];sent.append(item)
                return {'groupInfo':{'groupId':'G'+item['to']},'messageList':[{'messageId':'M'+item['to'],'statusCode':'2000'}]}
            value=path.split('value=')[1]
            phone=value[1:]
            return {'messageList':{value:{'messageId':value,'groupId':'G'+phone,'to':phone,'statusCode':'4000'}}}
        with self.message_env(),patch.object(order_messages,'api',side_effect=api):
            for _ in range(3):order_messages.process_one(agent.db,self.root)
            self.assertEqual(3,len(sent));self.assertEqual(1,len(set(uploaded)))
            self.assertTrue(all(item['imageId']=='TEST_IMAGE' for item in sent))
            self.assertTrue(any('/shop/delivery/' in item['text'] for item in sent))
            self.assertTrue(any('/receipt' in item['text'] for item in sent))
            self.assertTrue(all(row['state']=='accepted' for row in self.rows('shop_messages')))
            with agent.db() as c:c.execute('UPDATE shop_messages SET checked=0')
            for _ in range(3):order_messages.process_one(agent.db,self.root)
            self.assertTrue(all(row['state']=='delivered' for row in self.rows('shop_messages')))

    def test_message_timeout_is_not_resent_and_can_be_reconciled(self):
        self.paid()
        def api(path,body=None):
            if path.startswith('/storage/'):return {'fileId':'TEST_IMAGE'}
            raise TimeoutError('unknown acceptance')
        with self.message_env(),patch.object(order_messages,'api',side_effect=api) as mock:
            for _ in range(4):order_messages.process_one(agent.db,self.root)
            self.assertEqual(6,mock.call_count)
        rows=self.rows('shop_messages');self.assertTrue(all(row['state']=='uncertain' for row in rows))
        job=rows[0]['id']
        self.assertEqual(422,self.http(self.admin,dict(action='retry_message',message_id=job,checked_failure='yes'))[0])
        self.post_admin(action='reconcile_message',message_id=job,message_decision='failed',
                        message_note='테스트 발송사에서 미접수 확인',message_compared='yes')
        self.post_admin(action='retry_message',message_id=job,checked_failure='yes')
        self.assertEqual(1,sum(row['state']=='queued' for row in self.rows('shop_messages')))

    def test_disabled_and_restart_do_not_send_duplicates(self):
        self.paid()
        with patch.dict(os.environ,{'SHOP_MESSAGES_ENABLED':'0'}),patch.object(order_messages,'api') as mock:
            order_messages.process_one(agent.db,self.root);mock.assert_not_called()
        with agent.db() as c:
            c.execute("UPDATE shop_messages SET state='sending' WHERE audience='customer'")
            order_ops.recover(c)
        self.assertEqual(1,sum(row['state']=='uncertain' for row in self.rows('shop_messages')))

    def test_cash_receipt_issue_partial_cancel_reissue_and_full_cancel(self):
        self.paid(cash=True);calls=[]
        def request(fields):
            calls.append(fields)
            return {'state':'1','cashstno':str(1000+len(calls)),'receipt_no':'APPROVED', 'cashsturl':'https://payapp.kr/receipt'}
        with self.receipt_env(),patch.object(cash_receipts,'request',side_effect=request):
            cash_receipts.process_one(agent.db);cash_receipts.process_one(agent.db)
            self.assertEqual(1,len(calls));self.assertEqual(93500,calls[0]['amt_tot'])
            self.assertEqual(0,calls[0]['amt_tax']);self.assertEqual('01012345678',calls[0]['id_info'])
            self.notify(notification('a'*32,'9001','70',10000,orig_mul_no='1234',orig_price='93500'))
            cash_receipts.process_one(agent.db);cash_receipts.process_one(agent.db)
            self.assertEqual('cashStCancel',calls[1]['cmd']);self.assertEqual(83500,calls[2]['amt_tot'])
            self.notify(notification('a'*32,state='9'))
            cash_receipts.process_one(agent.db);cash_receipts.process_one(agent.db)
            self.assertEqual(4,len(calls));self.assertTrue(all(row['state']=='cancelled' for row in self.rows('shop_cash_receipts')))

    def test_card_wallet_unconfigured_tax_and_duplicate_receipts_are_excluded(self):
        self.paid(cash=True,method='1')
        with self.receipt_env(),patch.object(cash_receipts,'request') as mock:
            cash_receipts.process_one(agent.db);mock.assert_not_called()
        self.assertIn('카드',self.http(self.admin)[2])
        with agent.db() as c:c.execute("UPDATE shop_receipt_payments SET pay_type='6'")
        with patch.dict(os.environ,{'SHOP_CASH_RECEIPT_ENABLED':'1','SHOP_CASH_RECEIPT_OWNER':'provider'}),patch.object(cash_receipts,'request') as mock:
            cash_receipts.process_one(agent.db);mock.assert_not_called()
        with agent.db() as c:
            data=json.loads(self.order()['details']);data['product_snapshot']['cash_tax_type']=''
            c.execute('UPDATE invitation_orders SET details=? WHERE id=?',(json.dumps(data),'a'*32))
        with self.receipt_env(),patch.object(cash_receipts,'request') as mock:
            cash_receipts.process_one(agent.db);mock.assert_not_called()

    def test_receipt_timeout_recovery_and_reconciliation(self):
        self.paid(cash=True,tax='TG01')
        with self.receipt_env(),patch.object(cash_receipts,'request',side_effect=TimeoutError()) as mock:
            cash_receipts.process_one(agent.db);cash_receipts.process_one(agent.db)
            mock.assert_called_once();fields=mock.call_args.args[0]
            self.assertEqual(93500,fields['amt_sup']+fields['amt_tax'])
        self.assertEqual('uncertain',self.rows('shop_cash_receipts')[0]['state'])
        self.post_admin(action='reconcile_receipt',order_id='a'*32,generation='1',receipt_decision='issued',
                        receipt_compared='yes',receipt_note='테스트 페이앱 승인 원본 대조',cashstno='123',receipt_no='OK',receipt_amount='93500')
        self.assertEqual('issued',self.rows('shop_cash_receipts')[0]['state'])
        with agent.db() as c:
            c.execute("UPDATE shop_cash_receipts SET state='cancelling'");order_ops.recover(c)
        self.assertEqual('uncertain',self.rows('shop_cash_receipts')[0]['state'])

    def test_receipt_input_validation(self):
        self.assertEqual(422,self.place(cash_receipt_type='business',cash_receipt_identifier='123')[0])
        self.assertEqual(422,self.place(cash_receipt_type='unknown')[0])
        self.assertEqual(303,self.place(cash_receipt_type='self')[0])
        self.assertEqual({'kind':'self','identifier':'0100001234'},json.loads(self.order()['details'])['cash_receipt'])

    def test_expired_partial_receipt_keeps_original_for_merchant_adjustment(self):
        self.paid(cash=True)
        with self.receipt_env(),patch.object(cash_receipts,'request',return_value={
            'cashstno':'123','receipt_no':'OK','cashsturl':'https://payapp.kr/receipt'}) as mock:
            cash_receipts.process_one(agent.db)
            with agent.db() as c:c.execute('UPDATE shop_receipt_payments SET paid_at=?',
                ((dt.datetime.now(order_ops.SEOUL)-dt.timedelta(days=7)).isoformat(),))
            self.notify(notification('a'*32,'9001','70',10000,orig_mul_no='1234',orig_price='93500'))
            cash_receipts.process_one(agent.db)
            mock.assert_called_once()
        self.assertEqual('held',self.rows('shop_cash_receipts')[0]['state'])

    def test_two_receipt_workers_issue_once(self):
        self.paid(cash=True)
        def issue(fields):
            time.sleep(.15)
            return {'cashstno':'123','receipt_no':'OK','cashsturl':'https://payapp.kr/receipt'}
        with self.receipt_env(),patch.object(cash_receipts,'request',side_effect=issue) as mock:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _:cash_receipts.process_one(agent.db),range(2)))
            mock.assert_called_once()

    def test_backup_restores_notifications_receipts_history_and_sample(self):
        self.paid(cash=True)
        original=order_sample.sample(self.root,self.order())
        with self.receipt_env(),patch.object(cash_receipts,'request',return_value={
            'cashstno':'123','receipt_no':'OK','cashsturl':'https://payapp.kr/receipt'}):
            cash_receipts.process_one(agent.db)
        self.server.shutdown();self.server.server_close();self.thread.join()
        backup=self.root.parent/'ops-backup';restored=self.root.parent/'ops-restored'
        manage_data.transfer(self.root,backup);manage_data.transfer(backup,restored,restore=True)
        import sqlite3
        with sqlite3.connect(restored/'state.sqlite3') as c:
            self.assertEqual(3,c.execute('SELECT COUNT(*) FROM shop_messages').fetchone()[0])
            self.assertEqual('issued',c.execute('SELECT state FROM shop_cash_receipts').fetchone()[0])
            self.assertEqual('confirmed',c.execute('SELECT stage FROM shop_stage_history').fetchone()[0])
        self.assertEqual(original.read_bytes(),(restored/'shop-previews'/original.name).read_bytes())


class AdapterTest(unittest.TestCase):
    def test_solapi_auth_and_storage_json(self):
        with patch.dict(os.environ,{'SOLAPI_API_KEY':'TEST_KEY','SOLAPI_API_SECRET':'TEST_SECRET'}):
            with patch.object(order_messages,'build_opener') as opener:
                opener.return_value.open.return_value=io.BytesIO(b'{"fileId":"TEST_IMAGE"}')
                self.assertEqual({'fileId':'TEST_IMAGE'},order_messages.api('/storage/v1/files',{'file':'BASE64','type':'MMS'}))
                request=opener.return_value.open.call_args.args[0]
                self.assertEqual('https://api.solapi.com/storage/v1/files',request.full_url)
                self.assertEqual({'file':'BASE64','type':'MMS'},json.loads(request.data))
                auth=request.get_header('Authorization')
                self.assertTrue(auth.startswith('HMAC-SHA256 apiKey=TEST_KEY, date='))
                self.assertIn('signature=',auth)

    def test_payapp_cash_receipt_transport_and_timeout_response(self):
        env={'INVITATION_PAYAPP_'+key:value for key,value in CONFIG.items()}
        with patch.dict(os.environ,env),patch.object(cash_receipts,'build_opener') as opener:
            opener.return_value.open.return_value=io.BytesIO(b'state=1&cashstno=123&receipt_no=OK')
            result=cash_receipts.request({'cmd':'cashStRegist','amt_tot':93500,'mul_no':'1234'})
            self.assertEqual('123',result['cashstno'])
            request=opener.return_value.open.call_args.args[0]
            body=parse_qs(request.data.decode())
            self.assertEqual(['cashStRegist'],body['cmd']);self.assertEqual(['93500'],body['amt_tot'])
            self.assertEqual([CONFIG['LINK_KEY']],body['linkkey']);self.assertIn('hash',body)
            opener.return_value.open.return_value=io.BytesIO(b'state=0&errorCode=9040')
            with self.assertRaises(OSError):cash_receipts.request({'cmd':'cashStRegist'})
