import base64
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

import checkout
import erp_invitation_library as library
import erp_invitations as bridge
import erp_invitation_render as renderer
import erp_memorial_auto


class MemorialRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key = 'a' * 32
        self.checkout_key = 'b' * 32
        self.folder = self.root / 'erp-invitations' / 'public' / self.key
        self.folder.mkdir(parents=True)
        self.values = {'name': '故 가상고인 부고', 'deceasedName': '가상고인', 'host': '정명천 회장님',
                       'productPrefix': '정명천 회장님 모친상', 'greeting': '직접 정한 위로 문구',
                       'note': '기존 안내', 'date': '2099-01-02', 'time': '09:00', 'venue': '가상장례식장',
                       'address': '기존 주소', 'funeralRoom': '특1호실', 'sourceUrl': 'https://source.test/notice'}
        self.input = {'requestId': 'original-publication', 'invitationId': 'original-invitation', 'version': 1,
                      'kind': 'memorial', 'action': 'publish', 'source': self.values['sourceUrl'],
                      'orderMode': 'invitation-checkout', 'values': self.values}
        self.links = [{'price': price, 'productId': str(price + 1),
                       'url': f'https://app.test/erp/i/{self.key}/{price}.html',
                       'orderUrl': f'https://app.test/i/{self.checkout_key}/{price}/order'} for price in library.PRICES]
        self.manifest = json.dumps({'fingerprint': 'f' * 64, 'items': self.links, 'jobId': self.key,
                                    'expires': '2099-02-02'}).encode()
        (self.folder / 'result.json').write_bytes(self.manifest)
        for name in ['93500.html', '99000.html', 'sample.webp', 'thumbnail.jpg', 'reference-components.json']:
            (self.folder / name).write_bytes(('original-' + name).encode())
        self.snapshots = self.root / 'invitations' / self.checkout_key
        self.snapshots.mkdir(parents=True)
        for price in library.PRICES:
            (self.snapshots / f'{price}.order.json').write_text(json.dumps({
                'localCheckout': True, 'publicationId': self.key, 'releaseFingerprint': 'f' * 64,
                'number': str(price + 1), 'price': price, 'kind': 'memorial', 'title': '기존 상품',
                'event_time': '2099-01-02T09:00:00+09:00'}))
        with sqlite3.connect(self.root / 'state.sqlite3') as db:
            db.execute('CREATE TABLE payments(id TEXT,amount INTEGER)')
            db.execute('INSERT INTO payments VALUES(?,?)', ('original-payment', 99000))
            db.execute('CREATE TABLE orders(id TEXT,payload TEXT)')
            db.execute('INSERT INTO orders VALUES(?,?)', ('original-order', 'original-payload'))
        self.journal = self.root / 'journal.sqlite'
        with sqlite3.connect(self.journal) as db:
            db.execute('CREATE TABLE jobs(public_id TEXT,action TEXT,state TEXT,created_at TEXT,input TEXT)')
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',
                       (self.key, 'publish', 'done', '2026', json.dumps(self.input, ensure_ascii=False)))
        def job_db():
            db = sqlite3.connect(self.journal)
            db.row_factory = sqlite3.Row
            return db
        fresh = {**self.values, 'host': '잘못 자동 선택한 상주', 'productPrefix': '', 'address': '갱신 주소',
                 'mournerAccounts': [{'name': '정명천', 'bank': '가상은행', 'account': '123-456', 'holder': '정명천'},
                                     {'name': '다른상주', 'bank': '다른은행', 'account': '999-999', 'holder': '다른상주'}]}
        self.worker = SimpleNamespace(root=self.root / 'erp-invitations', checkout_root=self.root,
                                      origin='https://app.test', db=job_db,
                                      parse=Mock(return_value={'status': 'done', 'values': fresh, 'warnings': ['확인 문구']}),
                                      prepare=Mock(side_effect=AssertionError('must not create products')))
        self.row = {'id': self.key, 'kind': 'memorial', 'folder': str(self.folder), 'revision': 1,
                    'createdAt': library.now().isoformat(), 'date': '2099-01-02', 'title': self.values['productPrefix'],
                    'orderMode': 'invitation-checkout', 'links': self.links, 'salesStopped': False}
        library.save(self.root, {self.key: self.row})
        image = Image.new('RGBA', (8, 10), (80, 130, 180, 230))
        raw = renderer.image_bytes(image)
        data = renderer.data_image(raw)
        self.parts = {key: data for key in ['BASE', 'FRAME_LEFT', 'FRAME_RIGHT', 'SCROLL_LEFT', 'SCROLL_RIGHT']}
        self.parts.update(senderLeft='쌀빛그림화환', senderRight='정명천 회장', renderVersion=erp_memorial_auto.RENDER_VERSION)
        self.component = patch('erp_memorial_auto.components', return_value=self.parts)
        self.preview = patch('erp_memorial_auto.preview_composite', return_value=(image, []))
        self.thumbnail = patch('erp_invitation_render.thumbnail', return_value=b'refreshed-thumbnail')
        self.component_mock = self.component.start()
        self.preview.start()
        self.thumbnail.start()
        self.addCleanup(self.component.stop)
        self.addCleanup(self.preview.stop)
        self.addCleanup(self.thumbnail.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def original_files(self):
        paths = [self.root / 'state.sqlite3', self.folder / 'result.json',
                 *self.folder.glob('*'), *self.snapshots.glob('*')]
        return {str(path): path.read_bytes() for path in paths if path.is_file()}

    def test_refresh_preserves_urls_products_fingerprint_payments_and_engine_public_serves_new_assets(self):
        originals = self.original_files()
        prior_state = (library.base(self.root) / 'state.json').read_bytes()
        with patch('erp_local_checkout.build', side_effect=AssertionError('no new products')), \
             patch('erp_local_checkout.publish', side_effect=AssertionError('no snapshot publishing')):
            result = library.refresh_memorial(self.worker, self.root, self.key, 'staff')
        row = library.state(self.root)[self.key]
        self.assertEqual(result['links'], self.links)
        self.assertEqual(row['links'], self.links)
        self.assertEqual(row['revision'], 2)
        self.assertEqual(row['renderRefresh']['originalFingerprint'], 'f' * 64)
        self.assertEqual(row['renderRefresh']['accountCount'], 1)
        self.worker.prepare.assert_not_called()
        parsed_input = self.worker.parse.call_args.args[0]
        self.assertEqual(parsed_input['values']['host'], '정명천 회장님')
        rendered = self.component_mock.call_args.args[1]
        self.assertEqual(rendered['host'], '정명천 회장님')
        self.assertEqual(rendered['productPrefix'], '정명천 회장님 모친상')
        self.assertEqual(rendered['greeting'], '직접 정한 위로 문구')
        self.assertEqual(rendered['address'], '갱신 주소')
        self.assertEqual([account['account'] for account in rendered['mournerAccounts']], ['123-456'])
        backup = library.base(self.root) / row['editPath'] / 'backup'
        self.assertEqual((backup / 'state.json').read_bytes(), prior_state)
        self.assertEqual((backup / 'result.json').read_bytes(), self.manifest)
        self.assertEqual((backup / 'sample.webp').read_bytes(), b'original-sample.webp')
        self.assertEqual(self.original_files(), originals)
        for link in self.links:
            status, mime, raw = bridge.Engine.public(self.worker, self.key, f"{link['price']}.html")
            self.assertEqual(status, 200)
            text = raw.decode()
            self.assertIn(link['orderUrl'], text)
            self.assertIn(link['url'], text)
            self.assertIn('memorial-promotion', text)
            self.assertIn('정명천 회장', text)
            self.assertNotIn('999-999', text)
            product = checkout.read(self.root, self.checkout_key, str(link['price']))
            self.assertIsNotNone(product)
            self.assertEqual(product['releaseFingerprint'], 'f' * 64)
            self.assertTrue(checkout.available(product))
        status, mime, raw = bridge.Engine.public(self.worker, self.key, 'sample.webp')
        self.assertEqual((status, mime), (200, 'image/webp'))
        self.assertEqual(Image.open(io.BytesIO(raw)).size, (8, 10))
        self.assertEqual(bridge.Engine.public(self.worker, self.key, 'thumbnail.jpg'),
                         (200, 'image/jpeg', b'refreshed-thumbnail'))

    def test_wrong_kind_missing_external_readonly_and_deleted_records_never_parse_or_write(self):
        for patch_row in [None, {'kind': 'wedding'}, {'external': True}, {'readOnly': True}, {'orderOnly': True}, {'deleted': True}]:
            with self.subTest(patch_row=patch_row):
                data = {} if patch_row is None else {self.key: {**self.row, **patch_row}}
                library.save(self.root, data)
                before = (library.base(self.root) / 'state.json').read_bytes()
                with self.assertRaises(ValueError):
                    library.refresh_memorial(self.worker, self.root, self.key, 'staff')
                self.assertEqual((library.base(self.root) / 'state.json').read_bytes(), before)
        self.worker.parse.assert_not_called()

    def test_failed_render_or_atomic_state_save_leaves_current_publication_and_finances_intact(self):
        originals = self.original_files()
        before = (library.base(self.root) / 'state.json').read_bytes()
        with patch('erp_memorial_auto.page', side_effect=ValueError('render failed')):
            with self.assertRaisesRegex(ValueError, 'render failed'):
                library.refresh_memorial(self.worker, self.root, self.key, 'staff')
        self.assertEqual((library.base(self.root) / 'state.json').read_bytes(), before)
        self.assertEqual(self.original_files(), originals)
        with patch.object(library, 'save', side_effect=OSError('state save failed')):
            with self.assertRaisesRegex(OSError, 'state save failed'):
                library.refresh_memorial(self.worker, self.root, self.key, 'staff')
        self.assertEqual((library.base(self.root) / 'state.json').read_bytes(), before)
        self.assertEqual(self.original_files(), originals)
        self.assertEqual(bridge.Engine.public(self.worker, self.key, '93500.html')[2], b'original-93500.html')


if __name__ == '__main__':
    unittest.main()
