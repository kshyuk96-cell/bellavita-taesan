import json,unittest
from unittest.mock import Mock
import test_order_lifecycle
from customer_memory import CustomerMemory
from customer_orders import prepare
import customer_sources as sources,order_operations as ops,order_intake

class SourceTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    def setup_source(self):
        self.config=self.path.with_name('customer-memory.sqlite3');m=CustomerMemory(self.config)
        m.confirm_profile('c','모의고객','모의회사','test');m.allow_room('room','test');m.bind('room','sender','c','test');m.db.close()
        text,decision=prepare('고객 주문 room sender\n설치장소: 모의홀\n보내는분: 모의회사',self.config)
        key,_=ops.intake(self.db,'staff',text,decision.source_context)
        self.key=key;self.context=decision.source_context
        return text
    def guard(self,context):return sources.guard(self.config,context)
    def disable(self):
        m=CustomerMemory(self.config);m.disable_room('room','test');m.db.close()
    def state(self):
        with self.db() as c:return c.execute('SELECT state FROM order_operations WHERE id=?',(self.key,)).fetchone()[0]
    def test_revoked_room_blocks_before_extract_or_write(self):
        self.setup_source();self.disable();extract=Mock();writer=Mock()
        with self.assertRaises(sources.SourceRevoked):ops.save(self.db,self.key,extract,writer,source_guard=self.guard)
        extract.assert_not_called();writer.assert_not_called();self.assertEqual(self.state(),'source_hold')
    def test_missing_guard_is_fail_closed(self):
        self.setup_source()
        with self.assertRaises(sources.SourceRevoked):ops.save(self.db,self.key,Mock(),Mock())
        self.assertEqual(self.state(),'source_hold')
    def test_reenable_does_not_restore_old_pending_grants(self):
        self.setup_source();self.disable();m=CustomerMemory(self.config);m.allow_room('room','test');m.bind('room','sender','c','test');m.db.close()
        with self.assertRaises(sources.SourceRevoked):ops.save(self.db,self.key,Mock(),Mock(),source_guard=self.guard)
    def test_unbind_rebind_also_requires_new_order_confirmation(self):
        self.setup_source();m=CustomerMemory(self.config);m.unbind('room','sender','c','test');m.bind('room','sender','c','test');m.db.close()
        with self.assertRaises(sources.SourceRevoked):ops.save(self.db,self.key,Mock(),Mock(),source_guard=self.guard)
    def test_valid_source_can_save_and_provenance_survives_restart(self):
        self.setup_source();writer=Mock(side_effect=lambda key,data,created,update:(21381,data))
        number,_=ops.save(self.db,self.key,order_intake.extract,writer,source_guard=self.guard)
        self.assertEqual(number,21381);self.assertEqual(self.state(),'saved')
        self.disable();self.assertEqual(sources.suspend_revoked(self.db,self.config),1);self.assertEqual(self.state(),'source_hold')
        with self.db() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM order_share_jobs WHERE order_id=? AND state!='stale'",(self.key,)).fetchone()[0],0)
    def test_source_dict_order_does_not_create_duplicate(self):
        text=self.setup_source();reordered=dict(reversed(list(self.context.items())))
        key,_=ops.intake(self.db,'staff',text,reordered);self.assertEqual(key,self.key)
    def test_revoked_source_also_blocks_share_receipt(self):
        import order_sharing
        self.setup_source();ops.save(self.db,self.key,order_intake.extract,lambda key,data,created,update:(21381,data),source_guard=self.guard)
        with self.db() as c:job=dict(c.execute("SELECT * FROM order_share_jobs WHERE kind='summary' AND order_id=?",(self.key,)).fetchone())
        self.disable()
        with self.db() as c,self.assertRaises(sources.SourceRevoked):
            order_sharing.record_verified_receipt(c,job['id'],'기획방',job['body_digest'],'synthetic','2026-11-14T12:00:00+09:00',source_guard=self.guard)
    def test_corrupt_config_holds_tagged_orders_without_crashing_sweep(self):
        self.setup_source();self.config.write_bytes(b'not a sqlite database')
        self.assertEqual(sources.suspend_revoked(self.db,self.config),1);self.assertEqual(self.state(),'source_hold')

    def test_default_text_change_keeps_accepted_order_snapshot(self):
        self.setup_source();m=CustomerMemory(self.config);m.confirm_profile('c','모의고객','새 모의회사','test');m.db.close()
        with self.guard(self.context):pass

class SourceDispatchTests(unittest.TestCase):
    def test_source_context_is_persisted_and_dispatch_holds_revoked_pending_order(self):
        import tempfile,time
        from pathlib import Path
        from unittest.mock import patch
        import agent
        with tempfile.TemporaryDirectory() as root,patch.object(agent,'ROOT',Path(root)):
            path=Path(root)/'customer-memory.sqlite3';m=CustomerMemory(path)
            m.confirm_profile('c','모의고객','모의회사','test');m.allow_room('room','test');m.bind('room','sender','c','test');m.db.close()
            with agent.db() as c:c.execute('INSERT INTO staff VALUES (?,?,?)',('source-worker','staff',time.time()))
            def dispatch(text):return agent.dispatch({'userRequest':{'user':{'id':'source-worker'},'utterance':text}})
            result=dispatch('고객 주문 room sender\n설치장소: 모의홀\n보내는분: 모의회사')
            self.assertIn('보관',str(result))
            with agent.db() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM order_sources').fetchone()[0],1)
                self.assertIn('room_grant',c.execute('SELECT input FROM jobs').fetchone()[0])
            m=CustomerMemory(path);m.disable_room('room','test');m.db.close()
            dispatch('주문 검토 대기 연결')
            with agent.db() as c:self.assertEqual(c.execute('SELECT state FROM order_operations').fetchone()[0],'source_hold')
