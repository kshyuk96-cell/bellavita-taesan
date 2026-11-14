import datetime as dt
import json
import sys
from pathlib import Path
from urllib.parse import urlencode,parse_qs,urlsplit
from lxml import html
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_shop import ShopFixture,SECRET
import agent
import checkout
import shop
import shop_admin_queries


class AdminUsabilityTest(ShopFixture):
    def seed_orders(self):
        with agent.db() as c:
            for i in range(70):
                fields=dict(self.fields(),sender='운영 검증 보내는 분',buyer='찾는 주문자' if i==69 else '다른 주문자')
                data=checkout.normalize_order(self.product,fields);data['event_time']='2026-12-'+str(10+i%3).zfill(2)+'T12:00:00+09:00'
                state='paid' if i%2 else 'waiting';identity=f'{i:032x}'
                c.execute('INSERT INTO invitation_orders VALUES (?,?,?,?,?,?,NULL,NULL)',(identity,self.offer,93500,f'2026-11-01T00:00:{i%60:02}',json.dumps(data,ensure_ascii=False),state))
                if state=='paid':c.execute('INSERT INTO shop_fulfillment VALUES (?,?,1,?)',(identity,'shipping' if i%3 else 'confirmed','2026-11-01'))

    def test_search_full_records_phone_filters_and_paging(self):
        self.seed_orders()
        with agent.db() as c:
            rows,meta=shop_admin_queries.select(c,{});self.assertEqual(len(rows),30);self.assertEqual(meta['total'],70);self.assertEqual(meta['pages'],3)
            rows,meta=shop_admin_queries.select(c,{'q':'찾는 주문자'});self.assertEqual(len(rows),1);self.assertEqual(rows[0]['id'],f'{69:032x}')
            rows,meta=shop_admin_queries.select(c,{'q':'010-1234-5678','state':'paid','stage':'shipping','from_date':'2026-12-11','to_date':'2026-12-12'});self.assertTrue(rows);self.assertTrue(all(r['state']=='paid' for r in rows));self.assertLess(meta['total'],70)
            rows,meta=shop_admin_queries.select(c,{'page':'99999'});self.assertEqual(meta['page'],3);self.assertEqual(len(rows),10)
            rows,meta=shop_admin_queries.select(c,{'q':"' OR 1=1 --"});self.assertEqual(meta['total'],0)
        status,_,body=self.http(self.admin+'?'+urlencode({'q':'找不到','page':'3'}));self.assertEqual(status,200);self.assertIn('조건에 맞는 주문이 없습니다',body)
        self.assertEqual(self.http(self.admin+'?from_date=2026-12-20&to_date=2026-12-01')[0],422)

    def test_admin_forms_return_context_and_invalid_navigation_cannot_mutate(self):
        status,_,body=self.http(self.admin+'?'+urlencode({'q':'운영','state':'paid'}));self.assertEqual(status,200)
        tree=html.fromstring(body);forms=tree.xpath('//form[@method="post"]');self.assertTrue(forms)
        for form in forms:self.assertEqual(len(form.xpath('.//input[@name="_return_query"]')),1)
        self.assertTrue(shop_admin_queries.redirect(self.admin,{'product_id':self.product_id}).endswith('#product-'+self.product_id))
        query=urlencode({'q':'운영','state':'paid','page':'2'})
        fields=dict(action='change_offer',offer_id=self.offer,active='0',_return_query=query,_return_anchor='admin-products')
        status,headers,_=self.http(self.admin,fields);self.assertEqual(status,303);location=headers['Location'];self.assertTrue(location.startswith(self.admin+'?'));self.assertEqual(urlsplit(location).fragment,'admin-products');self.assertEqual(parse_qs(urlsplit(location).query)['q'],['운영'])
        status,_,body=self.http(self.admin,dict(fields,active='1',_return_query='next=https://evil.test'))
        self.assertEqual(status,422);self.assertIn('입력 화면으로 돌아가기',body)
        with agent.db() as c:self.assertEqual(c.execute('SELECT active FROM shop_offers WHERE id=?',(self.offer,)).fetchone()[0],0)

    def test_next_action_copy_and_manual_share_admin_authority(self):
        self.seed_orders();status,_,body=self.http(self.admin);self.assertEqual(status,200)
        self.assertIn('다음 할 일:',body);self.assertIn('data-copy-order',body);self.assertIn('고객 상태 링크 복사',body)
        status,_,manual=self.http(self.admin+'/manual');self.assertEqual(status,200);self.assertIn('manual-search',manual);self.assertIn('결제 확인과 결과 불명',manual)
        status,headers,manual=self.http(self.admin+'/manual.md');self.assertEqual(status,200);self.assertEqual(manual,Path('docs/shop-admin-manual.md').read_text())
        expiry='1000000000';bad='/shop/admin/'+expiry+'/'+shop.admin_signature(expiry,SECRET)
        self.assertEqual(self.http(bad+'/manual')[0],403)
        self.assertEqual(self.http(bad+'/manual.md')[0],403)
