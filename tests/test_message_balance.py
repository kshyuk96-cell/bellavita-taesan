import unittest

import fitz
from PIL import Image
from reportlab.pdfbase import pdfmetrics

from wedding_scroll.render import FONT_NAME, balanced_message_lines, write_custom_message


class MessageBalanceTest(unittest.TestCase):
    def test_default_condolence_uses_balanced_three_rows(self):
        text = '하늘의 별이 되신 고인을 추모하며 유가족 분들께 깊은 위로의 마음을 전합니다'
        expected = ['하늘의 별이 되신 고인을', '추모하며 유가족 분들께', '깊은 위로의 마음을 전합니다']
        width = max(pdfmetrics.stringWidth(row, FONT_NAME, 20) for row in expected) + 1
        self.assertEqual(expected, balanced_message_lines(text, 20, width, 3))

    def test_long_message_can_balance_across_more_rows(self):
        text = '함께한 모든 순간을 오래도록 기억하며 깊은 슬픔 가운데 계신 유가족 모든 분들께 진심 어린 위로와 평안의 마음을 전합니다'
        rows = balanced_message_lines(text, 20, 250, 4)
        self.assertEqual(4, len(rows))
        self.assertEqual(' '.join(text.split()), ' '.join(rows))

    def test_long_message_ink_stays_inside_its_fixed_box(self):
        text = '하늘의 별이 되신 고인을 추모하며 깊은 슬픔 가운데 계신 유가족 모든 분들께 진심 어린 위로와 평안의 마음을 전합니다'
        box = fitz.Rect(45, 350, 797, 550)
        with fitz.open() as document:
            page = document.new_page(width=841.89, height=1190.55)
            write_custom_message(page, text, box, 38, (.99, .96, .89), 1)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=True)
            alpha = Image.frombytes('RGBA', (pixmap.width, pixmap.height), pixmap.samples).getchannel('A')
            bounds = alpha.getbbox()
            self.assertIsNotNone(bounds)
            ink = fitz.Rect(*(value / 2 for value in bounds))
            self.assertGreaterEqual(ink.x0, box.x0 - 1)
            self.assertGreaterEqual(ink.y0, box.y0 - 1)
            self.assertLessEqual(ink.x1, box.x1 + 1)
            self.assertLessEqual(ink.y1, box.y1 + 1)


if __name__ == '__main__':
    unittest.main()
