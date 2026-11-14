import json,unittest
from unittest.mock import Mock
import test_order_lifecycle
from sheet_fake import MemoryLedger
from native_form_fake import native_payload
import order_operations as ops
import order_intake,order_lifecycle as life,order_assistance as assist,order_sharing as sharing

class LedgerVerificationTests(unittest.TestCase):
    def test_dropped_artwork_size_message_notes_each_prevents_success(self):
        data={'venue':'모의홀','address':'서울특별시 강남구','sender':'모의회사','recipient':'모의수신자','artwork':'모의작품','size':'2호','event_type':'결혼','message':'모의문구','note':'모의비고'}
        for column in (3,4,9,17):
            with self.subTest(column=column):
                book=MemoryLedger();book.drop_column=column
                with self.assertRaises(ValueError):book.save('synthetic-marker',data,'2026-11-14T12:00:00+09:00')
    def test_update_clear_is_verified(self):
        book=MemoryLedger();data={'venue':'모의홀','sender':'모의회사','message':'기존문구'}
        book.save('synthetic-marker',data,'2026-11-14T12:00:00+09:00')
        book.drop_column=17
        with self.assertRaises(ValueError):book.save('synthetic-marker',{**data,'message':''},'2026-11-14T12:00:00+09:00',update=True)
    def test_matching_full_roundtrip_and_midnight_clock(self):
        book=MemoryLedger();data={'venue':'모의홀','sender':'모의회사','event_time':'00:00','delivery_date':'2026-12-01','size':'2호'}
        number,_=book.save('synthetic-marker',data,'2026-11-14T12:00:00+09:00')
        self.assertEqual(number,5);self.assertEqual(book.rows[5][2],{'numberValue':0.0})

class FullFlowTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    def test_intake_write_amend_fragments_additional_receipts_cancel(self):
        book=MemoryLedger()
        text='주문 접수\n설치장소: 모의홀\n주소: 서울특별시 강남구\n보내는분: 모의회사\n받는분: 모의수신자\n행사구분: 결혼\n작품: 모의작품\n규격: 2호\n배송일: 2099-12-01\n배송시간: 10:00'
        key,_=ops.intake(self.db,'staff',text,trusted_manual=True)
        number,data=ops.save(self.db,key,order_intake.extract,book.save)
        self.assertEqual(number,5)
        key_again,_=ops.intake(self.db,'staff',text,trusted_manual=True);self.assertEqual(key_again,key)
        ops.save(self.db,key_again,Mock(),book.save);self.assertEqual(book.posts,1)
        life.command(self.db,'staff','주문 수정 5\n설치장소: 새 모의홀\n주소: 부산광역시 해운대구')
        ops.save(self.db,key,Mock(),book.save);self.assertEqual(len(book.rows),1)
        command='주문 문구추가 5\n메시지ID: title-002\n보내는분: 모의협회 회장'
        assist.command(self.db,'staff',command);assist.command(self.db,'staff',command)
        ops.save(self.db,key,Mock(),book.save)
        self.assertIn('모의협회 회장',book.rows[5][15]['stringValue'])
        with self.db() as c:
            details=json.loads(c.execute('SELECT details FROM order_operations WHERE id=?',(key,)).fetchone()[0])
            self.assertTrue(sharing.attach_native_form(c,key,native_payload(number,details),'synthetic',5)['ready_for_final_share'])
            jobs=c.execute("SELECT * FROM order_share_jobs WHERE order_id=? AND state='waiting'",(key,)).fetchall()
            self.assertEqual(len(jobs),2)
            for i,job in enumerate(jobs):sharing.record_verified_receipt(c,job['id'],'기획방',job['body_digest'],f'synthetic-{i}','2026-11-14T12:00:00+09:00')
        self.assertIn('카톡 공유: 기획방',assist.command(self.db,'staff','주문 상태 5'))
        life.command(self.db,'staff','추가 주문 5\n요청ID: additional-1');life.command(self.db,'staff','추가 주문 5\n요청ID: additional-1')
        with self.db() as c:
            extra=c.execute("SELECT * FROM order_operations WHERE raw='' AND owner='staff'").fetchall()
        self.assertEqual(len(extra),1);ops.save(self.db,extra[0]['id'],Mock(),book.save)
        self.assertEqual(len(book.rows),2)
        life.command(self.db,'staff','주문 취소 5')
        self.assertIn('취소 요청',assist.command(self.db,'staff','주문 상태 5'))
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM order_transmissions WHERE order_id=?',(key,)).fetchone()[0],0)
        with self.assertRaises(ValueError):ops.save(self.db,key,Mock(),book.save)
