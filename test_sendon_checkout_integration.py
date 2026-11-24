"""Exercise production checkout and deposit handlers without external I/O."""
import os
import socket
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import checkout
import erp_invitation_management as management
import order_operations
import sendon_order_queue as queue


class CheckoutIntegration(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')),
            patch.dict(sys.modules, {'order_sms':SimpleNamespace(staff_phones=lambda _:['01000000001'])}),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        with self.connect() as db:
            checkout.setup(db)
            order_operations.setup(db)
        self.product = dict(number='fixture', title='가상 상품', price=1000, groups=[],
                            address='가상 주소', event_time='2098-01-01T12:00:00+09:00')
        self.config = dict(USER_ID='fixture', LINK_KEY='fixture', LINK_VALUE='fixture', REQUEST_KEY='fixture')

    def connect(self):
        db = sqlite3.connect(self.root/'state.sqlite3')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        return db

    def start(self, order_id, method):
        fields = dict(attempt=order_id, consent='yes', buyer_phone='01000000002',
                      sender='가상 발신자', same_buyer='yes', payment_method=method)
        return checkout.start_order(self.connect, 'fixture', 1000, self.product,
                                    fields, 'https://fixture.invalid', self.config)

    def state(self, order_id):
        with self.connect() as db:
            row = db.execute('SELECT state FROM invitation_orders WHERE id=?',(order_id,)).fetchone()
            return row and row['state']

    def jobs(self, order_id):
        with self.connect() as db:
            return sorted(row['template'] for row in queue.list_jobs(db) if row['order_id']==order_id)

    def test_card_and_wallet_callbacks_replay_without_duplicate_jobs(self):
        for index, gateway_kind in enumerate(('card','wallet'),1):
            order_id = str(index)*32
            number = str(index)
            with patch.object(checkout,'payapp_request',return_value=(number,'https://fixture.invalid/checkout')):
                self.start(order_id,'payapp')
            self.assertEqual(self.jobs(order_id),[])
            date = '20970101120000'
            payload = dict(responsedate=date, userid='fixture', linkkey='fixture', linkval='fixture',
                           hash=checkout.signature(self.config,date),pay_state='4',var1=order_id,
                           mul_no=number,price='1000',payment_type=gateway_kind)
            for _ in range(2):
                self.assertTrue(checkout.receive_payment(self.connect,payload,self.config))
            self.assertEqual(self.state(order_id),'paid')
            self.assertEqual(self.jobs(order_id),[queue.CUSTOMER,queue.STAFF])
        with self.connect() as db:
            with patch.object(queue,'build_opener',side_effect=AssertionError('Network forbidden')):
                for _ in range(2):
                    queue.drain(db,{'SENDON_MODE':'dry_run'})
            self.assertEqual([r['result_code'] for r in queue.list_jobs(db)],['SIMULATED']*4)

    def test_bank_deposit_and_replay(self):
        order_id = 'b'*32
        self.start(order_id,'bank_transfer')
        self.start(order_id,'bank_transfer')
        self.assertEqual(self.state(order_id),'bank_pending')
        self.assertEqual(self.jobs(order_id),[queue.STAFF])
        for _ in range(2):
            self.assertEqual(management.confirm_deposit(self.root,{'sourceId':'payapp:'+order_id})['state'],'paid')
        self.assertEqual(self.jobs(order_id),[queue.CUSTOMER,queue.STAFF])

    def test_failed_order_and_failed_queue_are_separate(self):
        order_id = 'c'*32
        with self.connect() as db:
            db.execute("""CREATE TRIGGER fail_order BEFORE INSERT ON invitation_orders
                BEGIN SELECT RAISE(ABORT,'fixture'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.start(order_id,'bank_transfer')
        self.assertIsNone(self.state(order_id))
        self.assertEqual(self.jobs(order_id),[])
        with self.connect() as db:
            db.execute('DROP TRIGGER fail_order')
        with patch.object(queue,'setup',side_effect=sqlite3.OperationalError('fixture')):
            with self.assertLogs(queue.logger,level='ERROR'):
                self.start(order_id,'bank_transfer')
        self.assertEqual(self.state(order_id),'bank_pending')
        self.assertEqual(self.jobs(order_id),[])
