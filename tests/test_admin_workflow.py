import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_shop import ShopFixture,PRODUCT
import test_admin_usability as admin_tests
import agent
import shop_admin_workflow as workflow


class AdminWorkflowTest(ShopFixture):
    def test_saved_search_persistence_validation_and_deletion(self):
        fields=dict(action='save_search',search_name='배송 대기',saved_filters=urlencode({'q':'검색 대상','state':'paid','page':'3'}))
        self.assertEqual(self.http(self.admin,fields)[0],303)
        with agent.db() as c:
            row=c.execute('SELECT * FROM shop_saved_searches').fetchone();self.assertNotIn('page',json.loads(row['filters']))
        status,_,body=self.http(self.admin);self.assertEqual(status,200);self.assertIn('배송 대기',body);self.assertIn('state=paid',body)
        self.assertEqual(self.http(self.admin,fields)[0],422)
        self.assertEqual(self.http(self.admin,dict(fields,search_name='위험 조건',saved_filters='next=https://evil.test'))[0],422)
        self.assertEqual(self.http(self.admin,dict(action='delete_search',search_id=row['id']))[0],303)
        with agent.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM shop_saved_searches').fetchone()[0],0)

    def test_handover_is_append_only_and_cannot_advance_order(self):
        admin_tests.AdminUsabilityTest.seed_orders(self);oid=f'{69:032x}'
        fields=dict(action='save_operator_handover',order_id=oid,handover_version='0',handover_from='오전 담당',handover_to='오후 담당',handover_note='<script>hello</script>',check_payment='yes',check_exceptions='yes')
        with agent.db() as c:
            before=tuple(c.execute('SELECT state,details FROM invitation_orders WHERE id=?',(oid,)).fetchone());fields['handover_context']=workflow.handover_context(c,oid)
        self.assertEqual(self.http(self.admin,fields)[0],303)
        self.assertEqual(self.http(self.admin,fields)[0],422)
        self.assertEqual(self.http(self.admin,dict(fields,handover_version='1',handover_note='미처리 확인'))[0],303)
        with agent.db() as c:
            rows=c.execute('SELECT * FROM shop_operator_handover WHERE order_id=? ORDER BY version',(oid,)).fetchall();self.assertEqual(len(rows),2)
            import manage_data
            self.assertEqual(manage_data.inspect(self.root)['counts']['shop_operator_handover'],2)
            self.assertIn('<script>',json.loads(rows[0]['details'])['handover_note']);self.assertEqual(tuple(c.execute('SELECT state,details FROM invitation_orders WHERE id=?',(oid,)).fetchone()),before)
        status,_,body=self.http(self.admin+'?order='+oid);self.assertEqual(status,200);self.assertIn('&lt;script&gt;hello&lt;/script&gt;',body);self.assertNotIn('<script>hello</script>',body)

    def test_handover_requires_recheck_after_order_changes(self):
        admin_tests.AdminUsabilityTest.seed_orders(self);oid=f'{69:032x}'
        with agent.db() as c:context=workflow.handover_context(c,oid)
        fields=dict(action='save_operator_handover',order_id=oid,handover_version='0',handover_context=context,handover_from='인계자',handover_to='인수자',check_payment='yes')
        self.assertEqual(self.http(self.admin,fields)[0],303)
        with agent.db() as c:c.execute("UPDATE invitation_orders SET state='cancelled' WHERE id=?",(oid,))
        self.assertEqual(self.http(self.admin,dict(fields,handover_version='1'))[0],422)
        status,_,body=self.http(self.admin+'?order='+oid);self.assertEqual(status,200);self.assertIn('마지막 인수인계 이후',body)
        from lxml import html
        tree=html.fromstring(body);self.assertFalse(tree.xpath('//input[@name="check_payment"]/@checked'))

    def test_product_change_audit_preserves_issued_snapshot(self):
        with agent.db() as c:
            before=c.execute('SELECT details FROM shop_offers WHERE id=?',(self.offer,)).fetchone()[0]
        fields=dict(PRODUCT,action='save_product',product_id=self.product_id,revision='1',price='120000',title='수정 <상품>')
        self.assertEqual(self.http(self.admin,fields)[0],303)
        self.assertEqual(self.http(self.admin,fields)[0],422)
        with agent.db() as c:
            change=c.execute('SELECT * FROM shop_product_changes').fetchone();self.assertEqual(change['revision'],2);self.assertEqual(json.loads(change['after_details'])['price'],120000)
            self.assertEqual(c.execute('SELECT details FROM shop_offers WHERE id=?',(self.offer,)).fetchone()[0],before)
            html=workflow.product_history(c,self.product_id);self.assertIn('변경 전',html);self.assertIn('&lt;상품&gt;',html)
            self.assertEqual(c.execute('SELECT count(*) FROM shop_product_changes').fetchone()[0],1)

    def test_backup_stale_failure_running_and_external_modes(self):
        with agent.db() as c,patch.dict(os.environ,{'SHOP_BACKUP_ENABLED':'1','SHOP_BACKUP_INTERVAL_SECONDS':'600'}):
            status=workflow.backup_status(c,lambda:10000);self.assertTrue(any('없음' in x for x in status['warnings']))
            c.execute("INSERT INTO shop_backup_runs VALUES (1,9000,9100,'verified','/private/path','')")
            self.assertFalse(any('간격 초과' in x for x in workflow.backup_status(c,lambda:10000)['warnings']))
            self.assertTrue(any('간격 초과' in x for x in workflow.backup_status(c,lambda:10001)['warnings']))
            c.execute("INSERT INTO shop_backup_runs VALUES (2,9900,9901,'failed','/private/path','')")
            self.assertTrue(any('실패' in x for x in workflow.backup_status(c,lambda:10000)['warnings']))
            c.execute("INSERT INTO shop_external_backups VALUES ('private-folder-reference','google_drive','hash','1970-01-01T02:46:40+00:00','manual_verified')")
            self.assertTrue(any('자동 외부 백업 검증 기록 없음' in x for x in workflow.backup_status(c,lambda:10000)['warnings']))
            c.execute("INSERT INTO shop_external_backups VALUES ('private-reference-auto','google_drive','hash','1970-01-01T02:46:40+00:00','automatic_verified')")
            self.assertFalse(any('외부 백업' in x for x in workflow.backup_status(c,lambda:10000)['warnings']))
            c.execute("INSERT INTO shop_backup_runs VALUES (3,9500,NULL,'running','/private/path','')")
            self.assertTrue(any('장시간' in x for x in workflow.backup_status(c,lambda:10000)['warnings']))
            body=workflow.backups(c);self.assertNotIn('/private/path',body);self.assertNotIn('private-folder-reference',body)

    def test_timeline_groups_and_escapes_existing_events(self):
        admin_tests.AdminUsabilityTest.seed_orders(self);oid=f'{69:032x}'
        with agent.db() as c:
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',(oid,)).fetchone()
            c.execute('INSERT INTO payment_checks VALUES (1,?,?,?,?,?)',(oid,'1234',93500,'<img src=x>','2026-11-14T12:00:00+09:00'))
            c.execute("INSERT INTO shop_stage_history VALUES (?,1,'confirmed','payment','2026-11-14T11:00:00+09:00')",(oid,))
            body=workflow.timeline(c,row);self.assertIn('결제·취소 · 1건',body);self.assertIn('배송·배정 · 1건',body);self.assertIn('&lt;img src=x&gt;',body);self.assertNotIn('<img src=x>',body)
