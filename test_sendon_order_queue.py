import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sendon_order_queue as queue


class QueueTest(unittest.TestCase):
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
