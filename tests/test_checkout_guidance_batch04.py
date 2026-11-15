"""The new client guidance never replaces authoritative server validation."""
import json
from test_shop import ShopFixture
import test_shop_catalog as catalog_tests
import agent
import shop_catalog


class CheckoutGuidanceBatch04Test(ShopFixture):
    def setUp(self):
        super().setUp()
        with agent.db() as c:
            ids=shop_catalog.seed(c);row=c.execute('SELECT * FROM shop_products WHERE id=?',(ids[0],)).fetchone()
            self.identity=row['id'];self.source=json.loads(row['details'])

    def fields_catalog(self):return catalog_tests.CatalogTest.catalog_fields(self)

    def test_bypassed_client_past_event_and_blank_installation_never_request_payment(self):
        for changes in ({'event_time':'2020-01-01T12:00'},{'venue':'   '},{'address':'   '}):
            status,_,_=self.http('/shop/products/'+self.identity,dict(self.fields_catalog(),**changes))
            self.assertEqual(status,422)
        self.mock_payment.assert_not_called()
        with agent.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM invitation_orders').fetchone()[0],0)

    def test_server_trims_outer_spaces_preserves_inner_spaces_and_ignores_client_amounts(self):
        fields=self.fields_catalog();fields.update(sender='  보내는 분  ',buyer='  주문자  ',venue='  테스트 홀  ',address='  서울  강남구 1  ',amount='1',base_price='1',event_days='9999')
        fields['choice0']=list(self.source['groups'][0]['choices'])[1]
        self.assertEqual(self.http('/shop/products/'+self.identity,fields)[0],303)
        self.mock_payment.assert_called_once()
        with agent.db() as c:
            data=json.loads(c.execute('SELECT details FROM invitation_orders').fetchone()[0])
            self.assertEqual(data['amount'],165000)
            self.assertEqual(data['sender'],'보내는 분');self.assertEqual(data['buyer'],'주문자')
            self.assertEqual(data['venue'],'테스트 홀');self.assertEqual(data['address'],'서울  강남구 1')
            self.assertTrue(data['event_time'].endswith('+09:00'))
