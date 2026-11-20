"""Association design, complete family list, and selected-account privacy."""
import re
import unittest
from lxml import html

import erp_memorial_page as page


class Renderer:
    @staticmethod
    def date_words(values):
        return '', '2099년 11월 21일', '오전 9시 30분'


class MemorialPageTests(unittest.TestCase):
    def values(self):
        return {
            'name': '故 가상고인 부고', 'deceasedName': '가상고인',
            'deceasedDescription': '여, 향년 80세', 'deathDate': '2099년 11월 18일 별세',
            'venue': '가상장례식장', 'funeralRoom': '특1호실', 'address': '서울시 가상로 10-1',
            'host': '김병섭 상주님',
            'mourners': '상주: 김병섭, 김다른\n자부: 가상며느리\n딸: 가상딸\n사위: 가상사위\n손: 가상손자, 가상손녀\n외손: 가상외손',
            'mournerAccounts': [
                {'name': '김병섭', 'relation': '상주', 'bank': '가상은행', 'account': '111-0000-0001', 'holder': '김병섭'},
                {'name': '김다른', 'relation': '상주', 'bank': '다른은행', 'account': '222-0000-0002'},
            ],
            'entrance': '2099년 11월 19일 오전', 'admission': '2099년 11월 20일 오후 2시',
            'departure': '2099년 11월 21일 오전 9시 30분', 'burial': '가상승화원 / 가상선영',
            'originalNotice': '조문을 부탁드립니다.\n가족 일동', 'contactPhone': '010-0000-0001',
            'funeralPhone': '02-000-0000', 'sourceUrl': 'https://source.test/funeral',
        }

    def render(self, values=None, preview=False):
        return page.page(Renderer(), values or self.values(), 93500,
                         'https://order.test/93500', '', 'https://notice.test/og',
                         'https://notice.test/93500', preview)

    def test_all_six_family_groups_survive_while_only_customer_account_is_exposed(self):
        output = self.render()
        tree = html.fromstring(output)
        family = tree.xpath('//*[contains(@class,"mourner-line")]')
        self.assertEqual(len(family), 6)
        for value in ('김병섭, 김다른', '가상며느리', '가상딸', '가상사위', '가상손자, 가상손녀', '가상외손'):
            self.assertTrue(any(value in row.text_content() for row in family))
        self.assertIn('111-0000-0001', output)
        self.assertNotIn('222-0000-0002', output)
        self.assertNotIn('다른은행', output)
        self.assertEqual(tree.xpath('//button[@data-copy="111-0000-0001"]/text()'), ['계좌번호 복사'])
        self.assertNotIn('상주님', output)

    def test_reference_layout_fonts_and_funeral_detail_fields(self):
        output = self.render()
        for selector in ('obituary classic', 'obit-header', 'obit-section', 'memorial-portrait', 'portrait-frame', 'funeral-details', 'Noto+Sans+KR', 'Noto+Serif+KR'):
            self.assertIn(selector, output)
        for field in ('deceasedDescription', 'deathDate', 'venue', 'funeralRoom', 'address', 'entrance', 'admission', 'departure', 'burial'):
            self.assertIn(self.values()[field], output)
        self.assertIn('href="tel:01000000001"', output)
        self.assertIn('href="tel:020000000"', output)
        self.assertIn('백합 추모 디자인', output)
        # Kept exactly once for the approved real two-product banner wrapper.
        self.assertEqual(len(re.findall(r'<section class="products">.*?</section>', output, re.S)), 1)

    def test_accounts_fail_closed_without_target_or_when_target_account_ambiguous(self):
        values = self.values()
        values['host'] = ''
        self.assertNotIn('111-0000-0001', self.render(values))
        values['host'] = '김병섭'
        values['mournerAccounts'].append({'name': '김병섭', 'bank': '세번째은행', 'account': '333-0000-0003'})
        output = self.render(values)
        self.assertNotIn('111-0000-0001', output)
        self.assertNotIn('333-0000-0003', output)

    def test_untrusted_names_notice_and_bank_copy_are_escaped_and_bad_urls_rejected(self):
        values = self.values()
        values['mourners'] += '\n가족: <img src=x onerror=alert(1)>'
        values['originalNotice'] = '<script>alert("notice")</script>'
        values['portrait'] = 'javascript:alert(1)'
        values['sourceUrl'] = 'javascript:alert(2)'
        values['contactPhone'] = '010-1234-5678" onclick="alert(3)'
        values['mournerAccounts'][0]['account'] = '111" onclick="alert(4)'
        output = self.render(values)
        tree = html.fromstring(output)
        self.assertFalse(tree.xpath('//*[@onclick or @onerror]'))
        self.assertFalse(tree.xpath('//a[starts-with(@href,"javascript:")]'))
        self.assertIn('&lt;script&gt;', output)
        self.assertEqual(tree.xpath('//button[starts-with(@data-copy,"111")]/@data-copy'), ['111" onclick="alert(4)'])
        self.assertEqual(len(tree.xpath('//script')), 1)

    def test_https_portrait_and_preview_payment_link(self):
        values = self.values()
        values['portrait'] = 'https://source.test/portrait.jpg?a=1&b=2'
        output = self.render(values, preview=True)
        tree = html.fromstring(output)
        self.assertEqual(tree.xpath('//img[@class="portrait-image"]/@src'), [values['portrait']])
        self.assertNotIn('href="https://order.test/93500"', output)
        self.assertIn('결제 연결 비활성', output)
        self.assertIn('href="https://order.test/93500"', self.render(values))


if __name__ == '__main__':
    unittest.main()
