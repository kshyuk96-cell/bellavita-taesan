import datetime as dt,json,unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo
import test_order_lifecycle
import order_assistance as assist
import order_operations as ops

NOW=dt.datetime(2026,11,14,12,tzinfo=ZoneInfo('Asia/Seoul'))
COMPLETE={'sender':'모의회사','recipient':'모의수신자','event_type':'결혼','delivery_date':'2026-11-15','delivery_time':'10:00','event_date':'2026-11-15','event_time':'14:00','artwork':'모의작품','size':'2호','venue':'모의홀','address':'서울특별시 강남구'}

class AssessmentTests(unittest.TestCase):
    def test_question_only_missing_fields(self):
        data=dict(COMPLETE);del data['sender'];del data['size']
        self.assertEqual(assist.question(data,NOW),'이번 주문에서 보내는 분, 규격만 알려주세요.')
    def test_complete_order_needs_no_question(self):self.assertFalse(assist.assess(COMPLETE,NOW)['missing'])
    def test_event_clock_not_substituted_for_shipping(self):
        data=dict(COMPLETE);del data['delivery_time']
        self.assertIn('배송시각',assist.assess(data,NOW)['missing'])
    def test_past_after_event_invalid_and_imminent(self):
        for changes,code,hold in (({'delivery_date':'2026-11-13'},'delivery_past',True),({'delivery_time':'15:00'},'delivery_not_before_event',True),({'delivery_date':'2026-02-30'},'delivery_invalid',True),({'delivery_date':'2026-11-14','delivery_time':'13:00'},'delivery_soon',False)):
            result=assist.schedule({**COMPLETE,**changes},NOW)
            self.assertTrue(any(i['code']==code and i['hold']==hold for i in result))
    def test_same_time_as_event_needs_review(self):self.assertTrue(assist.assess({**COMPLETE,'delivery_time':'14:00'},NOW)['hold'])
    def test_korean_midnight_boundary(self):
        now=dt.datetime(2026,11,14,15,5,tzinfo=dt.timezone.utc)
        self.assertTrue(any(i['code']=='delivery_past' for i in assist.schedule({'delivery_date':'2026-11-14'},now)))
    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):assist.schedule(COMPLETE,dt.datetime(2026,11,14))

class AssistanceCommandsTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    row=test_order_lifecycle.LifecycleTests.row
    def command(self,text,owner='staff'):return assist.command(self.db,owner,text)
    def test_sender_fragments_append_in_order_and_replay_once(self):
        cmd='주문 문구추가 21380\n메시지ID: fragment1\n보내는분: 모의협회 회장\n모의재단 이사'
        self.assertIn('합쳤',self.command(cmd));self.assertIn('이미',self.command(cmd))
        data=json.loads(self.row()['details']);self.assertEqual(data['sender'],'모의회사\n모의협회 회장\n모의재단 이사')
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM order_changes').fetchone()[0],1)
    def test_same_id_different_body_held(self):
        cmd='주문 문구추가 21380\n메시지ID: fragment1\n보내는분: 모의협회 회장'
        self.command(cmd);self.assertIn('다른 내용',self.command(cmd+' 다름'))
    def test_fragment_requires_target_and_message_id(self):
        self.assertIsNone(self.command('이사장 모의인사'))
        self.assertIn('메시지ID',self.command('주문 문구추가 21380\n보내는분: 모의협회 회장'))
    def test_owner_isolation_status_and_waiting_list(self):
        self.assertIn('확인하지 못',self.command('주문 상태 21380','other'))
        self.assertNotIn('21380',self.command('주문 검토 대기','other'))
        self.assertIn('카톡 공유: 미확인',self.command('주문 상태 21380'))
        self.assertIn('21380',self.command('주문 검토 대기'))
    def test_pending_reference_supplement(self):
        with self.db() as c:c.execute("UPDATE order_operations SET state='pending',order_number=NULL WHERE id='parent'")
        self.assertIn('합쳤',self.command('접수 보완 parent\n규격: 2호'))
        self.assertEqual(self.row()['state'],'pending')
    def test_no_fabricated_share_receipt(self):
        result=self.command('주문 상태 21380');self.assertIn('원장 반영 확인',result);self.assertIn('공유: 미확인',result)
    def test_invalid_supplement_does_not_mutate(self):
        self.assertIn('보류',self.command('주문 보완 21380\n배송일: 2026-02-30'))
        self.assertEqual(self.row()['state'],'saved')
    def test_cancelled_cannot_append(self):
        with self.db() as c:c.execute("UPDATE order_operations SET state='cancel_requested' WHERE id='parent'")
        self.assertIn('취소 요청',self.command('주문 문구추가 21380\n메시지ID: abcde\n보내는분: 모의협회'))
    def test_schedule_hold_blocks_writer(self):
        with self.db() as c:c.execute("UPDATE order_operations SET state='pending',details=? WHERE id='parent'",(json.dumps({**COMPLETE,'delivery_date':'2026-02-30'}),))
        writer=Mock()
        with self.assertRaises(ValueError):ops.save(self.db,'parent',Mock(),writer)
        writer.assert_not_called()
        with self.db() as c:self.assertIn('delivery_invalid',c.execute('SELECT assessment FROM order_triage').fetchone()[0])

    def test_unparsed_draft_can_be_corrected_without_fetching(self):
        with self.db() as c:
            c.execute("UPDATE order_operations SET state='pending',order_number=NULL,details='{}',raw=? WHERE id='parent'",('주문 접수\nhttps://bojagicard.com/test\n설치장소: 모의홀',))
        self.assertIn('보내는 분',self.command('접수 확인질문 parent'))
        self.assertIn('원문을 보완',self.command('접수 보완 parent\n보내는분: 모의회사'))
        row=self.row();self.assertEqual(json.loads(row['details']),{})
        self.assertIn('https://bojagicard.com/test',row['raw']);self.assertIn('보내시는분: 모의회사',row['raw'])
    def test_conflicting_original_draft_is_not_silently_rewritten(self):
        with self.db() as c:
            c.execute("UPDATE order_operations SET state='pending',order_number=NULL,details='{}',raw=? WHERE id='parent'",('주문 접수\n주소: 모의주소A\n주소: 모의주소B',))
        self.assertIn('수동 검토',self.command('접수 보완 parent\n보내는분: 모의회사'))
