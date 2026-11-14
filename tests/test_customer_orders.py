import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from customer_memory import CustomerMemory
from customer_orders import prepare,allow_room
import order_intake

class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'memory.db'
        m=CustomerMemory(self.path)
        m.confirm_profile('c','모의 고객','모의 협회 회장','test')
        m.bind('room1','sender1','c','test');allow_room(m,'room1','test');m.db.close()
    def test_link_only_gets_confirmed_default(self):
        message,decision=prepare('고객 주문 room1 sender1\nhttps://bojagicard.com/test',self.path)
        self.assertEqual(decision.source,'confirmed_default')
        with patch.object(order_intake,'wedding',return_value={'event_type':'결혼','venue':'모의 예식장','recipient':'모의 수신자','address':'부산광역시 해운대구','event_date':'2026-12-01','event_time':'14:00'}):
            result=order_intake.extract(message)
        self.assertEqual(result['sender'],'모의 협회 회장')
        self.assertEqual(result['event_time'],'14:00')
    def test_other_room_blocked_before_fetch(self):
        with self.assertRaises(ValueError):prepare('고객 주문 private sender1\nhttps://bojagicard.com/test',self.path)
    def test_unknown_sender_held(self):
        with self.assertRaises(ValueError):prepare('고객 주문 room1 other\nhttps://bojagicard.com/test',self.path)
    def test_explicit_multiline_wins(self):
        message,d=prepare('고객 주문 room1 sender1\nhttps://bojagicard.com/test\n보내는분:\n모의 회사 대표\n모의 협회 회장',self.path)
        self.assertEqual(d.sender,'모의 회사 대표\n모의 협회 회장')
        self.assertNotIn('보내는 분: 모의 협회 회장',message)
    def test_unlabeled_current_signature_is_not_overwritten(self):
        with self.assertRaises(ValueError):prepare('고객 주문 room1 sender1\nhttps://bojagicard.com/test\n다른 모임 회장',self.path)
    def test_employee_channel_message_is_not_customer_identity(self):
        text='https://bojagicard.com/test'
        self.assertEqual(prepare(text,self.path),(text,None))
    def test_revoked_room_blocks_existing_binding(self):
        m=CustomerMemory(self.path);m.db.execute('DELETE FROM customer_order_rooms');m.db.commit();m.db.close()
        with self.assertRaises(ValueError):prepare('고객 주문 room1 sender1\nhttps://bojagicard.com/test',self.path)
