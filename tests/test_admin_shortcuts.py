import sys
from pathlib import Path
from urllib.parse import urlencode
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_shop import ShopFixture,SECRET
import test_admin_usability as fixtures
import agent
import shop
import shop_admin_team as team
import shop_admin_shortcuts as shortcuts
from lxml import html


class AdminShortcutsTest(ShopFixture):
    def seed(self):fixtures.AdminUsabilityTest.seed_orders(self);return f'{69:032x}'
    def task(self,oid):return dict(action='save_operator_task',order_id=oid,task_id='a'*32,task_version='0',task_title='확인 업무',task_owner='내부 담당',task_due='2026-12-10T10:00',task_state='open',task_priority='high',task_note='처리 근거 보존')
    def quick(self,oid,version,state):return dict(action='quick_operator_task',order_id=oid,task_id='a'*32,task_version=str(version),task_target=state)

    def test_quick_complete_reopen_preserves_content_and_rejects_stale_requests(self):
        oid=self.seed();self.assertEqual(self.http(self.admin,self.task(oid))[0],303)
        with agent.db() as c:before=tuple(c.execute('SELECT state,details FROM invitation_orders WHERE id=?',(oid,)).fetchone());original=c.execute('SELECT * FROM shop_operator_tasks').fetchone()
        fields=self.quick(oid,1,'done');self.assertEqual(self.http(self.admin,fields)[0],303);self.assertEqual(self.http(self.admin,fields)[0],422)
        self.assertEqual(self.http(self.admin,self.quick(f'{68:032x}',2,'open'))[0],422)
        self.assertEqual(self.http(self.admin,self.quick(oid,2,'open'))[0],303)
        with agent.db() as c:
            row=c.execute('SELECT * FROM shop_operator_tasks').fetchone();self.assertEqual(row['state'],'open');self.assertEqual(row['version'],3)
            for key in ('title','owner','due','priority','note'):self.assertEqual(row[key],original[key])
            self.assertEqual(c.execute('SELECT count(*) FROM shop_operator_task_events').fetchone()[0],3);self.assertEqual(tuple(c.execute('SELECT state,details FROM invitation_orders WHERE id=?',(oid,)).fetchone()),before)

    def test_reopening_respects_pending_limit(self):
        oid=self.seed();self.assertEqual(self.http(self.admin,self.task(oid))[0],303);self.assertEqual(self.http(self.admin,self.quick(oid,1,'done'))[0],303)
        with agent.db() as c:
            for i in range(30):c.execute('INSERT INTO shop_operator_tasks VALUES (?,?,?,?,?,?,?,?,?,?)',(f'{i:032x}',oid,'업무','담당','2026-12-10T10:00','open',1,'','now','normal'))
        self.assertEqual(self.http(self.admin,self.quick(oid,2,'open'))[0],422)
        with agent.db() as c:self.assertEqual(c.execute('SELECT state FROM shop_operator_tasks WHERE id=?',('a'*32,)).fetchone()[0],'done')

    def test_shared_favorite_order_initial_text_and_history(self):
        oid=self.seed();fields=dict(action='toggle_response_favorite',favorite_key='builtin:1',favorite_version='0',favorite_target='1')
        self.assertEqual(self.http(self.admin,fields)[0],303);self.assertEqual(self.http(self.admin,fields)[0],422)
        with agent.db() as c:
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',(oid,)).fetchone();tree=html.fromstring(team.response_tools(c,row));first=tree.xpath('//option')[0];self.assertEqual(first.text,'★ 결제 확인 안내');self.assertEqual(tree.xpath('//textarea')[0].text,first.get('value'))
            self.assertEqual(c.execute('SELECT count(*) FROM shop_response_favorite_events').fetchone()[0],1)
        self.assertEqual(self.http(self.admin,dict(fields,favorite_version='1',favorite_target='0'))[0],303)
        self.assertEqual(self.http(self.admin,dict(fields,favorite_key='missing'))[0],422)
        with agent.db() as c:self.assertEqual(shortcuts.favorite_keys(c),set());self.assertEqual(c.execute('SELECT count(*) FROM shop_response_favorite_events').fetchone()[0],2)

    def test_note_preview_is_bounded_escaped_and_private(self):
        note='<script>내부</script>'+'긴 메모'*80
        f=dict(action='save_product_note',product_id=self.product_id,note_version='0',note_author='내부 작성자',product_note=note)
        self.assertEqual(self.http(self.admin,f)[0],303)
        status,_,body=self.http(self.admin);self.assertEqual(status,200);tree=html.fromstring(body);preview=tree.xpath('//*[contains(@class,"product-note-preview")]')[0].text_content();self.assertIn(note[:120],preview);self.assertNotIn(note,preview);self.assertIn('…',preview);self.assertNotIn('<script>내부</script>',body)
        self.assertNotIn('긴 메모',self.http('/shop')[2])
        self.assertEqual(self.http(self.admin,dict(f,note_version='1',product_note=''))[0],303)
        with agent.db() as c:self.assertIn('운영 메모 없음',shortcuts.note_preview(c.execute('SELECT * FROM shop_product_notes ORDER BY version DESC LIMIT 1').fetchone()))

    def test_shift_download_uses_same_filters_and_admin_authority(self):
        oid=self.seed();self.assertEqual(self.http(self.admin,self.task(oid))[0],303)
        fields={'handover_owner':'내부 담당','handover_until':'2026-12-10'}
        status,headers,body=self.http(self.admin+'/shift-summary.txt?'+urlencode(fields));self.assertEqual(status,200);self.assertIn('text/plain',headers['Content-Type']);self.assertIn('attachment',headers['Content-Disposition']);self.assertIn('no-store',headers['Cache-Control']);self.assertIn('처리 근거 보존',body);self.assertNotIn('01012345678',body)
        with agent.db() as c:self.assertEqual(body.splitlines()[2:],team.shift_report(c,fields)['text'].splitlines()[2:]);self.assertEqual(c.execute('SELECT count(*) FROM shop_operator_task_events').fetchone()[0],1)
        self.assertEqual(self.http(self.admin+'/shift-summary.txt?handover_until=bad')[0],422)
        expiry='1000000000';expired='/shop/admin/'+expiry+'/'+shop.admin_signature(expiry,SECRET)
        self.assertEqual(self.http(expired+'/shift-summary.txt')[0],403)
