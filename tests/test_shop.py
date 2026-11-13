"""Real HTTP handlers + isolated SQLite; PayApp is always mocked."""
import datetime as dt
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('TAESAN_DATA', '/tmp/bellavita-test-import')
import agent
import checkout
import manage_data
import shop

CONFIG = {key: 'test-' + key.lower() for key in checkout.PAYAPP_FIELDS}
ORIGIN = 'https://shop.test'
SECRET = 'isolated-test-secret-' * 3
PRODUCT = dict(title='쌀빛그림화환', price='93500', images='https://shop.test/photo.webp',
               options='쌀 10kg | 0\n쌀 20kg | 10000\n쌀 30kg | 20000', greetings='결혼을 진심으로 축하합니다')


def notification(order_id, number='1234', state='4', amount=93500, **extra):
    when = dt.datetime.now(checkout.SEOUL).strftime('%Y%m%d%H%M%S')
    return dict(userid=CONFIG['USER_ID'], linkkey=CONFIG['LINK_KEY'], linkval=CONFIG['LINK_VALUE'],
                responsedate=when, hash=checkout.signature(CONFIG, when),
                var1=order_id, mul_no=number, price=str(amount), pay_state=state, **extra)


class ShopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'data'
        self.root.mkdir()
        self.root_patch = patch.object(agent, 'ROOT', self.root)
        self.root_patch.start()
        env = {'TAESAN_PUBLIC_URL': ORIGIN, 'TAESAN_WEBHOOK_SECRET': SECRET,
               'SHOP_CHECKOUT_ENABLED': '1', 'INVITATION_CHECKOUT_ENABLED': '0',
               'TAESAN_LIVE_WRITE': '0', 'TAESAN_ADMIN_IDS': 'admin', 'TAESAN_STAFF_IDS': 'staff',
               **{'INVITATION_PAYAPP_' + key: value for key, value in CONFIG.items()}}
        self.env_patch = patch.dict(os.environ, env)
        self.env_patch.start()
        self.payment = patch.object(checkout, 'payapp_request', return_value=('1234', 'https://payapp.kr/test'))
        self.mock_payment = self.payment.start()
        self.server = agent.ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        expiry = str(int(time.time()) + 600)
        self.admin = f'/shop/admin/{expiry}/{shop.admin_signature(expiry, SECRET)}'
        self.post_admin(action='save_product', **PRODUCT)
        with agent.db() as c:
            product = c.execute('SELECT * FROM shop_products').fetchone()
        self.product_id = product['id']
        self.event = dict(action='issue_offers', product_id=self.product_id, revision='1', event='검증 예식',
                          when=(dt.datetime.now(checkout.SEOUL) + dt.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M'),
                          kind='wedding', venue='테스트홀', address='테스트 설치 주소', prices='93500,99000',
                          wedding_photo='https://shop.test/couple.webp')
        self.post_admin(**self.event)
        with agent.db() as c:
            self.offers = c.execute('SELECT * FROM shop_offers ORDER BY price').fetchall()
        self.offer = self.offers[0]['id']
        self.product = json.loads(self.offers[0]['details'])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.payment.stop()
        self.env_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    def http(self, path, fields=None, origin=ORIGIN):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {'Origin': origin}
        if fields is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        client.request('POST' if fields is not None else 'GET', path,
                       urlencode(fields) if fields is not None else None, headers)
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        client.close()
        return result

    def post_admin(self, **fields):
        result = self.http(self.admin, fields)
        self.assertEqual(303, result[0], result[2])
        return result

    def fields(self, attempt='a' * 32):
        return dict(attempt=attempt, choice0='1', choice1='custom', choice2='1',
                    sender='벨라비타 대표 홍길동', message='행복한 결혼을 진심으로 축하합니다',
                    buyer='테스트 주문자', buyer_phone='010-1234-5678', consent='yes')

    def place(self, attempt='a' * 32, **overrides):
        fields = dict(self.fields(attempt), **overrides)
        return self.http('/shop/order/' + self.offer, fields)

    def notify(self, fields, status=200):
        result = self.http('/invitation-orders/payapp', fields)
        self.assertEqual(status, result[0], result)
        return result

    def order(self, attempt='a' * 32):
        with agent.db() as c:
            return c.execute('SELECT * FROM invitation_orders WHERE id=?', (attempt,)).fetchone()

    def test_price_snapshot_and_repeat_issue(self):
        self.post_admin(**self.event)
        with agent.db() as c:
            self.assertEqual(2, c.execute('SELECT COUNT(*) FROM shop_offers').fetchone()[0])
        self.post_admin(action='save_product', product_id=self.product_id, revision='1',
                        **dict(PRODUCT, price='110000', options='쌀 10kg | 5000'))
        self.assertEqual(303, self.place(price='1', amount='1', same_buyer='yes')[0])
        data = json.loads(self.order()['details'])
        self.assertEqual(93500, data['amount'])
        self.assertEqual(data['sender'], data['buyer'])
        self.assertEqual(93500, data['product_snapshot']['price'])
        self.assertEqual('쌀 10kg', data['options'][0][1])
        self.assertEqual(303, self.place(price='1', amount='1', same_buyer='yes')[0])
        self.mock_payment.assert_called_once()
        self.mock_payment.return_value = ('5678', 'https://payapp.kr/test2')
        self.offer = self.offers[1]['id']
        self.assertEqual(303, self.place('b' * 32, choice0='2')[0])
        self.assertEqual(109000, json.loads(self.order('b' * 32)['details'])['amount'])

    def test_authorization_origin_and_staff_approval(self):
        bad = self.admin[:-1] + ('a' if self.admin[-1] != 'a' else 'b')
        self.assertEqual(403, self.http(bad)[0])
        expired = str(int(time.time()) - 1)
        self.assertEqual(403, self.http(f'/shop/admin/{expired}/{shop.admin_signature(expired, SECRET)}')[0])
        self.assertEqual(403, self.http(self.admin, dict(action='save_product', **PRODUCT), origin='https://evil.test')[0])
        def dispatch(user, message):
            return agent.dispatch({'userRequest': {'user': {'id': user}, 'utterance': message}})
        self.assertIn('대표 계정', json.dumps(dispatch('staff', '쇼핑몰 관리'), ensure_ascii=False))
        self.assertIn('/shop/admin/', json.dumps(dispatch('admin', '쇼핑몰 관리')))
        dispatch('new-staff', '직원 신청 검증직원')
        with agent.db() as c:
            request = c.execute('SELECT id FROM access_requests WHERE owner=?', ('new-staff',)).fetchone()[0]
        dispatch('staff', '직원 승인 ' + request)
        self.assertIsNone(agent.role_of('new-staff'))
        dispatch('admin', '직원 승인 ' + request)
        self.assertEqual('staff', agent.role_of('new-staff'))
        self.assertEqual(403, self.place_with_origin('https://evil.test')[0])

    def place_with_origin(self, origin):
        return self.http('/shop/order/' + self.offer, self.fields(), origin=origin)

    def test_disabled_stopped_invalid_and_expired(self):
        with patch.dict(os.environ, {'SHOP_CHECKOUT_ENABLED': '0'}):
            self.assertEqual(503, self.place()[0])
        self.assertEqual(422, self.place(choice0='unknown')[0])
        self.assertEqual(422, self.place(consent='no')[0])
        self.post_admin(action='change_offer', offer_id=self.offer, active='0')
        self.assertEqual(410, self.place()[0])
        self.post_admin(action='change_offer', offer_id=self.offer, active='1')
        self.assertEqual(200, self.http('/shop/order/' + self.offer)[0])
        with agent.db() as c:
            data = dict(self.product, event_time='2020-01-01T00:00:00+09:00')
            c.execute('UPDATE shop_offers SET details=? WHERE id=?', (json.dumps(data), self.offer))
        self.assertEqual(410, self.place()[0])
        self.mock_payment.assert_not_called()

    def test_duplicate_reordered_partial_and_full_cancellation(self):
        self.place()
        partial = notification('a' * 32, '9001', '70', 10000, orig_mul_no='1234', orig_price='93500')
        self.notify(partial)
        self.notify(dict(partial, pay_state='71'))
        self.notify(notification('a' * 32))  # delayed original approval
        self.assertEqual('partial_cancel', self.order()['state'])
        self.notify(notification('a' * 32, '9002', '71', 5000, orig_mul_no='1234', orig_price='93500'))
        page = self.http(self.admin)[2]
        self.assertIn('15,000원', page)
        self.assertIn('78,500원', page)
        self.notify(dict(partial, price='9999'), 400)
        self.notify(notification('a' * 32, '9003', '70', 90000, orig_mul_no='1234', orig_price='93500'), 400)
        self.notify(notification('a' * 32, state='9'))
        self.notify(notification('a' * 32))
        self.assertEqual('cancelled', self.order()['state'])
        self.assertIn('93,500원 (전체)', self.http(self.admin)[2])

    def test_request_cancel_does_not_override_approval_in_either_order(self):
        self.place()
        self.notify(notification('a' * 32, state='8'))
        self.notify(notification('a' * 32, state='4'))
        self.notify(notification('a' * 32, state='32'))
        self.notify(notification('a' * 32, state='10'))
        self.assertEqual('paid', self.order()['state'])

    def test_legacy_cancel_without_event_history_stays_cancelled(self):
        self.place()
        with agent.db() as connection:
            connection.execute("UPDATE invitation_orders SET state='cancelled' WHERE id=?", ('a' * 32,))
        self.notify(notification('a' * 32, state='8'))
        self.notify(notification('a' * 32, state='4'))
        self.assertEqual('cancelled', self.order()['state'])

    def test_invalid_signature_amount_number_and_unknown_state(self):
        self.place()
        base = notification('a' * 32)
        for changes in ({'hash': 'wrong'}, {'linkval': 'wrong'}, {'price': '1'}, {'mul_no': '9999'},
                        {'responsedate': '20261399999999'}, {'pay_state': '999'}):
            self.notify(dict(base, **changes), 400)
        self.assertEqual('waiting', self.order()['state'])
        self.notify(dict(base, hash=base['hash'].upper()))
        self.assertEqual('paid', self.order()['state'])

    def test_timeout_reconciliation_no_retry_and_signed_recovery(self):
        self.mock_payment.side_effect = TimeoutError('mock lost response')
        self.assertEqual(502, self.place()[0])
        self.assertEqual(422, self.place()[0])
        self.assertEqual('requesting', self.order()['state'])
        self.mock_payment.assert_called_once()
        fields = dict(action='record_payment_check', order_id='a' * 32, payment_number='1234', amount='93500',
                      buyer_phone='01012345678', compared='yes', note='테스트 판매자 화면 대조, 재통보 요청')
        self.assertEqual(422, self.http(self.admin, dict(fields, amount='1'))[0])
        self.post_admin(**fields)
        self.assertEqual('requesting', self.order()['state'])
        self.assertIn('재통보 요청', self.http(self.admin)[2])
        self.notify(notification('a' * 32))
        self.assertEqual('paid', self.order()['state'])
        self.assertEqual(303, self.place()[0])
        self.mock_payment.assert_called_once()

    def test_cross_order_payment_reference_rejected(self):
        self.place()
        self.mock_payment.side_effect = TimeoutError()
        self.place('b' * 32)
        self.notify(notification('b' * 32), 400)
        self.assertIsNone(self.order('b' * 32)['payment_number'])

    def test_actual_payapp_response_parsing_and_uncertain_failures(self):
        self.payment.stop()  # Exercise transport parsing with a fake opener, never a live API.
        responses = [
            ('d' * 32, b'not-a-payapp-response', 'requesting', 502),
            ('1' * 32, b'\xff\xfe', 'requesting', 502),
            ('2' * 32, b'state=1&mul_no=1234&payurl=https%3A%2F%2F%5Bbad', 'requesting', 502),
            ('e' * 32, b'state=0&errno=9040', 'requesting', 502),
            ('f' * 32, b'state=0&errorMessage=rejected', 'failed', 422),
            ('b' * 32, b'state=1&mul_no=1234&payurl=https%3A%2F%2Fevil.test%2F', 'requesting', 502),
            ('c' * 32, b'state=1&mul_no=5678&payurl=http%3A%2F%2Fpayapp.kr%2Ftest', 'waiting', 303),
        ]
        for attempt, reply, state, status in responses:
            with patch.object(checkout, 'build_opener') as opener:
                opener.return_value.open.return_value = io.BytesIO(reply)
                self.assertEqual(status, self.place(attempt)[0])
                self.assertEqual(state, self.order(attempt)['state'])
                request = opener.return_value.open.call_args.args[0]
                fields = parse_qs(request.data.decode())
                self.assertEqual(['93500'], fields['price'])
                self.assertEqual([attempt], fields['var1'])
                self.assertEqual([ORIGIN + '/invitation-orders/payapp'], fields['feedbackurl'])
                # Same attempt never submits a second API request, including unclear results.
                self.place(attempt)
                opener.return_value.open.assert_called_once()

    def test_concurrent_form_submission_only_requests_payment_once(self):
        def slow_response(*args):
            time.sleep(.15)
            return '1234', 'https://payapp.kr/test'
        self.mock_payment.side_effect = slow_response
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.place()[0], range(2)))
        self.assertEqual([303, 422], sorted(responses))
        self.assertEqual(303, self.place()[0])
        self.mock_payment.assert_called_once()

    def test_callback_before_request_response_and_changed_replay(self):
        def early(*args):
            self.notify(notification('a' * 32))
            return '1234', 'https://payapp.kr/test'
        self.mock_payment.side_effect = early
        self.assertEqual(303, self.place()[0])
        self.assertEqual('paid', self.order()['state'])
        self.assertEqual(422, self.place(sender='다른 보내는 분')[0])
        self.mock_payment.assert_called_once()

    def test_invitation_paths_and_separate_order_history(self):
        job = 'e' * 32
        directory = self.root / 'invitations' / job
        directory.mkdir(parents=True)
        (directory / '93500.order.json').write_text(json.dumps(self.product))
        self.assertEqual(200, self.http(f'/i/{job}/93500/order')[0])
        self.assertEqual(503, self.http(f'/i/{job}/93500/order', self.fields('c' * 32))[0])
        with patch.dict(os.environ, {'INVITATION_CHECKOUT_ENABLED': '1'}):
            self.assertEqual(303, self.http(f'/i/{job}/93500/order', self.fields('c' * 32))[0])
        self.notify(notification('c' * 32))
        self.mock_payment.return_value = ('5678', 'https://payapp.kr/test2')
        self.place()
        self.notify(notification('a' * 32, '5678'))
        expiry = str(int(time.time()) + 600)
        page = self.http(f'/invitation-orders/admin/{expiry}/{checkout.admin_signature(expiry, SECRET)}')[2]
        self.assertIn('c' * 32, page)
        self.assertNotIn('a' * 32, page)
        shop_page = self.http(self.admin)[2]
        self.assertIn('a' * 32, shop_page)
        self.assertNotIn('c' * 32, shop_page)

    def test_backup_restore_restart_and_corrupt_backup(self):
        self.place()
        self.notify(notification('a' * 32))
        files = self.root / 'invitations' / ('e' * 32)
        files.mkdir(parents=True)
        (files / '93500.order.json').write_text(json.dumps(self.product))
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        backup = Path(self.tmp.name) / 'backup'
        restored = Path(self.tmp.name) / 'restored'
        manage_data.transfer(self.root, backup)
        manage_data.transfer(backup, restored, restore=True)
        inspection = manage_data.inspect(restored)
        self.assertEqual('ok', inspection['integrity_check'])
        self.assertEqual(1, inspection['counts']['invitation_orders'])
        self.assertFalse(inspection['under_var_data'])
        self.assertEqual(manage_data.digest(backup / 'state.sqlite3'), manage_data.digest(restored / 'state.sqlite3'))
        with sqlite3.connect(self.root / 'state.sqlite3') as original, sqlite3.connect(restored / 'state.sqlite3') as recovered:
            self.assertEqual(list(original.iterdump()), list(recovered.iterdump()))
        self.assertTrue((restored / 'invitations' / ('e' * 32) / '93500.order.json').exists())
        # New HTTP server, same persistent data, schema initialization repeated.
        self.root_patch.stop()
        self.root_patch = patch.object(agent, 'ROOT', restored)
        self.root_patch.start()
        self.server = agent.ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual('paid', self.order()['state'])
        self.assertEqual(200, self.http('/shop/order/' + self.offer)[0])
        self.assertIn('결제 완료', self.http(self.admin)[2])
        with self.assertRaises(ValueError):
            manage_data.transfer(backup, restored, restore=True)
        (backup / 'invitations' / ('e' * 32) / '93500.order.json').write_text('corrupted')
        with self.assertRaises(ValueError):
            manage_data.transfer(backup, Path(self.tmp.name) / 'bad', restore=True)


if __name__ == '__main__':
    unittest.main()
