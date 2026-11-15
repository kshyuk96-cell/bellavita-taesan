from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from taesan_core import AccessDenied, CoreError, Principal, Store
from taesan_core.commerce import Order
from taesan_core.order_workflow import (DIMENSIONS, RequestPolicy, StatusMap,
    StatusObservation, Webhooks, assess_request, handoff)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'projection.sqlite3'
        self.store = Store(self.path)
        self.staff = Principal('bellavita','staff',frozenset({'writer'}))
        self.reader = replace(self.staff, roles=frozenset({'reader'}))
        self.other = Principal('hope-sunrise','staff',frozenset({'writer'}))
        self.order = Order('bellavita','mall','13370','v1', {
            'event_at':'2099-11-15T14:00:00+09:00','address':'가상시 테스트로 1',
            'quantity':2,'product_id':'fixture-product','sender_key':'sender-fixture',
            'message':'가상 축하 문구'}, generation='generation-1')
        self.mapping = StatusMap('bellavita','mall', {
            'payment': {'PAY_WAIT':'unpaid','PAY_OK':'paid','REFUND':'refunded'},
            'production': {'DRAFT':'draft','READY':'ready','START':'in_progress','MADE':'done'},
            'fulfillment': {'WAIT':'pending','SENT':'shipped','DONE':'delivered'},
            'cancellation': {'ACTIVE':'active','CANCELLED':'cancelled'}},
            conflicts=((('cancellation','cancelled'),('fulfillment','delivered')),), enabled=True)
        self.auth_calls = []
        self.current = self.staff
        def verify(credential, payload):
            self.auth_calls.append((credential, payload['event_id']))
            if credential != 'fixture-signature':
                raise AccessDenied('bad signature')
            return self.current
        self.verify = verify
        self.hooks = Webhooks(self.store,self.mapping,verify=verify,enabled=True)
        self.hooks.register_order(self.staff,self.order)
        self.policy = RequestPolicy('bellavita',frozenset({'message','address'}),
                                    frozenset({'draft','ready'}),frozenset({'draft','ready'}),True)

    def event(self, event='event-1', sequence=1, **changes):
        value = {'tenant':'bellavita','source':'mall','event_id':event,
                 'order_id':'13370','generation':'generation-1','sequence':sequence,
                 'statuses':{'payment':'PAY_OK'}}
        value.update(changes)
        return value

    def receive(self, **kwargs):
        return self.hooks.receive('fixture-signature',self.event(**kwargs))

    def observation(self, **changes):
        state = {'payment':'unpaid','production':'draft','fulfillment':'pending','cancellation':'active'}
        state.update(changes)
        return StatusObservation(state, binding=(self.order.tenant,self.order.source,self.order.external_id,self.order.version,self.order.generation))

    def state(self):
        return self.hooks.state(self.staff,'mall','13370')

    def test_each_dimension_translates_without_payment_inference(self):
        result = self.mapping.translate(self.staff, {'fulfillment':'DONE'})
        self.assertEqual(result.state, {'fulfillment':'delivered'})
        self.assertNotIn('payment',result.state)
        self.assertEqual(result.status,'ok')

    def test_unknown_status_is_review(self):
        result = self.mapping.translate(self.staff, {'payment':'custom-new-status'})
        self.assertEqual(result.status,'review')
        self.assertEqual(result.state,{})

    def test_conflicting_statuses_review(self):
        result = self.mapping.translate(self.staff, {'fulfillment':'DONE','cancellation':'CANCELLED'})
        self.assertEqual(result.reasons,('conflict-1',))

    def test_mapping_defaults_disconnected(self):
        mapping = replace(self.mapping, enabled=False)
        self.assertEqual(mapping.translate(self.staff, {'payment':'PAY_OK'}).status,'review')

    def test_mapping_cross_company_denied(self):
        with self.assertRaises(AccessDenied):
            self.mapping.translate(self.other,{'payment':'PAY_OK'})

    def test_bad_canonical_mapping_rejected(self):
        with self.assertRaises(CoreError):
            StatusMap('bellavita','mall',{'payment':{'PAY_OK':'completed'}})

    def test_unregistered_dimension_rejected(self):
        with self.assertRaises(CoreError):
            self.mapping.translate(self.staff,{'approved':'yes'})

    def test_status_config_defensive_copy(self):
        original={'payment':{'PAY_OK':'paid'}}
        mapping=StatusMap('bellavita','mall',original,enabled=True)
        original['payment']['PAY_OK']='refunded'
        self.assertEqual(mapping.translate(self.staff,{'payment':'PAY_OK'}).state['payment'],'paid')

    def test_disabled_customer_policy_requires_review(self):
        result=assess_request(self.staff,self.order,self.observation(),replace(self.policy,enabled=False),action='cancel')
        self.assertEqual(result.status,'review')
        self.assertFalse(result.can_execute)

    def test_eligible_change_still_needs_approval_no_execution(self):
        result=assess_request(self.staff,self.order,self.observation(),self.policy,action='change',changes={'message':'새 문구'})
        self.assertEqual(result.status,'eligible')
        self.assertTrue(result.approval_required)
        self.assertFalse(result.can_execute)
        self.assertEqual(self.order.fields['message'],'가상 축하 문구')

    def test_unregistered_change_field_requires_review(self):
        result=assess_request(self.staff,self.order,self.observation(),self.policy,action='change',changes={'amount':0})
        self.assertEqual(result.status,'review')

    def test_empty_change_does_not_pass(self):
        self.assertEqual(assess_request(self.staff,self.order,self.observation(),self.policy,action='change').status,'review')

    def test_already_cancelled_blocked(self):
        result=assess_request(self.staff,self.order,self.observation(cancellation='cancelled'),self.policy,action='cancel')
        self.assertEqual(result.status,'blocked')

    def test_paid_cancellation_requires_payment_reconciliation(self):
        result=assess_request(self.staff,self.order,self.observation(payment='paid'),self.policy,action='cancel')
        self.assertEqual(result.reasons,('payment-reconciliation-required',))
        self.assertFalse(result.can_execute)

    def test_delivered_request_requires_review(self):
        result=assess_request(self.staff,self.order,self.observation(fulfillment='delivered'),self.policy,action='cancel')
        self.assertEqual(result.reasons,('delivery-already-started',))

    def test_production_in_progress_requires_review(self):
        result=assess_request(self.staff,self.order,self.observation(production='in_progress'),self.policy,action='cancel')
        self.assertEqual(result.status,'review')

    def test_incomplete_status_does_not_assume_eligibility(self):
        result=assess_request(self.staff,self.order,StatusObservation({}),self.policy,action='cancel')
        self.assertEqual(result.status,'review')

    def test_review_observation_not_eligible(self):
        result=assess_request(self.staff,self.order,StatusObservation({'production':'draft'},('unknown-payment',)),self.policy,action='cancel')
        self.assertEqual(result.status,'review')

    def test_cross_company_request_denied(self):
        with self.assertRaises(AccessDenied):
            assess_request(self.other,self.order,self.observation(),self.policy,action='cancel')

    def test_cancel_cannot_smuggle_changes(self):
        with self.assertRaises(CoreError):
            assess_request(self.staff,self.order,self.observation(),self.policy,action='cancel',changes={'amount':0})

    def test_production_handoff_version_pinned_draft(self):
        result=handoff(self.staff,self.order,'production')
        self.assertEqual(result['status'],'draft')
        self.assertEqual(result['version'],'v1')
        self.assertFalse(result['executed'])

    def test_event_time_never_becomes_delivery_time(self):
        result=handoff(self.staff,self.order,'delivery')
        self.assertEqual(result['status'],'missing')
        self.assertEqual(result['missing'],('delivery_at',))
        self.assertNotIn('delivery_at',result['fields'])
        self.assertNotIn('event_at',result['fields'])

    def test_handoff_custom_requirements_only_registered_fields(self):
        result=handoff(self.staff,self.order,'delivery',required_fields=('address','quantity'))
        self.assertEqual(result['status'],'draft')
        with self.assertRaises(CoreError):
            handoff(self.staff,self.order,'delivery',required_fields=('secret',))

    def test_handoff_cross_company_denied(self):
        with self.assertRaises(AccessDenied):
            handoff(self.other,self.order,'delivery')

    def test_webhook_defaults_disconnected(self):
        hooks=Webhooks(self.store,self.mapping,verify=self.verify)
        self.assertEqual(hooks.receive('fixture-signature',self.event()),{'status':'unavailable'})
        self.assertEqual(self.auth_calls,[])

    def test_unsigned_event_cannot_change_projection(self):
        self.assertEqual(self.hooks.receive('wrong',self.event())['status'],'denied')
        self.assertEqual(self.state()['sequence'],-1)

    def test_revoked_role_cannot_change_projection(self):
        self.current=self.reader
        self.assertEqual(self.receive()['status'],'denied')
        self.assertEqual(self.state()['sequence'],-1)

    def test_cross_company_payload_denied(self):
        self.assertEqual(self.receive(tenant='hope-sunrise')['status'],'denied')
        self.assertEqual(self.state()['sequence'],-1)

    def test_cross_company_verifier_cannot_change_projection(self):
        self.current=self.other
        self.assertEqual(self.receive()['status'],'denied')
        self.assertEqual(self.state()['sequence'],-1)

    def test_unregistered_source_denied(self):
        self.assertEqual(self.receive(source='unknown')['status'],'denied')

    def test_accept_only_observed_dimension(self):
        result=self.receive()
        self.assertEqual(result['status'],'accepted')
        state=self.state()['state']
        self.assertEqual(state['payment'],'paid')
        self.assertEqual(state['fulfillment'],'unknown')

    def test_duplicate_is_reauthenticated_and_durable(self):
        payload=self.event()
        first=self.hooks.receive('fixture-signature',payload)
        self.hooks=Webhooks(Store(self.path),self.mapping,verify=self.verify,enabled=True)
        second=self.hooks.receive('fixture-signature',payload)
        self.assertEqual(first['status'],second['status'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(len(self.auth_calls),2)
        self.assertEqual(self.state()['sequence'],1)

    def test_duplicate_after_revocation_does_not_leak_receipt(self):
        self.receive()
        self.current=self.reader
        self.assertEqual(self.receive(),{'status':'denied'})

    def test_same_event_id_changed_content_rejected(self):
        self.receive()
        with self.assertRaises(CoreError):
            self.receive(sequence=2)
        self.assertEqual(self.state()['sequence'],1)

    def test_out_of_order_cannot_regress_state(self):
        self.receive(event='later',sequence=2,statuses={'fulfillment':'DONE'})
        result=self.receive(event='earlier',sequence=1,statuses={'fulfillment':'SENT'})
        self.assertEqual(result['status'],'stale')
        self.assertEqual(self.state()['state']['fulfillment'],'delivered')

    def test_higher_sequence_regression_requires_review(self):
        self.receive(event='later',sequence=2,statuses={'fulfillment':'DONE'})
        result=self.receive(event='regress',sequence=3,statuses={'fulfillment':'SENT'})
        self.assertEqual(result['status'],'review')
        self.assertEqual(self.state()['sequence'],2)

    def test_partial_unknown_event_cannot_apply_known_dimension(self):
        result=self.receive(statuses={'payment':'PAY_OK','fulfillment':'UNRECOGNIZED'})
        self.assertEqual(result['status'],'review')
        self.assertEqual(self.state()['state']['payment'],'unknown')
        self.assertEqual(self.state()['sequence'],-1)

    def test_event_conflicts_with_existing_projection_reviewed(self):
        self.receive(event='delivered',sequence=1,statuses={'fulfillment':'DONE'})
        result=self.receive(event='cancelled',sequence=2,statuses={'cancellation':'CANCELLED'})
        self.assertEqual(result['status'],'review')
        self.assertEqual(self.state()['state']['cancellation'],'unknown')

    def test_reused_order_generation_does_not_apply(self):
        result=self.receive(generation='different-generation')
        self.assertEqual(result['status'],'review')
        self.assertEqual(self.state()['sequence'],-1)
        with self.assertRaises(CoreError):
            self.hooks.register_order(self.staff,replace(self.order,generation='new-generation'))

    def test_same_creation_generation_allows_new_mutable_version(self):
        result = self.hooks.register_order(self.staff,replace(self.order,version='v2'))
        self.assertEqual(result, {'status':'existing','generation':'generation-1'})

    def test_missing_creation_generation_cannot_register_projection(self):
        with self.assertRaises(CoreError):
            self.hooks.register_order(self.staff,replace(self.order,external_id='new-order',generation=None))

    def test_unknown_order_not_automatically_created(self):
        self.assertEqual(self.receive(order_id='missing')['status'],'unmatched')
        self.assertEqual(self.hooks.state(self.staff,'mall','missing')['status'],'not_found')

    def test_no_unverified_sequence_timestamp_fallback(self):
        for sequence in (True,-1,1.5,'2026-11-15T13:00:00+09:00',2**53):
            with self.subTest(sequence=sequence),self.assertRaises(CoreError):
                self.receive(sequence=sequence)

    def test_high_unknown_event_fences_older_known_event(self):
        self.receive(event='unknown-latest', sequence=100, statuses={'payment':'MYSTERY'})
        reply = self.receive(event='old-known', sequence=99, statuses={'payment':'PAY_OK'})
        self.assertEqual(reply['status'],'stale')
        self.assertEqual(self.state()['state']['payment'],'unknown')
        self.assertEqual(self.state()['status'],'review')
        self.assertEqual(self.state()['observed_sequence'],100)

    def test_pending_review_not_cleared_by_new_partial_observation(self):
        self.receive(event='unknown-latest', sequence=100, statuses={'payment':'MYSTERY'})
        reply = self.receive(event='other-dimension', sequence=101, statuses={'fulfillment':'DONE'})
        self.assertEqual(reply['status'],'review')
        self.assertEqual(self.state()['state']['fulfillment'],'unknown')
        self.assertEqual(self.state()['observed_sequence'],101)

    def test_unknown_relevant_dimension_prevents_eligibility(self):
        for dimension in DIMENSIONS:
            with self.subTest(dimension=dimension):
                result=assess_request(self.staff,self.order,self.observation(**{dimension:'unknown'}),self.policy,action='cancel')
                self.assertEqual(result.status,'review')
                self.assertFalse(result.can_execute)

    def test_unbound_other_company_other_order_stale_status_never_eligible(self):
        for binding in (None,('hope-sunrise','mall','13370','v1','generation-1'),
                        ('bellavita','mall','different','v1','generation-1'),
                        ('bellavita','mall','13370','stale-version','generation-1')):
            with self.subTest(binding=binding):
                observation=replace(self.observation(),binding=binding)
                result=assess_request(self.staff,self.order,observation,self.policy,action='cancel')
                self.assertEqual(result.status,'review')
                self.assertEqual(result.reasons,('status-order-binding-required',))

    def test_translation_can_bind_authenticated_order(self):
        observation=self.mapping.translate(self.staff,{'payment':'PAY_OK'},order=self.order)
        self.assertEqual(observation.binding,('bellavita','mall','13370','v1','generation-1'))
        with self.assertRaises(AccessDenied):
            self.mapping.translate(self.staff,{'payment':'PAY_OK'},order=replace(self.order,tenant='hope-sunrise'))

    def test_partial_bound_observation_returns_review(self):
        observation=replace(self.observation(),state={'production':'draft'})
        result=assess_request(self.staff,self.order,observation,self.policy,action='cancel')
        self.assertEqual(result.status,'review')
        self.assertEqual(result.reasons,('status-observation-incomplete',))

    def test_reused_id_same_version_old_generation_status_cannot_be_used(self):
        old=self.observation()
        reused=replace(self.order,generation='generation-2')
        result=assess_request(self.staff,reused,old,self.policy,action='cancel')
        self.assertEqual(result.status,'review')
        self.assertEqual(result.reasons,('status-order-binding-required',))

    def test_order_without_creation_generation_cannot_be_eligible(self):
        order=replace(self.order,generation=None)
        translated=self.mapping.translate(self.staff,{'payment':'PAY_WAIT'},order=order)
        self.assertIsNone(translated.binding)
        result=assess_request(self.staff,order,self.observation(),self.policy,action='cancel')
        self.assertEqual(result.status,'review')
        self.assertEqual(result.reasons,('status-order-binding-required',))

    def test_handoff_revalidates_mutable_fields(self):
        self.order.fields['quantity']=True
        with self.assertRaises(CoreError):
            handoff(self.staff,self.order,'production')

    def test_bad_payload_extra_fields_rejected(self):
        with self.assertRaises(CoreError):
            self.receive(url='https://external.invalid/side-effect')

    def test_registration_requires_writer_and_matching_source(self):
        with self.assertRaises(AccessDenied):
            self.hooks.register_order(self.reader,self.order)
        with self.assertRaises(AccessDenied):
            self.hooks.register_order(self.staff,replace(self.order,source='another'))

    def test_projection_state_cross_company_denied(self):
        with self.assertRaises(AccessDenied):
            self.hooks.state(self.other,'mall','13370')

    def test_concurrent_duplicate_only_one_accept(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies=list(pool.map(lambda _: self.receive(),range(12)))
        self.assertEqual(sum(not r.get('duplicate',False) for r in replies),1)
        self.assertTrue(all(r['status']=='accepted' for r in replies))
        self.assertEqual(self.state()['sequence'],1)
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM commerce_events').fetchone()[0],1)

    def test_independent_company_same_external_ids_do_not_mix(self):
        mapping=replace(self.mapping,tenant='hope-sunrise')
        hooks=Webhooks(self.store,mapping,verify=lambda credential,payload:self.other,enabled=True)
        hooks.register_order(self.other,replace(self.order,tenant='hope-sunrise'))
        self.receive()
        self.assertEqual(hooks.state(self.other,'mall','13370')['state']['payment'],'unknown')


if __name__ == '__main__':
    unittest.main()
