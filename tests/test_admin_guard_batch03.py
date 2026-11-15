"""Column whitelist, shared filter/export contract and manual link authorization."""
import csv
import io
import time
from urllib.parse import urlencode,urlsplit
from lxml import html
from test_shop import ShopFixture,SECRET
import test_admin_usability as baseline
import shop
import shop_admin_manual
import shop_admin_review


class AdminGuardBatch03Test(ShopFixture):
    def test_selected_csv_columns_keep_filter_mask_and_original_default(self):
        baseline.AdminUsabilityTest.seed_orders(self)
        query=urlencode({'state':'paid','columns':'order_id,phone,state','page':'3'})
        status,headers,body=self.http(self.admin+'/orders.csv?'+query)
        self.assertEqual(status,200);self.assertIn('no-store',headers['Cache-Control'])
        rows=list(csv.reader(io.StringIO(body.lstrip('\ufeff'))));self.assertEqual(len(rows),36)
        self.assertEqual(rows[0],['주문번호','휴대폰(가림)','결제 상태'])
        self.assertTrue(all(len(row)==3 and row[1]=='010-****-5678' for row in rows[1:]))
        original=self.http(self.admin+'/orders.csv')[2]
        self.assertEqual(len(next(csv.reader(io.StringIO(original.lstrip('\ufeff'))))),13)
        minimum=self.http(self.admin+'/orders.csv?columns=order_id')[2]
        self.assertEqual(next(csv.reader(io.StringIO(minimum.lstrip('\ufeff')))),['주문번호'])

    def test_invalid_column_selection_is_rejected_without_writes(self):
        baseline.AdminUsabilityTest.seed_orders(self)
        for value in ('','buyer','order_id,unknown','order_id,order_id','order_id;DROP TABLE invitation_orders','order_id,'+'x'*400):
            self.assertEqual(self.http(self.admin+'/orders.csv?'+urlencode({'columns':value}))[0],422)
        with __import__('agent').db() as c:self.assertEqual(c.execute('SELECT count(*) FROM invitation_orders').fetchone()[0],70)
        expiry=str(int(time.time())-1)
        expired='/shop/admin/'+expiry+'/'+shop.admin_signature(expiry,SECRET)
        self.assertEqual(self.http(expired+'/orders.csv?columns=order_id')[0],403)
        self.assertEqual(self.http(expired+'/manual')[0],403)

    def test_context_manual_links_resolve_to_existing_authorized_sections(self):
        status,_,body=self.http(self.admin);self.assertEqual(status,200)
        tree=html.fromstring(body);links=tree.xpath('//a[@class="context-manual"]/@href');self.assertEqual(len(links),5)
        for link in links:
            parsed=urlsplit(link);status,_,manual=self.http(parsed.path)
            self.assertEqual(status,200);self.assertTrue(html.fromstring(manual).xpath('//*[@id="'+parsed.fragment+'"]'))
        self.assertIn('관리자 매뉴얼',shop_admin_manual.page(self.admin))
        self.assertEqual(shop_admin_review.selected_columns({'columns':'state,order_id'}),['order_id','state'])
