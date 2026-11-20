import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import erp_invitation_approvals as approvals
import erp_invitation_order_edits as edits


class Approvals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        with sqlite3.connect(self.root / 'state.sqlite3') as db:
            db.execute('CREATE TABLE invitation_orders(id TEXT PRIMARY KEY,created TEXT,state TEXT,details TEXT)')
            db.execute('CREATE TABLE order_operations(id TEXT,order_number TEXT)')
            for key, state, created in [('old-paid', 'paid', '2001-01-01T12:00:00+09:00'),
                                         ('waiting', 'waiting', '2099-01-01T12:00:00+09:00'),
                                         ('cancelled', 'cancelled', '2099-01-01T12:00:00+09:00'),
                                         ('paid-new', 'paid', '2099-01-01T12:00:00+09:00')]:
                details = {'amount': 99000, 'title': '가상 결혼', 'sender': '원문 단체',
                           'recipient': '가상 혼주', 'event_time': '2099-01-02T12:00:00+09:00'}
                db.execute('INSERT INTO invitation_orders VALUES(?,?,?,?)',
                           (key, created, state, json.dumps(details)))
            db.execute('INSERT INTO order_operations VALUES(?,?)', ('paid-new', '13400'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_index_has_no_recent_date_cutoff_and_never_includes_unpaid_cancelled_or_hidden(self):
        before = (self.root / 'state.sqlite3').read_bytes()
        rows = approvals.query(self.root, {'view': ['index']})
        self.assertEqual(rows['count'], 2)
        self.assertEqual({r['id'] for r in rows['orders']}, {'payapp:old-paid', 'payapp:paid-new'})
        self.assertEqual(next(r for r in rows['orders'] if r['id'] == 'payapp:paid-new')['legacyOrderNumber'], '13400')
        self.assertTrue(all('fields' not in r for r in rows['orders']))
        edits.update(self.root, {'sourceId': 'payapp:paid-new', 'action': 'delete', 'revision': 0}, 'staff')
        rows = approvals.query(self.root, {'view': ['index']})
        self.assertEqual(rows['count'], 1)
        self.assertEqual(rows['orders'][0]['id'], 'payapp:old-paid')
        self.assertEqual((self.root / 'state.sqlite3').read_bytes(), before)

    def test_selected_records_use_latest_fulfillment_and_authoritative_payment_state(self):
        edits.update(self.root, {'sourceId': 'payapp:old-paid', 'action': 'edit', 'revision': 0,
                                'fields': {'sender': '수정 단체'}}, 'staff')
        row = approvals.query(self.root, {'ids': ['payapp:old-paid']})['orders'][0]
        self.assertEqual(row['fields']['sender'], '수정 단체')
        self.assertEqual(row['amount'], 99000)
        self.assertEqual(row['editRevision'], 1)
        with sqlite3.connect(self.root / 'state.sqlite3') as db:
            db.execute("UPDATE invitation_orders SET state='cancelled' WHERE id='old-paid'")
        self.assertEqual(approvals.query(self.root, {'ids': ['payapp:old-paid']})['orders'], [])

    def test_index_count_is_not_truncated_at_500_and_details_are_bounded(self):
        with sqlite3.connect(self.root / 'state.sqlite3') as db:
            for number in range(501):
                db.execute('INSERT INTO invitation_orders VALUES(?,?,?,?)',
                           ('added-' + str(number), '2099-01-01', 'paid', '{}'))
        index = approvals.query(self.root, {'view': ['index']})
        self.assertEqual(index['count'], 503)
        self.assertEqual(len(index['orders']), 503)
        self.assertEqual(len(approvals.query(self.root)['orders']), 100)
        self.assertEqual(approvals.query(self.root, {'ids': ['payapp:added-500']})['orders'][0]['id'], 'payapp:added-500')
        with self.assertRaises(ValueError):
            approvals.query(self.root, {'ids': [','.join('payapp:' + str(i) for i in range(101))]})

    def test_queue_route_requires_bridge_auth_and_does_not_accept_writes(self):
        import erp_invitation_management as management
        import erp_invitations
        answers = []
        handler = SimpleNamespace(headers={}, path='/erp/invitations/pending-orders?view=index',
                                  send_json=lambda data, status=200: answers.append((data, status)))
        self.assertTrue(management.dispatch(handler, 'POST', '/erp/invitations/pending-orders', self.root, None))
        self.assertEqual(answers[-1][1], 405)
        with patch.dict('os.environ', {'ERP_INVITATION_TOKEN': 'test'}):
            self.assertTrue(erp_invitations.dispatch(handler, 'GET', '/erp/invitations/pending-orders', self.root,
                                                    'https://erp.test', None, None))
        self.assertEqual(answers[-1][1], 401)


if __name__ == '__main__':
    unittest.main()
