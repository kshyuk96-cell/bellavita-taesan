import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import delivery_routes


class WeekendDeliveryTests(unittest.TestCase):
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
