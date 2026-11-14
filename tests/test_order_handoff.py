import csv
import http.client
import json
from pathlib import Path
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_shop import ShopFixture, notification
import agent
import manage_data
import order_handoff
import shop_receiver

KEY = 'isolated-factory-handoff-key-' * 2


class HandoffTest(ShopFixture):
    def setUp(self):
        super().setUp()
        self.handoff_env = patch.dict('os.environ', {'SHOP_HANDOFF_ENABLED':'1','SHOP_HANDOFF_SECRET':KEY})
        self.handoff_env.start()
        self.receiver = Path(self.tmp.name)/'factory'

    def tearDown(self):
        self.handoff_env.stop()
        super().tearDown()

    def pay(self, photo=False, sender='벨라비타 대표 홍길동'):
        if not photo:
            product = dict(self.product)
            product.pop('wedding_photo', None)
            with agent.db() as c:
                c.execute('UPDATE shop_offers SET details=? WHERE id=?', (json.dumps(product),self.offer))
        self.assertEqual(303,self.place(sender=sender,cash_receipt_type='business',cash_receipt_identifier='1234567890')[0])
        self.notify(notification('a'*32))

    def api(self, action, fields, secret=KEY):
        client=http.client.HTTPConnection(*self.server.server_address,timeout=5)
        client.request('POST','/shop/handoff/'+action,json.dumps(fields),
                       {'Authorization':'Bearer '+secret,'Content-Type':'application/json'})
        response=client.getresponse();status=response.status;data=json.loads(response.read());client.close()
        return status,data

    def transport(self, action, fields):
        status,result=self.api(action,fields)
        if status==409:raise shop_receiver.AckConflict(result['error'])
        self.assertEqual(200,status,result)
        return result

    def claim(self):
        return self.transport('claim',{'consumer':'factory'})['job']

    def events(self):
        with agent.db() as c:return c.execute('SELECT * FROM shop_handoff ORDER BY event_id').fetchall()

    def local_state(self):
        with sqlite3.connect(self.receiver/'receiver.sqlite3') as c:
            return c.execute('SELECT state FROM production_queue').fetchone()[0]

    def test_photo_review_gate_and_saved_production_payload(self):
        self.pay(photo=True)
        self.assertIsNone(self.claim());self.assertEqual('review',self.events()[0]['state'])
        self.post_admin(action='review_shop_photo',order_id='a'*32,both_faces='yes')
        self.post_admin(action='review_shop_photo',order_id='a'*32,both_faces='yes')
        job=self.claim();self.assertEqual('a'*32,job['payload']['production']['order_number'])
        self.assertEqual(10,job['payload']['production']['rice_kg'])
        self.assertEqual('행복한 결혼을 진심으로 축하합니다',job['payload']['production']['message'])
        self.assertNotIn('cash_receipt',job['payload']['order'])
        self.assertNotIn('1234567890',json.dumps(job['payload']))
        self.assertEqual(1,len(self.events()))

    def test_duplicate_payment_and_receipt_create_one_ledger_and_production_row(self):
        self.pay();self.notify(notification('a'*32));self.notify(notification('a'*32))
        self.assertEqual(1,len(self.events()))
        self.assertTrue(shop_receiver.run_once(self.receiver,'factory',self.transport))
        self.assertFalse(shop_receiver.run_once(self.receiver,'factory',self.transport))
        self.assertEqual('pending',self.local_state());self.assertEqual('acked',self.events()[0]['state'])
        with sqlite3.connect(self.receiver/'receiver.sqlite3') as c:
            self.assertEqual(1,c.execute('SELECT COUNT(*) FROM order_ledger').fetchone()[0])
            self.assertEqual(1,c.execute('SELECT COUNT(*) FROM production_queue').fetchone()[0])

    def test_lease_expiry_redelivery_does_not_duplicate_local_queue(self):
        self.pay();first=self.claim();old_ack=shop_receiver.ingest(self.receiver,first)
        self.assertIsNone(self.claim())
        with agent.db() as c:c.execute('UPDATE shop_handoff SET lease_until=?',(time.time()-1,))
        second=self.claim();self.assertNotEqual(first['lease_token'],second['lease_token'])
        self.assertEqual(409,self.api('ack',old_ack)[0])
        shop_receiver.ingest(self.receiver,second);shop_receiver.acknowledge_pending(self.receiver,self.transport)
        self.assertEqual('pending',self.local_state())
        with sqlite3.connect(self.receiver/'receiver.sqlite3') as c:
            self.assertEqual(1,c.execute('SELECT COUNT(*) FROM received_events').fetchone()[0])

    def test_lost_ack_response_is_recovered_after_restart(self):
        self.pay();lost=[False]
        def transport(action, fields):
            result=self.transport(action,fields)
            if action=='ack' and not lost[0]:lost[0]=True;raise TimeoutError('mock response lost')
            return result
        with self.assertRaises(TimeoutError):shop_receiver.run_once(self.receiver,'factory',transport)
        self.assertEqual('awaiting_ack',self.local_state());self.assertEqual('acked',self.events()[0]['state'])
        snapshot=self.receiver/'snapshots'/'worker.sqlite3'
        shop_receiver.backup(self.receiver,snapshot)
        with sqlite3.connect(snapshot) as c:
            self.assertIsNotNone(c.execute('SELECT ack_json FROM received_events').fetchone()[0])
        with self.assertRaises(ValueError):shop_receiver.backup(self.receiver,snapshot)
        self.assertFalse(shop_receiver.run_once(self.receiver,'factory',self.transport))
        self.assertEqual('pending',self.local_state())

    def test_cancel_between_claim_and_ack_holds_local_production(self):
        self.pay();job=self.claim();shop_receiver.ingest(self.receiver,job)
        self.notify(notification('a'*32,state='9'))
        shop_receiver.acknowledge_pending(self.receiver,self.transport)
        self.assertEqual('held',self.local_state())
        self.assertTrue(shop_receiver.run_once(self.receiver,'factory',self.transport))
        self.assertEqual('held',self.local_state())
        self.assertEqual(2,len(self.events()))

    def test_cancel_after_ack_and_late_ready_cannot_release_hold(self):
        self.pay();job=self.claim();ack=shop_receiver.ingest(self.receiver,job)
        shop_receiver.acknowledge_pending(self.receiver,self.transport)
        cancel=notification('a'*32,'9001','70',10000,orig_mul_no='1234',orig_price='93500')
        self.notify(cancel);self.notify(cancel)
        shop_receiver.run_once(self.receiver,'factory',self.transport)
        self.assertEqual('held',self.local_state())
        with sqlite3.connect(self.receiver/'receiver.sqlite3') as c:
            payment=json.loads(c.execute('SELECT payment FROM order_ledger').fetchone()[0])
            self.assertEqual(10000,payment['refunded_amount']);self.assertEqual(83500,payment['remaining_amount'])
        shop_receiver.ingest(self.receiver,job)
        self.assertEqual(200,self.api('ack',ack)[0])
        shop_receiver.finish(self.receiver,job['event_id'])
        self.assertEqual('held',self.local_state())
        with sqlite3.connect(self.receiver/'receiver.sqlite3') as c:
            self.assertEqual(83500,json.loads(c.execute('SELECT payment FROM order_ledger').fetchone()[0])['remaining_amount'])

    def test_auth_payload_tampering_and_reference_validation(self):
        self.pay()
        self.assertEqual(401,self.api('claim',{'consumer':'factory'},secret='wrong')[0])
        self.assertEqual(400,self.api('claim',{'consumer':'../factory'})[0])
        job=self.claim();tampered=json.loads(json.dumps(job));tampered['payload']['order']['amount']=1
        with self.assertRaises(ValueError):shop_receiver.ingest(self.receiver,tampered)
        ack=shop_receiver.ingest(self.receiver,job)
        self.assertEqual(400,self.api('ack',dict(ack,references={'ledger':'only'}))[0])
        self.assertEqual(409,self.api('ack',dict(ack,lease_token='0'*64))[0])
        with patch.dict('os.environ',{'SHOP_HANDOFF_ENABLED':'0'}):
            self.assertEqual(401,self.api('claim',{'consumer':'factory'})[0])

    def test_parallel_consumers_only_claim_once(self):
        self.pay()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs=list(pool.map(lambda _:self.claim(),range(2)))
        self.assertEqual(1,sum(job is not None for job in jobs))

    def test_csv_is_copy_only_and_formula_safe(self):
        self.pay(sender='=HYPERLINK("https://example.com")')
        shop_receiver.run_once(self.receiver,'factory',self.transport)
        destination=self.receiver/'ledger-copy.csv'
        shop_receiver.export_csv(self.receiver,destination)
        with destination.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.reader(stream))
        self.assertTrue(rows[1][5].startswith("'="))
        with self.assertRaises(ValueError):shop_receiver.export_csv(self.receiver,destination)
        with self.assertRaises(ValueError):shop_receiver.database(self.root)

    def test_request_cancel_before_payment_does_not_tombstone_later_approval(self):
        product=dict(self.product);product.pop('wedding_photo',None)
        with agent.db() as c:c.execute('UPDATE shop_offers SET details=? WHERE id=?',(json.dumps(product),self.offer))
        self.place();self.notify(notification('a'*32,state='8'));self.notify(notification('a'*32))
        self.assertEqual(['ready'],[event['kind'] for event in self.events()])
        self.assertTrue(shop_receiver.run_once(self.receiver,'factory',self.transport))
        self.assertEqual('pending',self.local_state())

    def test_handoff_and_ack_survive_source_backup_restore(self):
        self.pay();shop_receiver.run_once(self.receiver,'factory',self.transport)
        self.server.shutdown();self.server.server_close();self.thread.join()
        backup=self.root.parent/'handoff-backup';restored=self.root.parent/'handoff-restored'
        manage_data.transfer(self.root,backup);manage_data.transfer(backup,restored,restore=True)
        with sqlite3.connect(restored/'state.sqlite3') as c:
            self.assertEqual(('acked',),c.execute('SELECT state FROM shop_handoff').fetchone())
        self.assertEqual(1,manage_data.inspect(restored)['counts']['shop_handoff'])
