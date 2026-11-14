import json,unittest
from unittest.mock import Mock
import test_order_lifecycle
from native_form_fake import native_payload
from shipping_formula_proposal import proposal,model
import order_operations as ops,order_sharing as sharing
from native_order_form import inspect_form

class FormulaTests(unittest.TestCase):
    def test_proposal_never_uses_event_clock(self):
        for selector in ('$C$5','$I$5'):
            formula=proposal(selector);self.assertNotIn('액자화환!C',formula);self.assertIn('액자화환!J',formula)
    def test_clock_missing_invalid_and_conflicting_stay_unknown(self):
        for notes,result in (('행사 14:00','미확인'),('배송시각 25:00','확인 필요'),('배송시각 10:00 / 배송시각 11:00','확인 필요'),('배송시각 00:00','12.01 00:00')):
            self.assertEqual(model('2099-12-01',notes),result)

class NativeSharingTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    def save(self):
        self.details={'sender':'모의회사','recipient':'모의수신자','venue':'모의홀','address':'서울특별시 강남구','event_type':'결혼','artwork':'모의작품','size':'2호','delivery_date':'2099-12-01','delivery_time':'10:00','notes':'배송시각 10:00'}
        with self.db() as c:c.execute("UPDATE order_operations SET state='pending',details=? WHERE id='parent'",(json.dumps(self.details),))
        ops.save(self.db,'parent',Mock(),lambda key,data,created,update:(21380,data))
    def test_unchecked_form_cannot_confirm_final_sharing(self):
        self.save()
        with self.db() as c:
            job=c.execute("SELECT * FROM order_share_jobs WHERE kind='form'").fetchone()
            self.assertEqual(job['state'],'form_review')
            with self.assertRaises(ValueError):sharing.record_verified_receipt(c,job['id'],'기획방',job['body_digest'],'synthetic','2026-11-14T12:00:00+09:00')
    def test_verified_native_text_replaces_summary_and_old_digest_rejected(self):
        self.save()
        with self.db() as c:
            old=c.execute("SELECT * FROM order_share_jobs WHERE kind='form'").fetchone()
            result=sharing.attach_native_form(c,'parent',native_payload(21380,self.details),'synthetic',5)
            self.assertTrue(result['ready_for_final_share'])
            new=c.execute("SELECT * FROM order_share_jobs WHERE kind='form'").fetchone()
            self.assertEqual(new['native_checked'],1);self.assertNotEqual(old['body_digest'],new['body_digest'])
            self.assertIn('원본 주문서 텍스트',new['body'])
            with self.assertRaises(ValueError):sharing.record_verified_receipt(c,new['id'],'기획방',old['body_digest'],'old','2026-11-14T12:00:00+09:00')
    def test_wrong_selector_or_old_clock_stays_blocked(self):
        self.save()
        for number,oldclock in ((21381,False),(21380,True)):
            with self.db() as c:
                result=sharing.attach_native_form(c,'parent',native_payload(number,self.details,oldclock),'synthetic',5)
                self.assertFalse(result['ready_for_final_share'])
                self.assertEqual(c.execute("SELECT state FROM order_share_jobs WHERE kind='form'").fetchone()[0],'form_review')
    def test_native_content_must_equal_verified_order(self):
        self.save();bad={**self.details,'sender':'다른 모의회사'}
        result=inspect_form(native_payload(21380,bad),'synthetic',5,21380,self.details)
        self.assertFalse(result['ready_for_final_share'])

    def test_notes_clock_cannot_replace_conflicting_explicit_shipping_time(self):
        self.save();bad={**self.details,'notes':'배송시각 14:00'}
        result=inspect_form(native_payload(21380,bad),'synthetic',5,21380,bad)
        self.assertFalse(result['ready_for_final_share'])
        self.assertIn('explicit_shipping_missing_or_conflicting',[i['code'] for i in result['hazards']])
