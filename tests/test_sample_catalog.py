import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import checkout
import seed_shop_demo


class SampleCatalogTest(unittest.TestCase):
    def test_five_products_ten_links_and_product_specific_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'samples'
            first=seed_shop_demo.seed(root)
            again=seed_shop_demo.seed(root)
            self.assertEqual(first,again)
            self.assertEqual(5,len(first));self.assertEqual(10,sum(len(item['offers']) for item in first))
            with sqlite3.connect(root/'state.sqlite3') as c:
                c.row_factory=sqlite3.Row
                self.assertEqual(5,c.execute('SELECT COUNT(*) FROM shop_products').fetchone()[0])
                self.assertEqual(10,c.execute('SELECT COUNT(*) FROM shop_offers').fetchone()[0])
                self.assertEqual(0,c.execute('SELECT COUNT(*) FROM invitation_orders').fetchone()[0])
                for item in json.loads(seed_shop_demo.CATALOG.read_text()):
                    row=next(row for row in c.execute('SELECT * FROM shop_products') if json.loads(row['details'])['title']==item['title'])
                    product=json.loads(c.execute('SELECT details FROM shop_offers WHERE product_id=? ORDER BY price LIMIT 1',(row['id'],)).fetchone()[0])
                    for code,weight in enumerate(item['weights'],1):
                        normalized=checkout.normalize_order(product,dict(choice0=str(code),choice1='1',choice2='1',
                            sender='샘플 보내는 분',buyer='샘플 주문자',buyer_phone='01012345678',consent='yes'))
                        self.assertEqual(item['prices'][0]+weight['extra'],normalized['amount'])
                        self.assertIn(f'{weight["kg"]}kg',normalized['options'][0][1])
                    self.assertEqual(item['greetings'][0],dict(normalized['options'])['축하 문구'])

    def test_existing_environment_and_wrong_origin_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'existing';root.mkdir();(root/'state.sqlite3').write_text('existing')
            with self.assertRaises(ValueError):seed_shop_demo.seed(root)
            self.assertEqual('existing',(root/'state.sqlite3').read_text())
            with self.assertRaises(ValueError):seed_shop_demo.seed(Path(directory)/'new','http://example.com')
