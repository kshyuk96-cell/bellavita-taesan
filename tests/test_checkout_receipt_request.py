from contextlib import contextmanager
import json
import sqlite3
import unittest
from unittest.mock import patch

import checkout
from erp_invitation_management import paid_order


class ReceiptRequestTest(unittest.TestCase):
    def test_banking_order_keeps_invoice_request_in_staff_view(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        checkout.setup(connection)
        connection.commit()
        self.addCleanup(connection.close)

        @contextmanager
        def db():
            with connection:
                yield connection

        product = {'number': '123', 'title': '시험', 'price': 120000, 'groups': [],
                   'venue': '장소', 'address': '주소', 'event_time': '2026-12-01T12:00:00+09:00'}
        fields = {'attempt': 'a'*32, 'sender': '시험', 'same_buyer': 'yes', 'buyer_phone': '01011112222',
                  'consent': 'yes', 'payment_method': 'bank_transfer', 'receipt_type': 'invoice',
                  'invoice_representative': '대표', 'invoice_company': '시험 상호',
                  'invoice_business': '123-45-67890', 'invoice_email': 'test@example.org'}
        with patch('sendon_order_queue.enqueue'):
            for _ in range(2):
                self.assertEqual(checkout.start_order(db, 'b'*32, '99000', product, fields, 'https://example.org', {}),
                                 '/invitation-orders/' + 'a'*32 + '/receipt')
        row = connection.execute('SELECT * FROM invitation_orders').fetchone()
        request = {'type': 'invoice', 'representative': '대표', 'company': '시험 상호',
                   'businessNumber': '1234567890', 'email': 'test@example.org'}
        self.assertEqual(json.loads(row['details'])['receipt_request'], request)
        self.assertEqual(paid_order(row)['receiptRequest'], request)
        self.assertEqual(paid_order(row)['amount'], 120000)

    def test_cash_receipt_modes_validate_only_selected_identifier(self):
        self.assertIsNone(checkout.receipt_request({'invoice_email': 'invalid'}))
        self.assertEqual(checkout.receipt_request({'receipt_type': 'cash_phone', 'receipt_phone': '010-0000-0000'}),
                         {'type': 'cash_phone', 'phone': '01000000000'})
        self.assertEqual(checkout.receipt_request({'receipt_type': 'cash_business', 'receipt_business': '123-45-67890'}),
                         {'type': 'cash_business', 'businessNumber': '1234567890'})
        for fields in ({'receipt_type': 'cash_phone', 'receipt_phone': '123'},
                       {'receipt_type': 'cash_business', 'receipt_business': '01000000000'},
                       {'receipt_type': 'invoice', 'invoice_email': 'invalid'}):
            with self.assertRaises(ValueError):
                checkout.receipt_request(fields)
