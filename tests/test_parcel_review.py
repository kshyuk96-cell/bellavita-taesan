import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent
import parcel_review


class ParcelReviewTest(unittest.TestCase):
    def test_review_reports_registered_exact_region_but_not_address(self):
        rows = [['주소 확인 필요: 2건'], parcel_review.HEADERS,
                [12, '강원특별자치도 춘천시 가상로 90', '지명 시트 확인 필요'],
                [14, '미등록읍 구로산책로 30', '다른 메모']]
        answer = parcel_review.summarize(lambda _: [rows, [['춘천, 구로, 중']]])
        self.assertIn('미분류 2건', answer)
        self.assertIn('8행 · 주문번호 12', answer)
        self.assertIn('지명 후보 춘천', answer)
        self.assertIn('9행 · 주문번호 14', answer)
        self.assertIn('확인된 지명 후보 없음', answer)
        self.assertNotIn('가상로', answer)
        self.assertNotIn('구로산책로', answer)
        self.assertNotIn('다른 메모', answer)

    def test_empty_and_mismatched_schema(self):
        self.assertIn('없어요', parcel_review.summarize(
            lambda _: [[['주소 확인 필요: 0건'], parcel_review.HEADERS], [['춘천']]]))
        with self.assertRaises(ValueError):
            parcel_review.summarize(lambda _: [[['주소 확인 필요: 1건'], ['새 머리글']], [['춘천']]])

    def test_employee_only_read_refreshes_without_consuming_invitation(self):
        def dispatch(owner, message):
            response = agent.dispatch({'userRequest': {'user': {'id': owner}, 'utterance': message}})
            return str(response)

        filled = [['주소 확인 필요: 1건'], parcel_review.HEADERS,
                  [12, '춘천시 가상로 90', '지명 시트 확인 필요']]
        cleared = [['주소 확인 필요: 0건'], parcel_review.HEADERS]
        with tempfile.TemporaryDirectory() as tmp, patch.object(agent, 'ROOT', Path(tmp)), \
             patch.object(agent, 'read_delivery_ranges', side_effect=[[filled, [['춘천']]], [cleared, [['춘천']]]]) as read, \
             patch.object(agent, 'choose') as choose:
            with agent.db() as c:
                c.execute("INSERT INTO staff VALUES ('test-staff','staff',0)")
            self.assertNotIn('주문번호 12', dispatch('new-account', '택배 미분류 보여줘'))
            read.assert_not_called()
            dispatch('test-staff', '초대장')
            self.assertIn('주문번호 12', dispatch('test-staff', '택배 미분류 보여줘'))
            self.assertIn('없어요', dispatch('test-staff', '택배 미분류 보여줘'))
            self.assertEqual(read.call_count, 2)
            self.assertEqual(read.call_args.args[0], parcel_review.RANGES)
            with agent.db() as c:
                self.assertIsNotNone(c.execute("SELECT owner FROM invitation_input WHERE owner='test-staff'").fetchone())
            choose.assert_not_called()
