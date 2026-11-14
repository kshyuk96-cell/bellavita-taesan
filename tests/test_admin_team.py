import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_shop import ShopFixture
import test_admin_usability as fixtures
import agent
import shop_admin_daily as daily
import shop_admin_team as team
import shop_admin_queries as queries


class AdminTeamTest(ShopFixture):
    def seed(self):fixtures.AdminUsabilityTest.seed_orders(self);return f'{69:032x}'
    def task(self,oid,key,priority,due):return dict(action='save_operator_task',order_id=oid,task_id=key*32,task_version='0',task_title='우선 업무 '+priority,task_owner='교대 담당',task_due=due,task_state='open',task_priority=priority,task_note='직원 전용 근거')

    def test_existing_task_schema_migrates_without_data_loss(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        c.execute('CREATE TABLE shop_operator_tasks(id TEXT PRIMARY KEY,order_id TEXT,title TEXT,owner TEXT,due TEXT,state TEXT,version INTEGER,note TEXT,updated TEXT)')
        c.execute("INSERT INTO shop_operator_tasks VALUES ('old','order','기존 업무','담당','2026-12-10T10:00','open',1,'메모','2026-11-14')")
        daily.setup(c);daily.setup(c);row=c.execute('SELECT * FROM shop_operator_tasks').fetchone();self.assertEqual(row['priority'],'normal');self.assertEqual(row['title'],'기존 업무');self.assertEqual(row['version'],1);c.close()

    def test_priority_sort_filter_and_optimistic_updates(self):
        oid=self.seed();low=self.task(oid,'a','low','2026-12-01T09:00');high=self.task(oid,'b','high','2026-12-20T09:00')
        self.assertEqual(self.http(self.admin,low)[0],303);self.assertEqual(self.http(self.admin,high)[0],303)
        with agent.db() as c:
            board=daily.task_board(c,self.admin);self.assertLess(board.index('우선 업무 high'),board.index('우선 업무 low'))
            self.assertEqual(queries.select(c,{'task_priority':'high'})[1]['total'],1)
            before=c.execute('SELECT count(*) FROM shop_operator_task_events').fetchone()[0]
        self.assertEqual(self.http(self.admin,dict(high,task_version='1',task_priority='invalid'))[0],422)
        with agent.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM shop_operator_task_events').fetchone()[0],before)
        self.assertEqual(self.http(self.admin,dict(high,task_version='1',task_priority='normal'))[0],303);self.assertEqual(self.http(self.admin,high)[0],422)
        with agent.db() as c:self.assertEqual(queries.select(c,{'task_priority':'high'})[1]['total'],0)

    def test_template_validation_state_substitution_and_archive(self):
        oid=self.seed();f=dict(action='save_response_template',template_id='d'*32,template_version='0',template_name='상태 응대',template_body='주문 {주문번호} · {결제상태} · {문구}',template_active='yes')
        self.assertEqual(self.http(self.admin,f)[0],303);self.assertEqual(self.http(self.admin,f)[0],422)
        for bad in ('{내부메모}','{요약.__class__}','{요약!r}','{요약:>10}','{요약'):
            self.assertEqual(self.http(self.admin,dict(f,template_version='1',template_body=bad))[0],422)
        with agent.db() as c:
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',(oid,)).fetchone();body=team.response_tools(c,row);self.assertIn('결제 완료',body);self.assertNotIn('직원 전용 근거',body)
            self.assertEqual(c.execute('SELECT count(*) FROM shop_response_template_events').fetchone()[0],1)
        self.assertEqual(self.http(self.admin,dict(f,template_version='1',template_active=''))[0],303)
        with agent.db() as c:self.assertNotIn('상태 응대</option>',team.response_tools(c,row));self.assertEqual(c.execute('SELECT count(*) FROM shop_response_template_events').fetchone()[0],2)

    def test_product_notes_are_private_versioned_and_do_not_edit_catalog(self):
        f=dict(action='save_product_note',product_id=self.product_id,note_version='0',note_author='운영자',product_note='내부 전용 <script>검수</script>')
        with agent.db() as c:before=tuple(c.execute('SELECT revision,details FROM shop_products WHERE id=?',(self.product_id,)).fetchone())
        self.assertEqual(self.http(self.admin,f)[0],303);self.assertEqual(self.http(self.admin,f)[0],422)
        with agent.db() as c:
            self.assertEqual(tuple(c.execute('SELECT revision,details FROM shop_products WHERE id=?',(self.product_id,)).fetchone()),before)
            body=team.product_note(c,self.product_id,self.admin);self.assertIn('&lt;script&gt;',body);self.assertNotIn('<script>',body)
        self.assertEqual(self.http(self.admin,dict(f,note_version='1',product_note=''))[0],303)
        with agent.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM shop_product_notes').fetchone()[0],2)
        self.assertNotIn('내부 전용',self.http('/shop')[2])

    def test_shift_summary_filters_pending_records_and_bounds_output(self):
        oid=self.seed();self.assertEqual(self.http(self.admin,self.task(oid,'a','high','2026-12-10T09:00'))[0],303)
        with agent.db() as c:
            body=team.shift_summary(c,self.admin,{'handover_owner':'교대','handover_until':'2026-12-10'});self.assertIn('미처리 1건 중 1건',body);self.assertIn('직원 전용 근거',body);self.assertNotIn('01012345678',body)
            self.assertIn('조건에 맞는 미처리 업무 없음',team.shift_summary(c,self.admin,{'handover_until':'2026-12-09'}))
            for i in range(101):c.execute('INSERT INTO shop_operator_tasks VALUES (?,?,?,?,?,?,?,?,?,?)',(f'{i:032x}',f'{i%4:032x}','누적 업무','다른 담당','2026-12-10T10:00','open',1,'','now','normal'))
            body=team.shift_summary(c,self.admin,{});self.assertIn('102건 중 100건',body);self.assertIn('100건을 초과',body)
        self.assertEqual(self.http(self.admin+'?handover_until=2026-02-30')[0],422)
        self.assertEqual(self.http(self.admin,dict(self.task(oid,'a','high','2026-12-10T09:00'),task_version='1',task_state='done'))[0],303)
        with agent.db() as c:self.assertIn('조건에 맞는 미처리 업무 없음',team.shift_summary(c,self.admin,{'handover_owner':'교대'}))

    def test_order_focus_and_reference_keep_approved_and_original_details(self):
        oid=self.seed();status,_,body=self.http(self.admin+'?order='+oid);self.assertEqual(status,200)
        from lxml import html
        tree=html.fromstring(body);reference=tree.xpath('//*[contains(@class,"order-reference")]')[0];self.assertIsNone(reference.get('open'));self.assertIn('원결제금액',reference.text_content());self.assertIn('작업 핵심 정보',tree.xpath('//*[contains(@class,"order-focus")]')[0].text_content())
