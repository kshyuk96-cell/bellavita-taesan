import json
import sqlite3
import test_order_handoff
from test_shop import notification
import agent
import order_ops
import partial_reviews
import shop_receiver


class PartialReviewTest(test_order_handoff.HandoffTest):
    def cancel_part(self, number='9001', amount=10000):
        self.notify(notification('a'*32,number,'70',amount,orig_mul_no='1234',orig_price='93500'))

    def review_fields(self, decision='resume'):
        with agent.db() as c:
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',('a'*32,)).fetchone()
            fingerprint,_,remaining=partial_reviews.balance(c,row)
            latest=c.execute('SELECT id FROM shop_partial_reviews ORDER BY id DESC LIMIT 1').fetchone()
            revision=latest['id'] if latest else 0
        return dict(action='review_partial',order_id='a'*32,partial_decision=decision,partial_fingerprint=fingerprint,
                    partial_review_revision=str(revision),partial_remaining=str(remaining),partial_reviewer='검증 운영자',partial_note='모의 페이앱 결제번호와 잔액 대조',partial_compared='yes')

    def test_partial_resume_keeps_finances_and_releases_once(self):
        self.pay();shop_receiver.run_once(self.receiver,'factory',self.transport);self.cancel_part()
        shop_receiver.run_once(self.receiver,'factory',self.transport)
        self.assertEqual('held',self.local_state())
        fields=self.review_fields();self.post_admin(**fields);self.post_admin(**fields)
        self.assertIsNone(self.claim()) # Old receivers cannot lease schema 2 resume.
        self.assertTrue(shop_receiver.run_once(self.receiver,'factory',self.transport))
        self.assertEqual('pending',self.local_state())
        self.assertFalse(shop_receiver.run_once(self.receiver,'factory',self.transport))
        with agent.db() as c:
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',('a'*32,)).fetchone()
            self.assertEqual('partial_cancel',row['state']);self.assertTrue(partial_reviews.allowed(c,row))
            self.assertEqual(1,c.execute('SELECT count(*) FROM shop_partial_reviews').fetchone()[0])
        self.post_admin(action='change_stage',order_id='a'*32,stage='shipping',version='1')
        with sqlite3.connect(self.receiver/'receiver.sqlite3') as c:
            self.assertEqual(1,c.execute('SELECT count(*) FROM production_queue').fetchone()[0])

    def test_stale_review_and_new_refund_revokes_approval(self):
        self.pay();self.cancel_part();fields=self.review_fields();self.cancel_part('9002')
        self.assertEqual(422,self.http(self.admin,fields)[0])
        fields=self.review_fields();fields['partial_remaining']='1'
        self.assertEqual(422,self.http(self.admin,fields)[0])
        self.post_admin(**self.review_fields());self.cancel_part('9003')
        self.assertEqual(422,self.http(self.admin,dict(action='change_stage',order_id='a'*32,stage='shipping',version='1'))[0])
        with agent.db() as c:
            row=c.execute('SELECT * FROM invitation_orders WHERE id=?',('a'*32,)).fetchone()
            self.assertFalse(partial_reviews.allowed(c,row))

    def test_hold_after_resume_and_delayed_events_cannot_unhold(self):
        self.pay();self.cancel_part();self.post_admin(**self.review_fields())
        while shop_receiver.run_once(self.receiver,'factory',self.transport):pass
        self.assertEqual('pending',self.local_state())
        with agent.db() as c:event=c.execute("SELECT * FROM shop_handoff WHERE kind='resume'").fetchone()
        self.post_admin(**self.review_fields('hold'))
        shop_receiver.run_once(self.receiver,'factory',self.transport);self.assertEqual('held',self.local_state())
        job=dict(event_id=event['event_id'],order_id=event['order_id'],kind='resume',payload=json.loads(event['payload']),sha256=event['digest'],lease_token='f'*64,consumer='factory')
        shop_receiver.ingest(self.receiver,job);shop_receiver.finish(self.receiver,job['event_id'])
        self.assertEqual('held',self.local_state())
        self.post_admin(**self.review_fields())
        shop_receiver.run_once(self.receiver,'factory',self.transport);self.assertEqual('pending',self.local_state())

    def test_cancel_during_resume_lease_holds_receiver(self):
        self.pay();self.cancel_part();self.post_admin(**self.review_fields())
        shop_receiver.run_once(self.receiver,'factory',self.transport) # hold first
        job=self.transport('claim',{'consumer':'factory','capabilities':['partial-resume-v1']})['job']
        shop_receiver.ingest(self.receiver,job);self.notify(notification('a'*32,state='9'))
        shop_receiver.acknowledge_pending(self.receiver,self.transport);self.assertEqual('held',self.local_state())

    def test_stale_admin_decision_cannot_override_later_hold(self):
        self.pay();self.cancel_part();stale=self.review_fields()
        self.post_admin(**stale);self.post_admin(**self.review_fields('hold'))
        self.assertEqual(422,self.http(self.admin,stale)[0])

    def test_photo_can_be_reviewed_after_partial_but_resume_requires_review(self):
        self.pay(photo=True);self.cancel_part()
        self.assertEqual(422,self.http(self.admin,self.review_fields())[0])
        self.post_admin(action='review_shop_photo',order_id='a'*32,both_faces='yes')
        self.post_admin(**self.review_fields())
        self.assertTrue(shop_receiver.run_once(self.receiver,'factory',self.transport))
        self.assertTrue(shop_receiver.run_once(self.receiver,'factory',self.transport))
        self.assertEqual('pending',self.local_state())

    def test_delayed_old_ack_conflict_cannot_hold_newer_approved_resume(self):
        self.pay();job=self.claim();shop_receiver.ingest(self.receiver,job)
        shop_receiver.acknowledge_pending(self.receiver,self.transport)
        self.cancel_part();self.post_admin(**self.review_fields())
        while shop_receiver.run_once(self.receiver,'factory',self.transport):pass
        self.assertEqual('pending',self.local_state())
        shop_receiver.finish(self.receiver,job['event_id'],payment_changed=True)
        self.assertEqual('pending',self.local_state())
