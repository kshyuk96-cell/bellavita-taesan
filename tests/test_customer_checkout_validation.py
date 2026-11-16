"""Offline regression coverage using the real checkout normalizer."""
import unittest

import checkout


class CustomerCheckoutValidationTests(unittest.TestCase):
    def setUp(self):
        self.product = {
            'number': '123', 'title': 'Sample', 'price': 93500,
            'venue': 'Hall', 'address': 'Address', 'event_time': '2027-01-01T12:00:00+09:00',
            'groups': [{'label': 'Option', 'choices': {
                '1': {'label': 'Selected', 'amount': 5500, 'custom': False},
                '2': {'label': 'Custom', 'amount': 0, 'custom': True}}}],
        }
        self.fields = {'consent': 'yes', 'choice0': '1', 'sender': ' Sender ',
                       'buyer': ' Buyer ', 'buyer_phone': '010-1234-5678'}

    def normalize(self, **fields):
        return checkout.normalize_order(self.product, {**self.fields, **fields})

    def test_existing_order_fields(self):
        self.assertEqual(self.normalize(), {
            'product': '123', 'title': 'Sample', 'options': [['Option', 'Selected']],
            'sender': 'Sender', 'message': '', 'buyer': 'Buyer', 'buyer_phone': '01012345678',
            'venue': 'Hall', 'address': 'Address', 'event_time': self.product['event_time'],
            'amount': 99000, 'source': '',
        })

    def test_customer_regressions(self):
        for prefix in ('010', '011', '016', '017', '018', '019'):
            for suffix in ('1234567', '12345678'):
                for phone in (prefix + suffix, prefix + '- ' + suffix):
                    with self.subTest(phone=phone):
                        self.assertEqual(self.normalize(buyer_phone=phone)['buyer_phone'], prefix + suffix)
        self.assertEqual(self.normalize(same_buyer='yes', buyer='')['buyer'], 'Sender')
        self.assertEqual(self.normalize(choice0='2', message=' Custom message ')['message'], 'Custom message')
        for identifier in ('01012345678', ' 010-1234 5678 ', '', '   '):
            with self.subTest(identifier=identifier):
                self.assertEqual(self.normalize(cash_receipt_type='personal',
                    cash_receipt_identifier=identifier)['cash_receipt'],
                    {'kind': 'personal', 'identifier': '01012345678'})
        self.assertEqual(self.normalize(cash_receipt_type='business',
            cash_receipt_identifier=' 123-45 67890 ')['cash_receipt'],
            {'kind': 'business', 'identifier': '1234567890'})
        self.assertEqual(self.normalize(cash_receipt_type='self',
            cash_receipt_identifier='ignored')['cash_receipt'],
            {'kind': 'self', 'identifier': '0100001234'})
        for fields in ({'consent': ''}, {'choice0': ''}, {'buyer_phone': '01212345678'},
                       {'buyer_phone': '010123456'}, {'buyer_phone': '010123456789'},
                       {'cash_receipt_type': 'other'},
                       {'cash_receipt_type': 'personal', 'buyer_phone': '0111234567'},
                       {'cash_receipt_type': 'business', 'cash_receipt_identifier': '123456789'},
                       {'cash_receipt_type': 'personal', 'cash_receipt_identifier': '0101234567'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.normalize(**fields)

    def test_ascii_boundary(self):
        for digits in ('０１２３４５６７８９', '٠١٢٣٤٥٦٧٨٩', '۰۱۲۳۴۵۶۷۸۹', '०१२३४५६७८९'):
            translate = str.maketrans('0123456789', digits)
            # Keep the ASCII phone prefix to expose Unicode acceptance in the suffix.
            for phone in ('010' + '12345678'.translate(translate),
                          '011' + '1234567'.translate(translate),
                          '010-1234-567' + digits[8],
                          '01012345678'.translate(translate)):
                with self.subTest(phone=phone), self.assertRaises(ValueError):
                    self.normalize(buyer_phone=phone)
            for kind, identifier in (('personal', '010' + '12345678'.translate(translate)),
                                     ('personal', '010-1234-567' + digits[8]),
                                     ('business', '1234567890'.translate(translate)),
                                     ('business', '123-45-6789' + digits[0])):
                with self.subTest(kind=kind, identifier=identifier), self.assertRaises(ValueError):
                    self.normalize(cash_receipt_type=kind, cash_receipt_identifier=identifier)


if __name__ == '__main__':
    unittest.main()
