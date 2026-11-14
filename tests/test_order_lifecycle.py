import contextlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
import order_operations as ops
import order_lifecycle as life

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'orders.db'
        @contextlib.contextmanager
        def db():
            c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
            try:
                with c:yield c
            finally:c.close()
        self.db=db
        self.original={'sender':'모의회사','venue':'모의홀','address':'서울특별시 강남구','event_time':'14:00','delivery_time':'10:00','recipient':'모의수신자'}
        with db() as c:
            ops.setup(c);ops.stage(c,'parent','staff','kakao','raw',self.original,ops.now())
            c.execute("UPDATE order_operations SET state='saved',order_number=21380 WHERE id='parent'")
    def command(self,text,owner='staff'):return life.command(self.db,owner,text)
    def row(self):
        with self.db() as c:return dict(c.execute("SELECT * FROM order_operations WHERE id='parent'").fetchone())
    def test_amend_same_id_then_save_updates_existing_row(self):
        self.command('주문 수정 21380\n주소: 부산광역시 해운대구')
        row=self.row();self.assertEqual(row['state'],'update');self.assertEqual(row['order_number'],21380)
        writer=Mock(side_effect=lambda key,data,created,update:(21380,data))
        number,data=ops.save(self.db,'parent',Mock(side_effect=AssertionError('do not reparse')),writer)
        self.assertTrue(writer.call_args.kwargs['update']);self.assertEqual(number,21380)
        self.assertEqual(data['event_time'],'14:00');self.assertEqual(data['address'],'부산광역시 해운대구')
    def test_cancel_blocks_even_direct_save_and_is_idempotent(self):
        self.assertIn('미확인',self.command('주문 취소 21380'))
        self.command('주문 취소 21380')
        with self.assertRaises(ValueError):ops.save(self.db,'parent',Mock(),Mock())
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM order_changes').fetchone()[0],1)
    def test_wrong_owner_cannot_change_or_copy(self):
        for cmd in ('주문 취소 21380','주문 수정 21380\n주소: 다른주소','추가 주문 21380\n요청ID: abcd'):
            self.command(cmd,'other')
        self.assertEqual(self.row()['state'],'saved')
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM order_operations').fetchone()[0],1)
    def test_additional_retry_is_one_new_order(self):
        self.command('추가 주문 21380\n요청ID: request1');self.command('추가 주문 21380\n요청ID: request1')
        with self.db() as c:
            rows=c.execute("SELECT * FROM order_operations WHERE id!='parent'").fetchall()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['state'],'pending')
        writer=Mock(side_effect=lambda key,data,created,update:(21381,data))
        ops.save(self.db,rows[0]['id'],Mock(),writer)
        self.assertFalse(writer.call_args.kwargs['update']);self.assertEqual(self.row()['order_number'],21380)
    def test_two_explicit_requests_are_two_new_orders(self):
        for rid in ('request1','request2'):self.command('추가 주문 21380\n요청ID: '+rid)
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM order_operations').fetchone()[0],3)
    def test_ambiguous_and_invalid_changes_do_not_mutate(self):
        for cmd in ('아까 주문 취소','같은 걸 하나 더','추가 주문 21380','주문 수정 21380\n배송일: 2026-02-30','주문 수정 21380\n배송시간: 25:00','주문 수정 21380\n수량: 2','주문 수정 21380\n주소:'):
            self.assertIsNotNone(self.command(cmd));self.assertEqual(self.row()['state'],'saved')
    def test_ordinary_order_unchanged(self):self.assertIsNone(self.command('주문 접수\n설치장소: 모의홀\n보내는분: 모의회사'))
    def test_cancel_pending_amendment_prevents_write(self):
        self.command('주문 수정 21380\n주소: 다른주소');self.command('주문 취소 21380')
        self.assertEqual(self.row()['state'],'cancel_requested')
    def test_cancelled_order_cannot_be_copied(self):
        self.command('주문 취소 21380');self.command('추가 주문 21380\n요청ID: request1')
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM order_operations').fetchone()[0],1)
