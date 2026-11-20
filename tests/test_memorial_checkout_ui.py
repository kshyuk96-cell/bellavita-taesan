import json
import tempfile
import unittest
from pathlib import Path

from lxml import html

import checkout


class MemorialCheckoutUI(unittest.TestCase):
    def product(self):
        return {
            'number': '1', 'title': '가상 상주 모친상', 'price': 93500,
            'images': ['https://example.test/erp/i/' + 'a' * 32 + '/sample.webp'],
            'groups': [
                {'label': '근조 문구', 'choices': {'1': {'label': '과거 문구', 'amount': 0, 'custom': False}}},
                {'label': '받으시는 분', 'choices': {'1': {'label': '故 가상고인', 'amount': 0, 'custom': False}}},
                {'label': '상품 선택', 'choices': {'1': {'label': '쌀빛그림화환(쌀 10kg)', 'amount': 0, 'custom': False}}},
            ],
            'date': '2099-01-02', 'clock': '09:00', 'venue': '가상장례식장',
            'address': '가상로 1', 'event_time': '2099-01-02T09:00:00+09:00',
            'kind': 'memorial', 'localCheckout': True, 'publicationId': 'a' * 32,
        }

    def test_existing_snapshot_gets_four_messages_and_one_live_rice_sample(self):
        product = checkout.apply_memorial_rules(self.product())
        output = checkout.render(product, '#', False)
        tree = html.fromstring(output)
        fixed = tree.xpath('//select[@id="greeting-choice"]/option[@data-greeting]')
        self.assertEqual([node.get('data-greeting') for node in fixed], list(checkout.MEMORIAL_MESSAGES))
        self.assertEqual(len(tree.xpath('//figure[@id="wreath-preview" and contains(@class,"memorial-preview")]')), 1)
        self.assertEqual(len(tree.xpath('//figure[@id="wreath-preview"]//img[contains(@class,"memorial-frame-photo")]')), 1)
        self.assertFalse(tree.xpath('//figure[@id="wreath-preview"]//img[contains(@class,"scroll-couple")]'))
        self.assertEqual(tree.xpath('//figure[@id="wreath-preview"]/@data-preview-kind'), ['memorial'])
        self.assertIn("sender.addEventListener('focus', resetCloseUp)", output)
        self.assertIn("message.addEventListener('focus', showCloseUp)", output)

    def test_memorial_weight_uses_one_complete_artwork_without_duplicate_text(self):
        tree = html.fromstring(checkout.render(checkout.apply_memorial_rules(self.product()), '#', False))
        mark = tree.xpath('//div[@class="memorial-rice-mark"]')[0]
        self.assertEqual(mark.xpath('./img/@id'), ['scroll-weight'])
        self.assertEqual(mark.xpath('./img/@src'), ['/checkout-art/rice-10kg.webp'])
        self.assertFalse(mark.xpath('.//span|.//strong|.//small'))
        self.assertFalse(tree.xpath('//*[@id="scroll-weight-label"]'))
        icon = tree.xpath('//link[@rel="icon"]/@href')
        self.assertEqual(len(icon), 1)
        self.assertTrue(icon[0].startswith('data:image/svg+xml,'))

    def test_read_upgrades_old_published_snapshot_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            publication = 'a' * 32
            job = 'b' * 32
            manifest = root / 'erp-invitations' / 'public' / publication
            manifest.mkdir(parents=True)
            (manifest / 'result.json').write_text(json.dumps({'fingerprint': 'f' * 64}))
            folder = root / 'invitations' / job
            folder.mkdir(parents=True)
            product = self.product()
            product.update(publicationId=publication, releaseFingerprint='f' * 64)
            target = folder / '93500.order.json'
            original = json.dumps(product, ensure_ascii=False)
            target.write_text(original)
            loaded = checkout.read(root, job, '93500')
            greeting = next(group for group in loaded['groups'] if group['label'] == '근조 문구')
            self.assertEqual(list(greeting['choices']), ['0', '1', '2', '3', '4'])
            self.assertEqual(target.read_text(), original)

    def test_memorial_thumbnail_lines_use_customer_relationship_and_death_date(self):
        from erp_invitation_render import memorial_thumbnail_lines
        values = {'host': '정명천 회장님', 'productPrefix': '정명천 회장님 모친상',
                  'deceasedName': '오금순', 'deathDate': '2026년 11월 19일 15:00 별세'}
        self.assertEqual(memorial_thumbnail_lines(values), [
            '정명천 상주의 모친', '故 오금순님께서',
            '2026년 11월 19일 별세 하셨음을', '삼가 알려드립니다.',
        ])


if __name__ == '__main__':
    unittest.main()
