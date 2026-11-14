import json,unittest
from unittest.mock import Mock,patch
import test_order_lifecycle
import order_operations as ops,order_origin as origin,order_assistance as assist,customer_sources
from customer_memory import CustomerMemory
from customer_orders import prepare

class OriginRecoveryTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    row=test_order_lifecycle.LifecycleTests.row
    def legacy(self):
        with self.db() as c:
            c.execute("DELETE FROM order_origin_approvals WHERE order_id='parent'")
            c.execute("UPDATE order_operations SET state='pending',order_number=NULL,raw=? WHERE id='parent'",('주문 접수\n설치장소: 모의홀\n보내는분: 모의회사',))
    def manual(self,owner='staff',sender='모의회사'):
        return assist.command(self.db,owner,'접수 직접입력확인 parent\n보내는분: '+sender+'\n확인근거: 현재 직원이 원문을 확인함')
    def test_untracked_pending_held_without_guessing_sender_from_owner(self):
        self.legacy();self.assertEqual(origin.classify_untracked(self.db),1)
        self.assertEqual(self.row()['state'],'origin_hold')
        with self.db() as c:self.assertFalse(c.execute('SELECT 1 FROM order_sources').fetchone())
        self.assertIn('출처 미확인',assist.command(self.db,'staff','주문 검토 대기 출처'))
    def test_paid_and_completed_legacy_are_not_quarantined(self):
        with self.db() as c:ops.stage(c,'paid','','payapp','',{},ops.now())
        self.assertEqual(origin.classify_untracked(self.db),0)
        self.assertEqual(self.row()['state'],'saved')
    def test_enforced_save_stops_untracked_before_extract_or_write(self):
        self.legacy();extract=Mock();write=Mock();guard=Mock()
        with self.assertRaises(origin.OriginUnverified):ops.save(self.db,'parent',extract,write,source_guard=guard)
        extract.assert_not_called();write.assert_not_called();guard.assert_not_called()
    def test_new_trusted_manual_intake_is_distinct_from_legacy_replay(self):
        key,_=ops.intake(self.db,'staff','주문 접수\n설치장소: 모의홀\n보내는분: 신규 모의회사',trusted_manual=True)
        with self.db() as c:self.assertTrue(origin.approved(c,key))
        self.legacy();legacy=self.row()['raw'];oldkey,_=ops.intake(self.db,'staff',legacy)
        ops.intake(self.db,'staff',legacy,trusted_manual=True)
        with self.db() as c:self.assertFalse(origin.approved(c,oldkey))
    def test_manual_reconfirmation_preserves_id_and_requires_owner(self):
        self.legacy();origin.classify_untracked(self.db)
        self.assertIn('확인하지 못',self.manual('other'))
        self.assertIn('재개 요청',self.manual())
        self.assertEqual(self.row()['state'],'pending')
        writer=Mock(side_effect=lambda key,data,created,update:(21381,data))
        ops.save(self.db,'parent',Mock(),writer,source_guard=Mock(side_effect=AssertionError('manual origin needs no customer default')))
        self.assertEqual(writer.call_args.args[0],'parent')
    def test_reconfirmation_receipt_replay_does_not_make_another_order(self):
        self.legacy();origin.classify_untracked(self.db);self.manual()
        with self.db() as c:count=c.execute('SELECT COUNT(*) FROM order_changes').fetchone()[0]
        self.assertIn('이미 재개',self.manual())
        key,_=ops.intake(self.db,'staff',self.row()['raw'],trusted_manual=True);self.assertEqual(key,'parent')
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM order_changes').fetchone()[0],count)
    def test_sender_field_injection_and_pending_proof_held(self):
        self.legacy();origin.classify_untracked(self.db)
        self.assertIn('다른 주문 항목',self.manual(sender='모의회사\n배송일: 2099-12-01'))
        result=assist.command(self.db,'staff','접수 직접입력확인 parent\n보내는분: 모의회사\n확인근거: 아직 확인 예정입니다')
        self.assertIn('실제로',result);self.assertEqual(self.row()['state'],'origin_hold')
    def test_restoration_failure_rolls_back_approval_alias_and_state(self):
        self.legacy();origin.classify_untracked(self.db)
        with patch.object(assist,'record',side_effect=ValueError('synthetic config failure')):self.assertIn('저장하지',self.manual())
        self.assertEqual(self.row()['state'],'origin_hold')
        with self.db() as c:
            self.assertFalse(origin.approved(c,'parent'));self.assertEqual(c.execute('SELECT COUNT(*) FROM order_intake_aliases').fetchone()[0],0)

class SourceReconfirmationTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    def source(self):
        self.config=self.path.with_name('customer-memory.sqlite3');m=CustomerMemory(self.config)
        m.confirm_profile('c','모의고객','모의회사','test');m.allow_room('room','test');m.bind('room','sender','c','test');m.db.close()
        self.text,decision=prepare('고객 주문 room sender\n설치장소: 모의홀\n보내는분: 모의회사',self.config)
        self.key,_=ops.intake(self.db,'staff',self.text,decision.source_context)
        self.old=decision.source_context
        m=CustomerMemory(self.config);m.disable_room('room','test');m.allow_room('room','test');m.bind('room','sender','c','test');m.db.close()
        customer_sources.suspend_revoked(self.db,self.config)
    def command(self,room='room',sender='sender'):
        return assist.command(self.db,'staff',f'접수 연결재확인 {self.key}\n원본방: {room}\n발신자ID: {sender}\n보내는분: 모의회사\n확인근거: 원본 방과 현재 문구를 직원이 확인함',config_path=self.config)
    def test_explicit_reconfirmation_refreshes_grant_without_new_operation(self):
        self.source();self.assertIn('재개 요청',self.command())
        with self.db() as c:
            new=json.loads(c.execute('SELECT context FROM order_sources WHERE order_id=?',(self.key,)).fetchone()[0])
        self.assertNotEqual(new['room_grant'],self.old['room_grant'])
        writer=Mock(side_effect=lambda key,data,created,update:(21381,data))
        ops.save(self.db,self.key,__import__('order_intake').extract,writer,source_guard=lambda ctx:customer_sources.guard(self.config,ctx))
        key,_=ops.intake(self.db,'staff',self.text,new);self.assertEqual(key,self.key)
        ops.save(self.db,key,Mock(),writer,source_guard=lambda ctx:customer_sources.guard(self.config,ctx));writer.assert_called_once()
    def test_room_sender_cannot_be_silently_switched_or_bypassed_as_manual(self):
        self.source();self.assertIn('달라',self.command(room='other'))
        result=assist.command(self.db,'staff',f'접수 직접입력확인 {self.key}\n보내는분: 모의회사\n확인근거: 직원이 현재 원문을 확인함')
        self.assertIn('우회할 수 없',result)
    def test_reconfirmation_does_not_undo_cancellation(self):
        self.source()
        with self.db() as c:c.execute("UPDATE order_operations SET state='cancel_requested' WHERE id=?",(self.key,))
        self.assertIn('보류된 주문만',self.command())
