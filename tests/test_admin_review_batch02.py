"""All-record search, bounded private export and UI-only restoration guards."""
import csv
import datetime as dt
import io
import json
from unittest.mock import MagicMock
from urllib.parse import urlencode,urlsplit,parse_qs
from lxml import html
from test_shop import ShopFixture
import test_admin_usability as baseline
import agent
import shop_admin_queries as queries
import shop_admin_review as review


class AdminReviewBatch02Test(ShopFixture):
    def seed(self):baseline.AdminUsabilityTest.seed_orders(self)

    def test_highlight_chips_and_empty_search_are_safe(self):
        self.seed()
        self.assertEqual(review.highlight('<script>x</script>','<script>'),'<mark class="order-match">&lt;script&gt;</mark>x&lt;/script&gt;')
        self.assertIn('<mark',review.highlight('보 내는-분','보내는분'))
        url=self.admin+'?'+urlencode({'q':'찾는 주문자','state':'paid','sort':'event'})
        status,_,body=self.http(url);self.assertEqual(status,200)
        tree=html.fromstring(body);self.assertTrue(tree.xpath('//mark[@class="order-match"]'))
        message_page=self.http(self.admin+'?'+urlencode({'q':'행복한'}))[2]
        self.assertTrue(html.fromstring(message_page).xpath('//div[@class="order-focus"]//mark'))
        clear=tree.xpath('//nav[@aria-label="적용 중인 검색 조건"]/a[@aria-label="결제 상태 해제"]/@href')[0]
        f=parse_qs(urlsplit(clear).query);self.assertNotIn('state',f);self.assertEqual(f['q'],['찾는 주문자']);self.assertEqual(f['sort'],['event'])
        self.assertIn('행사일 조건 없이 다시 찾기',self.http(self.admin+'?'+urlencode({'q':'찾을수없는주문'}))[2])
        self.assertEqual(self.http(self.admin+'?sort=DROP')[0],422)

    def test_sorting_preserves_default_and_orders_upcoming_first(self):
        self.seed();oid=f'{69:032x}';past=f'{68:032x}'
        with agent.db() as c:
            data=json.loads(c.execute('SELECT details FROM invitation_orders WHERE id=?',(oid,)).fetchone()[0]);data['event_time']=(dt.datetime.now(queries.ZoneInfo('Asia/Seoul'))+dt.timedelta(hours=2)).isoformat()
            c.execute('UPDATE invitation_orders SET details=?,created=? WHERE id=?',(json.dumps(data),'2020-01-01',oid))
            data['event_time']='2020-01-01T12:00:00+09:00';c.execute("UPDATE invitation_orders SET details=?,state='requesting' WHERE id=?",(json.dumps(data),past))
            self.assertEqual(queries.select(c,{})[0][0]['id'],past)
            self.assertNotEqual(queries.select(c,{'sort':'newest'})[0][0]['id'],oid)
            self.assertEqual(queries.select(c,{'sort':'event'})[0][0]['id'],oid)
            self.assertNotIn(past,[r['id'] for r in queries.select(c,{'sort':'event'})[0]])

    def test_filtered_csv_covers_all_pages_masks_contacts_and_escapes_formulas(self):
        self.seed();oid=f'{69:032x}'
        with agent.db() as c:
            data=json.loads(c.execute('SELECT details FROM invitation_orders WHERE id=?',(oid,)).fetchone()[0]);data['buyer']='=HYPERLINK("bad")'
            c.execute('UPDATE invitation_orders SET details=? WHERE id=?',(json.dumps(data),oid))
        status,headers,body=self.http(self.admin+'/orders.csv?page=3&sort=event')
        self.assertEqual(status,200);self.assertIn('no-store',headers['Cache-Control'])
        records=list(csv.reader(io.StringIO(body.lstrip('\ufeff'))));self.assertEqual(len(records),71)
        self.assertTrue(all(row[5]=='010-****-5678' for row in records[1:]))
        self.assertEqual(next(r for r in records[1:] if r[0]==oid)[4],'\'=HYPERLINK("bad")')
        paid=self.http(self.admin+'/orders.csv?state=paid')[2];self.assertEqual(len(list(csv.reader(io.StringIO(paid.lstrip('\ufeff'))))),36)
        baseline.AdminUsabilityTest.seed_attention(self)
        alerts=self.http(self.admin+'/orders.csv?attention=alerts')[2]
        self.assertEqual(len(list(csv.reader(io.StringIO(alerts.lstrip('\ufeff'))))),2)
        bad=self.admin[:-1]+('0' if self.admin[-1]!='0' else '1')
        self.assertEqual(self.http(bad+'/orders.csv')[0],403)
        self.assertEqual(self.http(self.admin+'/orders.csv?sort=invalid')[0],422)
        c=MagicMock();c.execute.return_value.fetchall.return_value=[None]*10001
        with self.assertRaises(ValueError):review.export_csv(c,{})
        for value in ('=1+1',' +cmd','-1','@SUM(A1)','\t=1'):
            self.assertTrue(review.csv_cell(value).startswith("'"))

    def test_detail_return_validates_before_mutation_and_preserves_order_state(self):
        self.seed();oid=f'{69:032x}'
        fields=dict(action='save_operator_task',order_id=oid,task_id='a'*32,task_version='0',task_title='상세 복귀 확인',task_owner='담당',task_due='2026-12-10T10:00',task_state='open',task_priority='normal',task_note='독립 검증',_return_query='sort=event',_return_details='0.1')
        status,headers,_=self.http(self.admin,fields);self.assertEqual(status,303)
        self.assertEqual(parse_qs(urlsplit(headers['Location']).query)['expanded'],['0.1'])
        with agent.db() as c:self.assertEqual(c.execute('SELECT state FROM invitation_orders WHERE id=?',(oid,)).fetchone()[0],'paid')
        for bad in ('0<script>','-1','999','0.'+'1.'*15+'1'):
            self.assertEqual(self.http(self.admin,dict(fields,_return_details=bad))[0],422)
        with agent.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM shop_operator_tasks').fetchone()[0],1)
