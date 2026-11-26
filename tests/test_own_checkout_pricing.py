import unittest

import checkout
import own_checkout_pricing as pricing


class OwnCheckoutPriceTest(unittest.TestCase):
    def test_existing_snapshot_prices_match_server_order_total(self):
        fields = {
            'consent': 'yes', 'sender': '테스트', 'buyer_phone': '01000000000',
            'same_buyer': 'yes', 'payment_method': 'payapp', 'quantity': '1',
        }
        for old_price, base in ((93500, 93500), (99000, 120000)):
            snapshot = {
                'number': '123', 'title': '테스트', 'price': old_price,
                'date': '2026-12-01', 'clock': '12:00', 'venue': '장소',
                'address': '주소', 'event_time': '2026-12-01T12:00:00+09:00',
                'images': [],
                'groups': [{'label': '상품 선택', 'choices': {
                    str(kg): {'label': f'쌀 {kg}kg', 'amount': extra, 'custom': False}
                    for kg, extra in ((10, 0), (20, 51000), (30, 101000))
                }}],
            }
            current = pricing.apply(snapshot, str(old_price))
            self.assertEqual(current, pricing.apply(current, str(old_price)))
            self.assertEqual(snapshot['price'], old_price)
            page = checkout.render(current, '#', False)
            self.assertIn(f'{base:,}원', page)
            for kg, total in ((10, base), (20, 170000), (30, 220000)):
                self.assertIn(f'data-amount="{total-base}" data-kg="{kg}"', page)
                for method in ('payapp', 'bank_transfer'):
                    order = checkout.normalize_order(current, {**fields, 'choice0': str(kg), 'payment_method': method})
                    self.assertEqual(order['amount'], total)

    def test_trial_snapshot_and_blogpay_copy_keep_original_price(self):
        trial = {'price': 1000}
        self.assertIs(pricing.apply(trial, '93500'), trial)
        copy = '🌾 화환 안내🌾 · 99,000원\n별도 안내 99,000원'
        self.assertEqual(pricing.public_copy(copy, 99000, 'https://blogpay.example/item'), copy)
        own = 'https://orders.example/i/' + 'a' * 32 + '/99000/order'
        self.assertEqual(pricing.public_copy(copy, 99000, own),
                         '🌾 화환 안내🌾 · 120,000원\n별도 안내 99,000원')
