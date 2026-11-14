import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

import delivery_reports
import delivery_routes


class WeekendDeliveryTests(unittest.TestCase):
    def test_missing_address_and_event_time_are_identified_in_chat_and_excel(self):
        day = date(2026, 11, 21)
        serial = (day - delivery_routes.EPOCH).days
        cases = (
            ('1001', '', '', '배송지와 행사시간 확인 필요'),
            ('1002', '', '13:00', '배송지 확인 필요'),
            ('1003', '가상 예식장', '', '행사시간 확인 필요'),
        )
        selected, fees, ledger = [], [[]], {}
        for index, (number, address, event, _) in enumerate(cases, 1):
            selected.append((index, [number, serial, event, address]))
            fees.append([number, serial, '구로', '구로', '', address])
            ledger[number] = [serial, event, '', '', address, '결혼', '', '구로']
        inputs = ([[], ['구로'], ['구로'], ['구로']], [[], [], [], []], [[], [], [], []],
                  fees, [], [['배송원명']], [], [], selected, ledger, '가상 센터')
        settings = {'days': [day.isoformat()], 'skipOrders': [], 'manualTimes': {},
                    'excluded': [], 'nearGap': 120, 'delay': 0}

        with patch.object(delivery_routes, 'workbook', return_value=inputs):
            plans = delivery_routes.calculate(settings, date(2026, 11, 14), 540, Mock(), Mock())

        plan = plans[day.isoformat()]
        self.assertEqual(plan['routes'], [])
        lines = delivery_routes.format_result(day.isoformat(), plan, settings)
        workbook = load_workbook(BytesIO(delivery_reports.excel(plans)))
        self.assertCountEqual([(r['order'], r['reason']) for r in plan['missed']],
                              [(number, reason) for number, _, _, reason in cases])
        for number, _, _, reason in cases:
            self.assertIn(f'{number}번 · {reason}', lines)
        self.assertCountEqual(list(workbook['미배정'].values)[1:],
                              [(day.isoformat(), number, reason) for number, _, _, reason in cases])

    def test_weekend_uses_only_dates_that_have_not_passed(self):
        cases = (
            (date(2026, 11, 14), '이번 주말', ['2026-11-14', '2026-11-15']),
            (date(2026, 11, 15), '이번 주말', ['2026-11-15']),
            (date(2026, 11, 15), '토요일 일요일 배송루트', ['2026-11-15']),
            (date(2026, 11, 15), '다음 주말', ['2026-11-21', '2026-11-22']),
        )
        for today, message, expected in cases:
            with self.subTest(today=today, message=message):
                self.assertEqual(delivery_routes.requested_days(message, today), expected)

    def test_sunday_quick_reply_calculates_and_saves_sunday_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.sqlite3'

            def db():
                return sqlite3.connect(path)

            with closing(db()) as connection:
                delivery_routes.setup(connection)
                connection.commit()

            now = datetime(2026, 11, 15, 9, tzinfo=ZoneInfo('Asia/Seoul'))
            day = '2026-11-15'
            calculator = Mock(return_value={day: {'routes': [], 'missed': [], 'held': 0}})
            with patch.object(delivery_routes, 'calculate', calculator):
                result = delivery_routes.run('이번 주말', '가상직원', db, Mock(), now)

            self.assertEqual(calculator.call_args.args[0]['days'], [day])
            self.assertEqual(list(result['route_report']), [day])
            self.assertEqual(delivery_routes.session(db, '가상직원')['days'], [day])
            self.assertIsNone(delivery_routes.session(db, '다른직원'))
            with self.assertRaisesRegex(ValueError, '오늘 이후'):
                delivery_routes.run('2026-11-14 구로 배송루트', '가상직원', db, Mock(), now)


if __name__ == '__main__':
    unittest.main()
