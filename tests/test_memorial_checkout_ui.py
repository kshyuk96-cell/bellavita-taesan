import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from lxml import html
from PIL import Image, ImageStat

import checkout


class MemorialCheckoutUI(unittest.TestCase):
    def product(self):
        return {
            'number': '1', 'title': '가상 상주 모친상 · 가상장례식장 특1호실', 'price': 93500,
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

    def test_existing_snapshot_gets_six_messages_and_one_live_rice_sample(self):
        product = checkout.apply_memorial_rules(self.product())
        output = checkout.render(product, '#', False)
        tree = html.fromstring(output)
        fixed = tree.xpath('//select[@id="greeting-choice"]/option[@data-greeting]')
        self.assertEqual([node.get('data-greeting') for node in fixed], list(checkout.MEMORIAL_MESSAGES))
        self.assertEqual(len(fixed), 6)
        self.assertEqual([node.get('data-greeting') for node in fixed[-2:]], [
            '삼가 고인의 명복을 빕니다.', '삼가 故人의 冥福을 빕니다.',
        ])
        self.assertEqual(len(tree.xpath('//figure[@id="wreath-preview" and contains(@class,"memorial-preview")]')), 1)
        self.assertEqual(len(tree.xpath('//figure[@id="wreath-preview"]//img[contains(@class,"memorial-frame-photo")]')), 1)
        self.assertFalse(tree.xpath('//figure[@id="wreath-preview"]//img[contains(@class,"scroll-couple")]'))
        self.assertEqual(tree.xpath('//figure[@id="wreath-preview"]/@data-preview-kind'), ['memorial'])
        self.assertEqual(tree.xpath('//a[contains(@class,"memorial-rice-qr")]/@href'),
                         ['https://form.jotform.com/251220921797458'])
        self.assertFalse(tree.xpath('//a[contains(@class,"memorial-rice-qr")]//img'))
        notice = ' '.join(tree.xpath('//p[contains(@class,"memorial-rice-notice")]')[0].text_content().split())
        self.assertIn('설치 액자 또는 쌀을 받으시거나, 상주님 명의로 쌀을 기부하실 수 있습니다.', notice)
        self.assertIn('발인 후 5일까지 회신 없으시면 쌀을 기부처리합니다.', notice)
        self.assertIn("const greeting = selected?.dataset.custom === '1'", output)
        self.assertIn("form.addEventListener('change', update);", output)
        self.assertIn("sender.addEventListener('focus', resetCloseUp)", output)
        self.assertIn("message.addEventListener('focus', showCloseUp)", output)
        self.assertIn('.memorial-frame-photo { position: absolute;', output)
        self.assertIn('object-fit: contain; background: #fff;', output)

    def test_memorial_weight_uses_one_complete_artwork_without_duplicate_text(self):
        tree = html.fromstring(checkout.render(checkout.apply_memorial_rules(self.product()), '#', False))
        base = tree.xpath('//img[contains(@class,"memorial-scroll-base")]')[0]
        self.assertEqual(base.get('id'), 'scroll-weight')
        self.assertEqual(base.get('src'), '/checkout-art/memorial-rice-10kg-base.webp')
        self.assertFalse(tree.xpath('//*[contains(@class,"memorial-rice-mark")]'))
        self.assertFalse(tree.xpath('//*[@id="scroll-weight-label"]'))
        output = checkout.render(checkout.apply_memorial_rules(self.product()), '#', False)
        self.assertIn("memorialPreview\n    ? '/checkout-art/memorial-rice-10kg-base.webp'", output)
        self.assertIn(": '/checkout-art/rice-' + kg + 'kg.webp'", output)
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
            self.assertEqual(list(greeting['choices']), ['0', '1', '2', '3', '4', '5', '6'])
            self.assertEqual([group['label'] for group in loaded['groups']], ['근조 문구', '상품 선택'])
            self.assertEqual(loaded['title'], '가상상주님 모친상 · 가상장례식장 특1호실')
            self.assertEqual(target.read_text(), original)

    def test_memorial_hides_fixed_recipient_and_details_and_shows_funeral_room(self):
        product = checkout.apply_memorial_rules(self.product())
        self.assertEqual([group['label'] for group in product['groups']], ['근조 문구', '상품 선택'])
        output = checkout.render(product, '#', False)
        tree = html.fromstring(output)
        self.assertFalse(tree.xpath('//*[@id="open-detail" or @id="product-detail"]'))
        self.assertNotIn('상세설명 보기', output)
        self.assertFalse(tree.xpath('//label[contains(normalize-space(.), "받으시는 분")]'))
        venue = ' '.join(tree.xpath('//p[contains(@class,"venue")]')[0].text_content().split())
        self.assertIn('가상장례식장 특1호실', venue)
        order = checkout.normalize_order(product, {
            'consent': 'yes', 'choice0': '1', 'choice1': '1',
            'sender': '가상 보내는 분', 'same_buyer': 'yes', 'buyer_phone': '01012345678',
        })
        self.assertEqual([label for label, _ in order['options']], ['근조 문구', '상품 선택'])
        self.assertEqual(order['venue'], '가상장례식장 특1호실')

    def test_wedding_recipient_and_product_details_remain_unchanged(self):
        product = self.product()
        product['kind'] = 'wedding'
        product['groups'][0]['label'] = '축하 문구'
        checkout.apply_memorial_rules(product)
        output = checkout.render(product, '#', False)
        tree = html.fromstring(output)
        self.assertEqual([group['label'] for group in product['groups']], ['축하 문구', '받으시는 분', '상품 선택'])
        self.assertEqual(len(tree.xpath('//*[@id="open-detail"]')), 1)
        self.assertEqual(len(tree.xpath('//*[@id="product-detail"]')), 1)
        self.assertFalse(tree.xpath('//*[contains(@class,"memorial-rice-notice") or contains(@class,"memorial-rice-qr")]'))
        self.assertEqual(len(tree.xpath('//label[contains(normalize-space(.), "받으시는 분")]/select[@required]')), 1)
        venue = ' '.join(tree.xpath('//p[contains(@class,"venue")]')[0].text_content().split())
        self.assertNotIn('특1호실', venue)
        self.assertIn('const choiceTotal = choices.length;', output)

    def test_approved_memorial_base_is_real_production_artwork(self):
        path = checkout.artwork_path('memorial-rice-10kg-base.webp')
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())
        self.assertEqual(checkout.artwork_mime(path.name), 'image/webp')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                         'f6f9e7e72c4b15b59baad53cc4edfa9d66e24d87cd0fe29e717783ff00872648')
        with Image.open(path) as image:
            self.assertEqual(image.size, (1684, 2382))
            # A synthetic flat/gradient replacement cannot satisfy the star
            # field's channel variation in the lower production background.
            variation = ImageStat.Stat(image.convert('RGB').crop((1100, 1300, 1500, 2100))).stddev
        self.assertGreater(min(variation), 20)

    def test_live_boxes_share_the_production_pdf_geometry(self):
        from wedding_scroll.render import (make_memorial_rice_checkout_base,
                                            memorial_rice_checkout_geometry)
        geometry = memorial_rice_checkout_geometry()
        page = geometry['page']
        css = checkout.TEMPLATE.read_text(encoding='utf-8')
        message = geometry['message']
        sender = geometry['sender']
        qr = geometry['qr']
        self.assertIn(f'inset: {message.y0 / page.height * 100:.6f}% '
                      f'{(page.width-message.x1) / page.width * 100:.6f}% '
                      f'{(page.height-message.y1) / page.height * 100:.6f}%', css)
        self.assertIn(f'inset: {sender.y0 / page.height * 100:.6f}% '
                      f'{(page.width-sender.x1) / page.width * 100:.6f}% '
                      f'{(page.height-sender.y1) / page.height * 100:.6f}%', css)
        self.assertIn(f'left: {qr.x0 / page.width * 100:.6f}%', css)
        self.assertIn(f'top: {qr.y0 / page.height * 100:.6f}%', css)
        self.assertNotIn('radial-gradient', css)
        self.assertNotIn('inset: 64.5%', css)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, '10kg'):
                make_memorial_rice_checkout_base(Path(temp) / 'invented-20kg.webp', 20)

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
