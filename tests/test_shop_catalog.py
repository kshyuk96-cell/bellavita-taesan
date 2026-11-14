"""Four-product native HTTP checkout and immutable editor integration."""
import datetime as dt
import http.client
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_shop import ShopFixture,SECRET,ORIGIN
import agent
import checkout
import manage_data
import shop
import shop_catalog


class CatalogTest(ShopFixture):
    def setUp(self):
        super().setUp()
        with agent.db() as c:
            self.ids=shop_catalog.seed(c)
            self.rows=c.execute("SELECT * FROM shop_products WHERE id IN ("+','.join('?'*4)+") ORDER BY rowid",self.ids).fetchall()
        self.identity=self.rows[0]['id'];self.source=json.loads(self.rows[0]['details'])

    def catalog_fields(self,attempt='c'*32):
        p=self.source
        return dict(attempt=attempt,product_revision='1',sender='샘플 보내는 분',buyer='검증 주문자',buyer_phone='01012345678',consent='yes',message='축하합니다',venue='샘플 설치 홀',address='검증 전용 설치 주소',event_time=(dt.datetime.now(checkout.SEOUL)+dt.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M'),**{'choice'+str(i):next(iter(g['choices'])) for i,g in enumerate(p['groups'])},**{'extra'+str(i):'없음' for i,_ in enumerate(p.get('extra_fields',[]))})

    def place_catalog(self,fields=None):return self.http('/shop/products/'+self.identity,fields or self.catalog_fields())

    def editor(self,p=None,revision='1'):
        p=p or self.source
        f=dict(action='save_product',product_id=self.identity,revision=revision,title=p['title'],price='120000',images='\n'.join(p['images']),cash_tax_type='',groups_mode='1',group_count=str(len(p['groups'])),catalog_edit='1',public='yes',available='yes',category=p['category'],description='새 상품 안내')
        for i,g in enumerate(p['groups']):
            f['group_label'+str(i)]=g['label'];f['group_choices'+str(i)]='\n'.join(v['label']+' | '+str(v['amount']) for v in g['choices'].values() if not v['custom'])
            if any(v['custom'] for v in g['choices'].values()):f['group_custom'+str(i)]='yes'
        return f

    def test_only_four_public_products_and_native_options(self):
        status,_,html=self.http('/shop');self.assertEqual(status,200);self.assertEqual(html.count('class="card"'),4)
        self.assertNotIn('검증 예식',html);self.assertNotIn('쌀 10kg',html)
        for row in self.rows:
            status,_,page=self.http('/shop/products/'+row['id']);self.assertEqual(status,200);self.assertIn('product_revision',page);self.assertIn('name="event_time"',page)
        client=http.client.HTTPConnection(*self.server.server_address);client.request('GET','/shop-art/thumbnails-v3.png');response=client.getresponse();self.assertEqual(response.status,200);self.assertTrue(response.read().startswith(b'\x89PNG'));client.close()
        self.assertEqual(self.http('/shop-art/catalog.json')[0],404)
        with agent.db() as c:self.assertEqual(shop_catalog.seed(c),[])

    def test_server_amount_snapshot_options_and_duplicate(self):
        fields=self.catalog_fields();fields['choice0']=list(self.source['groups'][0]['choices'])[1];fields['amount']='1'
        self.assertEqual(self.place_catalog(fields)[0],303)
        self.assertEqual(self.place_catalog(fields)[0],303);self.assertEqual(self.mock_payment.call_count,1)
        with agent.db() as c:
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',('c'*32,)).fetchone();data=json.loads(row['details'])
        self.assertEqual(data['amount'],165000);self.assertEqual(data['product_snapshot']['source_id'],'1454214');self.assertEqual(data['venue'],'샘플 설치 홀');self.assertIn('모바일청첩장 주소 또는 없음',[x[0] for x in data['options']])
        self.assertEqual(self.place_catalog(dict(fields,address='변경 주소'))[0],422)
        self.assertEqual(self.place_catalog(dict(fields,attempt='d'*32,choice0='forged'))[0],422)
        self.assertEqual(self.place_catalog(dict(fields,attempt='e'*32,extra0=''))[0],422)

    def test_editor_preserves_old_offer_and_orders_and_rejects_stale_revision(self):
        fields=self.catalog_fields();self.assertEqual(self.place_catalog(fields)[0],303)
        self.post_admin(**self.editor())
        with agent.db() as c:
            row=c.execute('SELECT * FROM shop_products WHERE id=?',(self.identity,)).fetchone();updated=json.loads(row['details'])
            self.assertEqual(row['revision'],2);self.assertEqual(updated['price'],120000);self.assertEqual(updated['source_id'],'1454214');self.assertEqual(updated['thumbnail'],self.source['thumbnail'])
            original=json.loads(c.execute('SELECT details FROM invitation_orders WHERE id=?',('c'*32,)).fetchone()[0]);self.assertEqual(original['amount'],110000)
        self.assertEqual(self.place_catalog(fields)[0],303);self.assertEqual(self.mock_payment.call_count,1)
        self.assertEqual(self.place_catalog(dict(fields,attempt='f'*32))[0],422)
        self.assertEqual(self.http(self.admin,self.editor())[0],422)
        self.mock_payment.return_value=('5678','https://payapp.kr/test2')
        self.assertEqual(self.place_catalog(dict(fields,attempt='f'*32,product_revision='2',**{'choice'+str(i):next(iter(g['choices'])) for i,g in enumerate(updated['groups'])}))[0],303)
        with agent.db() as c:self.assertEqual(json.loads(c.execute('SELECT details FROM invitation_orders WHERE id=?',('f'*32,)).fetchone()[0])['amount'],120000)

    def test_uncertain_request_never_reissues_and_search_restores(self):
        with patch.object(checkout,'payapp_request',side_effect=TimeoutError) as request:
            fields=self.catalog_fields();self.assertEqual(self.place_catalog(fields)[0],502);self.assertEqual(self.place_catalog(fields)[0],422);self.assertEqual(request.call_count,1)
        status,_,page=self.http(self.admin+'?'+urlencode({'q':'검증 주문자'}));self.assertEqual(status,200);self.assertIn('data-shop-order="'+'c'*32+'"',page)
        self.assertNotIn('data-shop-order="'+'c'*32+'"',self.http(self.admin+'?q=no-result-123')[2])
        backup=Path(self.tmp.name)/'backup';restored=Path(self.tmp.name)/'restored'
        manage_data.transfer(self.root,backup);manage_data.transfer(backup,restored,restore=True)
        self.assertEqual(manage_data.inspect(self.root)['counts'],manage_data.inspect(restored)['counts'])
        with sqlite3.connect(restored/'state.sqlite3') as c:self.assertEqual(c.execute('SELECT state FROM invitation_orders WHERE id=?',('c'*32,)).fetchone()[0],'requesting')
        self.assertEqual(self.place_catalog(dict(fields,attempt='a'*32),)[0],303)

    def test_hide_and_stop_do_not_change_issued_event_links(self):
        edit=self.editor();edit.pop('public');edit.pop('available');self.post_admin(**edit)
        self.assertEqual(self.http('/shop')[2].count('class="card"'),3);self.assertEqual(self.http('/shop/products/'+self.identity)[0],404)
        self.assertEqual(self.place_catalog()[0],422)
        self.assertEqual(self.http('/shop/order/'+self.offer)[0],200)
