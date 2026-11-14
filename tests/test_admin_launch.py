import datetime as dt
import json
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_shop import ShopFixture,SECRET
import test_admin_usability as fixtures
import agent
import shop
import shop_admin_daily as daily
import shop_admin_launch as launch
import shop_admin_queries as queries
import shop_admin_feature_search as features


class AdminLaunchTest(ShopFixture):
    def seed(self):fixtures.AdminUsabilityTest.seed_orders(self);return f'{69:032x}'
    def task(self,oid):return dict(action='save_operator_task',order_id=oid,task_id='a'*32,task_version='0',task_title='교대 확인',task_owner='검증 담당',task_due='2026-12-10T10:00',task_state='open',task_priority='high',task_note='직원 전용 보관 근거')

    def test_done_search_combines_state_owner_priority_on_same_task(self):
        oid=self.seed();f=self.task(oid);self.assertEqual(self.http(self.admin,f)[0],303)
        self.assertEqual(self.http(self.admin,dict(f,task_id='b'*32,task_owner='완료 담당',task_priority='normal'))[0],303)
        self.assertEqual(self.http(self.admin,dict(f,task_id='b'*32,task_version='1',task_owner='완료 담당',task_priority='normal',task_state='done'))[0],303)
        with agent.db() as c:
            self.assertEqual(queries.select(c,{'task':'done','task_owner':'완료','task_priority':'normal'})[1]['total'],1)
            self.assertEqual(queries.select(c,{'task':'done','task_priority':'high'})[1]['total'],0)
            self.assertEqual(queries.select(c,{'task':'open','task_owner':'완료'})[1]['total'],0)
        self.assertEqual(self.http(self.admin+'?task=unknown')[0],422)

    def test_due_soon_window_is_inclusive_and_excludes_done_and_overdue(self):
        self.seed();current=dt.datetime(2026,12,10,10,0,tzinfo=daily.SEOUL)
        with agent.db() as c:
            for i,(offset,state) in enumerate([(-1,'open'),(0,'open'),(1440,'open'),(1441,'open'),(60,'done')]):
                c.execute('INSERT INTO shop_operator_tasks VALUES (?,?,?,?,?,?,?,?,?,?)',(f'{i:032x}',f'{i:032x}','기한 업무','담당',(current+dt.timedelta(minutes=offset)).strftime('%Y-%m-%dT%H:%M'),state,1,'','now','normal'))
            with patch.object(queries.dt,'datetime',wraps=dt.datetime) as clock:
                clock.now.return_value=current;rows,meta=queries.select(c,{'task':'soon'});self.assertEqual(meta['total'],2);self.assertEqual({r['id'] for r in rows},{f'{1:032x}',f'{2:032x}'})
            self.assertIn('24시간 이내 2건',daily.task_board(c,self.admin,lambda:current))

    def test_feature_favorites_shared_versioned_and_limited(self):
        self.assertEqual(len(features.FEATURES),len(launch.FEATURE_IDS))
        f=dict(action='toggle_feature_favorite',feature_id='launch-check',feature_version='0',feature_target='1')
        self.assertEqual(self.http(self.admin,f)[0],303);self.assertEqual(self.http(self.admin,f)[0],422)
        with agent.db() as c:self.assertIn('data-admin-feature="launch-check"',launch.favorite_nav(c))
        self.assertEqual(self.http(self.admin,dict(f,feature_id='evil-selector'))[0],422)
        for key in launch.FEATURE_IDS[:7]:self.assertEqual(self.http(self.admin,dict(f,feature_id=key))[0],303)
        self.assertEqual(self.http(self.admin,dict(f,feature_id='offer-links'))[0],422)
        self.assertEqual(self.http(self.admin,dict(f,feature_version='1',feature_target='0'))[0],303)
        with agent.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM shop_feature_favorites WHERE favorite=1').fetchone()[0],7);self.assertEqual(c.execute('SELECT count(*) FROM shop_feature_favorite_events').fetchone()[0],9)

    def test_snapshot_is_immutable_idempotent_and_download_authorized(self):
        oid=self.seed();task=self.task(oid);self.assertEqual(self.http(self.admin,task)[0],303)
        f=dict(action='save_shift_snapshot',snapshot_id='d'*32,snapshot_title='오후 교대',snapshot_author='내부 작성자',snapshot_owner='검증 담당',snapshot_until='2026-12-10')
        self.assertEqual(self.http(self.admin,f)[0],303)
        self.assertEqual(self.http(self.admin,dict(task,task_version='1',task_state='done',task_note='변경된 메모'))[0],303)
        self.assertEqual(self.http(self.admin,f)[0],303);self.assertEqual(self.http(self.admin,dict(f,snapshot_author='다른 작성자'))[0],422)
        status,headers,body=self.http(self.admin+'/shift-history/'+'d'*32+'.txt');self.assertEqual(status,200);self.assertIn('직원 전용 보관 근거',body);self.assertNotIn('변경된 메모',body);self.assertIn('미처리 1건 중 1건',body);self.assertIn('no-store',headers['Cache-Control'])
        with agent.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM shop_shift_snapshots').fetchone()[0],1)
        expiry='1000000000';expired='/shop/admin/'+expiry+'/'+shop.admin_signature(expiry,SECRET)
        self.assertEqual(self.http(expired+'/shift-history/'+'d'*32+'.txt')[0],403);self.assertEqual(self.http(self.admin+'/shift-history/'+'e'*32+'.txt')[0],404)

    def test_snapshot_capacity_preserves_existing_records(self):
        self.seed()
        with agent.db() as c:
            for i in range(200):c.execute('INSERT INTO shop_shift_snapshots VALUES (?,?,?,?,?,?,?)',(f'{i:032x}','기존 기록','담당','','','보관 내용','now'))
        f=dict(action='save_shift_snapshot',snapshot_id='f'*32,snapshot_title='새 교대',snapshot_author='담당')
        self.assertEqual(self.http(self.admin,f)[0],422)
        with agent.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM shop_shift_snapshots').fetchone()[0],200)

    def test_launch_checks_are_notes_and_never_activate_providers(self):
        oid=self.seed()
        with agent.db() as c:before=tuple(c.execute('SELECT state,details FROM invitation_orders WHERE id=?',(oid,)).fetchone());initial=launch.launch_report(c,self.root)
        self.assertFalse(initial['merchant_settings']);self.assertEqual(initial['actual_provider_validation'],'not_verified_by_this_screen');self.assertEqual(initial['external_requests'],0)
        f=dict(action='save_launch_check',launch_version='0',launch_author='확인 담당',launch_note='<script>확인 근거</script>',**{'launch_'+k:'yes' for k in launch.CHECKS})
        self.assertEqual(self.http(self.admin,f)[0],303);self.assertEqual(self.http(self.admin,f)[0],422)
        with agent.db() as c:
            self.assertEqual(tuple(c.execute('SELECT state,details FROM invitation_orders WHERE id=?',(oid,)).fetchone()),before);report=launch.launch_report(c,self.root);self.assertFalse(report['merchant_settings']);self.assertEqual(report['external_requests'],0);self.assertEqual(c.execute('SELECT count(*) FROM shop_launch_checks').fetchone()[0],1)
            self.assertIn('&lt;script&gt;',launch.launch_panel(c,self.admin,self.root));self.assertEqual(c.execute('SELECT count(*) FROM shop_cash_receipts').fetchone()[0],0)
