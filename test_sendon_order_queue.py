import socket
from urllib.error import HTTPError
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sendon_order_queue as queue
import owner_alerts


class QueueTest(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('Outbound network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_non_payapp_owner_alerts_keep_their_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            def connect():
                db = sqlite3.connect(Path(directory) / 'alerts.sqlite')
                db.row_factory = sqlite3.Row
                return db
            with connect() as db:
                owner_alerts.setup(db)
                db.execute("INSERT INTO alert_account VALUES (1,'fixture')")
                for event in ('payapp:fixture','staff:fixture','job:fixture','kakao-order:fixture'):
                    owner_alerts.enqueue(db,event,'테스트 알림')
            with patch.object(owner_alerts,'token',return_value={'access_token':'fixture'}), patch.object(
                owner_alerts,'post',return_value={'result_code':0}
            ) as transport:
                for _ in range(4):
                    owner_alerts.send_pending(connect)
                self.assertEqual(transport.call_count,3)
            with connect() as db:
                sent = dict(db.execute('SELECT id,sent FROM alert_outbox'))
            self.assertEqual(sent,{'payapp:fixture':0,'staff:fixture':1,'job:fixture':1,'kakao-order:fixture':1})

    def test_transient_and_ambiguous_provider_outcomes(self):
        env = {'SENDON_MODE':'live','SENDON_ID':'fixture','SENDON_API_KEY':'fixture'}
        for exception, state, code in (
            (HTTPError(queue.API,429,'',{},None),'queued','429'),
            (HTTPError(queue.API,503,'',{},None),'held','503'),
            (TimeoutError(),'held','UNKNOWN'),
        ):
            def fail(*args, **kwargs):
                raise exception
            self.assertEqual(queue.transmit(queue.STAFF,'01000000001',{},env=env,open_request=fail)[::2],(state,code))

    def test_activation_cutoff_holds_old_orders_even_if_enqueued_later(self):
        env = dict(SENDON_MODE='live', SENDON_LIVE_START_AT='2020-01-01T00:00:00+00:00',
                   SENDON_ID='fixture', SENDON_API_KEY='fixture')
        with sqlite3.connect(':memory:') as db:
            queue.setup(db)
            db.execute('CREATE TABLE invitation_orders (id TEXT PRIMARY KEY, created TEXT)')
            db.executemany('INSERT INTO invitation_orders VALUES (?,?)', [
                ('past', '2019-01-01'), ('after', '2021-01-01'),
            ])
            db.executemany(
                "INSERT INTO sendon_order_queue(order_id,template,recipient,variables,created_at) "
                "VALUES(?,?,?,'{}',?)",
                [('past',queue.CUSTOMER,'01000000001','2021-01-01'),
                 ('old-queue',queue.CUSTOMER,'01000000001','2019-01-01'),
                 ('after',queue.CUSTOMER,'01000000001','2021-01-01')],
            )
            with patch.object(queue, 'transmit', return_value=('sent','','200','fixture')) as fake:
                self.assertFalse(queue.process(db, 'past', queue.CUSTOMER, env=env))
                self.assertTrue(queue.process(db, 'after', queue.CUSTOMER, env=env))
                self.assertFalse(queue.process(db, 'after', queue.CUSTOMER, env=env))
                fake.assert_called_once()
            rows = {row['order_id']: row for row in queue.list_jobs(db)}
            self.assertEqual(len(rows), 3)
            for key in ('past', 'old-queue'):
                self.assertEqual(rows[key]['result_code'], 'PRE_LIVE_HOLD')
                self.assertEqual(rows[key]['state'], 'held')
                self.assertTrue(rows[key]['reason'])

    def test_missing_either_credential_records_configuration_error(self):
        for configured in ('SENDON_ID', 'SENDON_API_KEY'):
            with self.subTest(configured=configured), sqlite3.connect(':memory:') as db:
                queue.setup(db)
                db.execute(
                    "INSERT INTO sendon_order_queue(order_id,template,recipient,variables,created_at) "
                    "VALUES(?,?,?,'{}',?)", ('new',queue.STAFF,'01000000001',queue.now()),
                )
                env = {'SENDON_MODE':'live', 'SENDON_LIVE_START_AT':'2020-01-01T00:00:00+00:00',
                       configured:'fixture'}
                with patch.object(queue, 'build_opener') as http:
                    self.assertTrue(queue.process(db, 'new', queue.STAFF, env=env))
                    http.assert_not_called()
                self.assertEqual(queue.list_jobs(db)[0]['result_code'], 'CONFIG_ERROR')
                self.assertEqual(queue.list_jobs(db)[0]['state'], 'held')

    def test_invalid_mode_is_off_and_expired_claim_is_held(self):
        with sqlite3.connect(':memory:') as db:
            queue.setup(db)
            db.execute("INSERT INTO sendon_order_queue(order_id,template,recipient,variables,state,created_at) VALUES(?,?,?,?,?,?)",
                       ('fixture',queue.STAFF,'01000000001','{}','sending',queue.now()))
            queue.drain(db,{'SENDON_MODE':'typo'})
            self.assertEqual(queue.list_jobs(db)[0]['state'],'sending')
            queue.drain(db,{'SENDON_MODE':'dry_run'})
            self.assertEqual(queue.list_jobs(db)[0]['state'],'held')

    def test_bank_lifecycle_is_idempotent_and_dry(self):
        with tempfile.TemporaryDirectory() as directory:
            def connect():
                return sqlite3.connect(Path(directory) / 'orders.sqlite')
            order = {'payment_method':'bank_transfer', 'buyer_phone':'01000000002'}
            with patch.dict(sys.modules, {'order_sms':SimpleNamespace(staff_phones=lambda _: ['01000000001'])}):
                queue.enqueue(connect, 'synthetic', order)
                queue.enqueue(connect, 'synthetic', order)
                with connect() as db:
                    self.assertEqual([r['template'] for r in queue.list_jobs(db)], [queue.STAFF])
                queue.enqueue(connect, 'synthetic', order, customer=True)
                queue.enqueue(connect, 'synthetic', order, customer=True)
            def no_network(*args, **kwargs):
                self.fail('dry mode attempted network access')
            with connect() as db:
                for template in (queue.STAFF, queue.CUSTOMER):
                    self.assertTrue(queue.process(db,'synthetic',template,env={'SENDON_MODE':'dry_run'},open_request=no_network))
                    self.assertFalse(queue.process(db,'synthetic',template,env={},open_request=no_network))
                rows = queue.list_jobs(db)
                self.assertEqual(len(rows),2)
                self.assertTrue(all(r['result_code']=='SIMULATED' and 'recipient' not in r for r in rows))


if __name__ == '__main__':
    unittest.main()
