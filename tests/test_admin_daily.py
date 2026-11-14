import datetime as dt
import json
import sys
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_shop import ShopFixture,PRODUCT,SECRET
import test_admin_usability as fixtures
import agent
import shop
import shop_admin_daily as daily
import shop_admin_queries as queries
import shop_admin_training as training


class AdminDailyTest(ShopFixture):
    def seed(self):fixtures.AdminUsabilityTest.seed_orders(self);return f'{69:032x}'
    def task(self,oid,**changes):return dict(action='save_operator_task',order_id=oid,task_id='a'*32,task_version='0',task_title='설치 장소 확인',task_owner='검증 담당',task_due='2026-12-10T10:00',task_state='open',task_note='내부 전용 메모',**changes)

    def test_tasks_are_versioned_filtered_and_preserve_order(self):
        oid=self.seed();fields=self.task(oid)
        with agent.db() as c:before=tuple(c.execute('SELECT state,details FROM invitation_orders WHERE id=?',(oid,)).fetchone())
        self.assertEqual(self.http(self.admin,fields)[0],303);self.assertEqual(self.http(self.admin,fields)[0],422)
        with agent.db() as c:
            rows,meta=queries.select(c,{'task':'open','task_owner':'검증'});self.assertEqual(meta['total'],1);self.assertEqual(rows[0]['id'],oid)
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',(oid,)).fetchone();self.assertEqual(tuple(row[k] for k in ('state','details')),before)
            self.assertEqual(c.execute('SELECT count(*) FROM shop_operator_task_events').fetchone()[0],1)
        self.assertEqual(self.http(self.admin,dict(fields,task_version='1',task_state='done',task_note='대조 완료'))[0],303)
        with agent.db() as c:
            self.assertEqual(queries.select(c,{'task':'open'})[1]['total'],0)
            self.assertEqual(c.execute('SELECT count(*) FROM shop_operator_task_events').fetchone()[0],2)
        self.assertEqual(self.http(self.admin,dict(fields,task_version='2',order_id=f'{68:032x}'))[0],422)
        self.assertEqual(self.http(self.admin,dict(fields,task_id='b'*32,task_due='2026-02-30T10:00'))[0],422)

    def test_task_deadline_and_event_shortcuts_use_seoul_dates(self):
        oid=self.seed();self.assertEqual(self.http(self.admin,self.task(oid))[0],303)
        with agent.db() as c,patch.object(queries.dt,'datetime',wraps=dt.datetime) as clock:
            clock.now.return_value=dt.datetime(2026,12,10,10,1,tzinfo=daily.SEOUL)
            self.assertEqual(queries.select(c,{'task':'overdue'})[1]['total'],1)
            clock.now.return_value=dt.datetime(2026,12,10,10,0,tzinfo=daily.SEOUL)
            self.assertEqual(queries.select(c,{'task':'overdue'})[1]['total'],0)
        # Sunday UTC is already Monday in Korea; week starts on Korean Monday.
        with agent.db() as c:
            body=daily.quick_dates(c,self.admin,lambda:dt.datetime(2026,12,6,16,tzinfo=dt.timezone.utc));self.assertIn('2026-12-07 ~ 2026-12-13',body);self.assertIn('오늘 행사',body)
            board=daily.task_board(c,self.admin,lambda:dt.datetime(2026,12,10,10,1,tzinfo=daily.SEOUL));self.assertIn('기한 초과 1건',board)

    def test_customer_summary_excludes_internal_data_and_uses_approved_wording(self):
        oid=self.seed()
        with agent.db() as c:
            c.execute('INSERT INTO shop_amendments VALUES (?,?,?,?,?,?,?,?)',('b'*32,oid,0,json.dumps({'sender':'정정 보내는 분','message':'정정 문구'}),'approved','내부 요청자','비공개 메모','2026-12-01'))
            c.execute('INSERT INTO shop_amendment_events VALUES (1,?,?,?,?,?,?,?)',(oid,'b'*32,'approved','내부 담당','내부 검수자','비공개 메모','2026-12-01'))
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',(oid,)).fetchone();summary=daily.customer_summary(c,row)
            self.assertIn('정정 문구',summary);self.assertIn('정정 보내는 분',summary);self.assertNotIn('비공개',summary);self.assertNotIn('내부',summary);self.assertNotIn('01012345678',summary);self.assertNotIn('/shop/admin/',summary)
            c.execute("UPDATE invitation_orders SET state='cancelled' WHERE id=?",(oid,));c.execute("INSERT INTO payment_events VALUES (?,?,'partial','p',10000,'','2026-12-01','paid')",(oid,'partial:p'));c.execute("INSERT INTO payment_events VALUES (?,?,'cancel','x',93500,'','2026-12-02','partial_cancel')",(oid,'cancel:x'))
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',(oid,)).fetchone();summary=daily.customer_summary(c,row);self.assertIn('93,500원 (통보 기록 기준)',summary);self.assertNotIn('103,500',summary)

    def test_invalid_options_rejected_without_product_changes(self):
        for fields in [dict(PRODUCT,options='쌀 10kg | 0\n쌀 10kg | 1000'),dict(PRODUCT,price='100000000',options='쌀 | 1')]:
            self.assertEqual(self.http(self.admin,dict(fields,action='save_product',product_id=self.product_id,revision='1'))[0],422)
        with agent.db() as c:self.assertEqual(c.execute('SELECT revision FROM shop_products WHERE id=?',(self.product_id,)).fetchone()[0],1)
        status,_,body=self.http(self.admin);self.assertEqual(status,200);self.assertIn('product-guidance',body)

    def test_training_requires_admin_and_has_no_persistent_side_effects(self):
        with agent.db() as c:before=c.execute('SELECT count(*) FROM invitation_orders').fetchone()[0]
        status,_,body=self.http(self.admin+'/training');self.assertEqual(status,200);self.assertIn('교육용 문구 예시',body);self.assertEqual(len(training.SCENARIOS),5);self.assertNotIn('<form',body);self.assertNotIn('fetch(',body)
        expiry='1000000000';expired='/shop/admin/'+expiry+'/'+shop.admin_signature(expiry,SECRET)
        self.assertEqual(self.http(expired+'/training')[0],403)
        with agent.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM invitation_orders').fetchone()[0],before)
