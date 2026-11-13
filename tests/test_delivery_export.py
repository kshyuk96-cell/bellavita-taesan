from io import BytesIO
import unittest

from openpyxl import load_workbook

import delivery_reports


class DeliveryExportTests(unittest.TestCase):
    def test_long_delivery_notes_wrap_and_keep_the_full_text(self):
        address = '서울 구로구 가상로 10 모의예식장 본관 3층 로비 안내데스크 앞'
        reason = '설치 장소 상세 주소와 행사장 도착 가능 시간을 확인해야 합니다. ' * 3
        visit = {'orders': ['1001'], 'event': 780, 'done': 710, 'address': address,
                 'quantity': 1, 'carried': 1, 'repickup': False}
        days = {'2026-11-15': {'routes': [{'name': '모의 배송원', 'visits': [visit]}],
                               'missed': [{'order': '1002', 'reason': reason}], 'held': 0}}
        workbook = load_workbook(BytesIO(delivery_reports.excel(days)))
        for sheet, address_cell, value in (
            ('배송원별 일정', 'G2', address),
            ('미배정', 'C2', reason),
        ):
            with self.subTest(sheet=sheet):
                cell = workbook[sheet][address_cell]
                self.assertEqual(cell.value, value)
                self.assertTrue(cell.alignment.wrap_text)
        self.assertTrue(workbook['배정 요약']['G2'].alignment.wrap_text)
