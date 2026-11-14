import unittest
from unittest.mock import Mock, patch

import order_intake
import order_ledger


class DeliveryOrderFieldTests(unittest.TestCase):
    def test_ledger_keeps_delivery_time_out_of_the_event_time_column(self):
        marker = 'qa-order'
        selection = {'dataValidation': {'condition': {'type': 'ONE_OF_LIST',
                     'values': [{'userEnteredValue': '모의 선택'}]}}}
        for update, event_time, expected in ((False, '13:30', 13.5 / 24), (False, '', None), (True, '13:30', None)):
            with self.subTest(update=update, event_time=event_time):
                session = Mock()
                ledger = order_ledger.Ledger(session, 'fixture')
                cells = [{} for _ in range(29)]
                for column in (4, 6, 8):
                    cells[column] = selection
                if update:
                    cells[28] = {'userEnteredValue': {'stringValue': marker}}
                stored = [None] * 29
                ledger.values = Mock(side_effect=[[order_ledger.HEADER], *([] if update else [[]]), [], [stored]])
                ledger.find = Mock(side_effect=[5 if update else None, 5])
                ledger.row_cells = Mock(return_value=cells)
                ledger.save(marker, {'delivery_date': '2026-11-21', 'delivery_time': '09:30',
                                     'event_time': event_time}, '2026-11-14T09:00:00+09:00', update=update)
                requests = session.post.call_args.kwargs['json']['requests']
                columns = {r['updateCells']['start']['columnIndex']: r['updateCells']['rows'][0]['values'][0]
                           for r in requests}
                if expected is None:
                    self.assertNotIn(2, columns)
                else:
                    self.assertAlmostEqual(columns[2]['userEnteredValue']['numberValue'], expected)
                self.assertIn('배송시각 09:30', columns[9]['userEnteredValue']['stringValue'])

    def test_spaced_shipping_labels_survive_ledger_validation(self):
        for date_label, time_label in (('배송일', '배송시간'), ('배송 일', '배송 시간'), ('배송일', '배송시각')):
            with self.subTest(date_label=date_label, time_label=time_label):
                message = ('주문 접수\n설치장소: 가상 예식장\n보내는 분: 모의 직원\n'
                           f'{date_label}: 2026-11-21\n{time_label}：09:30')
                data = order_ledger.details_for_sheet(order_intake.extract(message), [{}] * 29, [])
                self.assertEqual((data['delivery_date'], data['delivery_time']), ('2026-11-21', '09:30'))
                self.assertIn('11.21 09:30', order_intake.report(1001, data))

    def test_event_time_does_not_fill_missing_delivery_time_in_report(self):
        event = {'event_type': '결혼', 'recipient': '모의 수신자', 'event_date': '2026-11-21', 'event_time': '13:30'}
        with patch.object(order_intake, 'wedding', return_value=event):
            data = order_intake.extract('주문 접수\nhttps://salondeletter.com/w/example\n보내는 분: 모의 직원')
        self.assertEqual((data['delivery_date'], data['delivery_time']), ('', ''))
        self.assertIn('배송 일시: 확인 필요', order_intake.report(1001, data))

    def test_linked_order_uses_explicit_address_when_invitation_has_none(self):
        for invitation_address in ('', '부산 동구 가상로 22'):
            with self.subTest(invitation_address=invitation_address):
                event = {'event_type': '결혼', 'recipient': '모의 수신자', 'venue': '가상 예식장',
                         'address': invitation_address}
                message = ('주문 접수\nhttps://salondeletter.com/w/example\n'
                           '주소: 부산 동구 가상로 10\n보내는 분: 모의 직원')
                with patch.object(order_intake, 'wedding', return_value=event):
                    details = order_intake.extract(message)
                cells = [{} for _ in range(29)]
                cells[8] = {'dataValidation': {'condition': {'type': 'ONE_OF_LIST',
                            'values': [{'userEnteredValue': '부산센터A3'}]}}}
                data = order_ledger.details_for_sheet(details, cells, [['부산센터A3', '부산센터', '', True]])
                self.assertEqual(data['address'], invitation_address or '부산 동구 가상로 10')
                self.assertEqual(data['channel'], '부산센터A3')
                self.assertEqual(data['sender'], '모의 직원')
