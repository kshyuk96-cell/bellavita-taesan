from dataclasses import asdict, replace
import json
import unittest
from taesan_core import AccessDenied, CoreError, Evidence, Principal
from taesan_core.commerce import Order
from taesan_core.order_duplicates import DuplicateReviewer, Location


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.user = Principal('bellavita', 'employee', frozenset({'reader'}))
        self.calls = []
        self.locations = {}
        self.reviewer = DuplicateReviewer(self.resolve)

    def order(self, id='13370', **fields):
        values = {'event_at': '2026-11-16T14:00:00+09:00',
                  'address': '가상로 1', 'venue_detail': '본관 2층 청실', 'sender_key': 'sender-1'}
        values.update(fields)
        return Order('bellavita', 'shop', id, 'v1', values)

    def location(self, **values):
        args = {'status': 'resolved', 'tenant': 'bellavita', 'building_id': 'building-1',
                'floor': 'floor-2', 'hall': 'hall-blue', 'wing': 'main',
                'evidence': Evidence('fixture-address', 'receipt-1', 100)}
        args.update(values)
        return Location(**args)

    def resolve(self, principal, address, detail):
        self.calls.append((principal, address, detail))
        value = self.locations.get(address, self.location())
        if isinstance(value, Exception):
            raise value
        return value

    def review(self, candidate=None, order=None):
        return self.reviewer.review(self.user, order or self.order(),
                                    [candidate or self.order('13371')])

    def test_conflicting_creation_generations_require_review(self):
        first=replace(self.order('13371'),generation='creation-1')
        second=replace(first,generation='creation-2')
        result=self.reviewer.review(self.user,self.order(),[first,second])
        self.assertEqual(result.status,'review')
        self.assertEqual(result.findings[0].reason,'conflicting-candidate-versions')
        self.assertEqual(self.calls,[])

    def test_same_verified_place_suspected(self):
        result = self.review()
        self.assertEqual(result.status, 'suspected')
        self.assertEqual(result.findings[0].external_id, '13371')

    def test_road_lot_and_building_first_can_resolve_same_place(self):
        for address in ('가상동 15-1', '가상웨딩홀 가상로 1', '가상로 1 가상웨딩홀'):
            with self.subTest(address=address):
                self.assertEqual(self.review(self.order('13371', address=address)).status, 'suspected')

    def test_identical_address_different_building_not_match(self):
        counter = [0]
        def resolve(principal, address, detail):
            counter[0] += 1
            return self.location(building_id=f'building-{counter[0]}')
        self.reviewer = DuplicateReviewer(resolve)
        self.assertEqual(self.review().status, 'clear')

    def test_different_hall_floor_wing_not_match(self):
        for field in ('hall', 'floor', 'wing'):
            with self.subTest(field=field):
                self.locations['other'] = self.location(**{field:'different'})
                result = self.review(self.order('13371', address='other'))
                self.assertEqual(result.status, 'clear')
                self.assertEqual(result.findings[0].reason, 'different-location-detail')

    def test_known_detail_difference_even_with_another_unknown_dimension(self):
        self.locations['other'] = self.location(hall='red', wing=None)
        self.assertEqual(self.review(self.order('13371',address='other')).status,'clear')

    def test_missing_detail_requires_review(self):
        for key in ('hall','floor','wing'):
            with self.subTest(key=key):
                self.locations['other'] = self.location(**{key:None})
                result = self.review(self.order('13371', address='other'))
                self.assertEqual(result.status, 'review')
                self.assertEqual(result.findings[0].reason, 'missing-location-detail')

    def test_explicit_not_applicable_dimension_can_match(self):
        self.locations['가상로 1'] = self.location(wing='not-applicable')
        self.assertEqual(self.review().status, 'suspected')

    def test_ambiguous_result_cannot_pick_first_candidate(self):
        self.locations['other'] = Location('ambiguous','bellavita')
        result = self.review(self.order('13371',address='other'))
        self.assertEqual(result.status,'review')
        self.assertEqual(result.findings[0].reason,'location-ambiguous')

    def test_unavailable_location_not_clear(self):
        self.locations['other'] = Location('unavailable','bellavita')
        self.assertEqual(self.review(self.order('13371',address='other')).status,'review')

    def test_missing_resolver_not_clear(self):
        self.reviewer = DuplicateReviewer()
        self.assertEqual(self.review().status,'review')

    def test_exception_not_leaked_as_no_duplicate(self):
        self.locations['other'] = RuntimeError('sensitive provider response')
        result = self.review(self.order('13371',address='other'))
        self.assertEqual(result.status,'review')
        self.assertNotIn('sensitive',repr(result))

    def test_malformed_provider_not_clear(self):
        self.locations['other'] = {'status':'resolved','building_id':'building-1'}
        self.assertEqual(self.review(self.order('13371',address='other')).status,'review')

    def test_cross_company_location_is_not_trusted(self):
        self.locations['other'] = self.location(tenant='hope-sunrise')
        self.assertEqual(self.review(self.order('13371',address='other')).status,'review')

    def test_timezone_equivalent_instants_match(self):
        self.assertEqual(self.review(self.order('13371',event_at='2026-11-16T05:00:00+00:00')).status,'suspected')

    def test_different_time_not_match_and_no_resolver_call(self):
        self.assertEqual(self.review(self.order('13371',event_at='2026-11-16T14:01:00+09:00')).status,'clear')
        self.assertEqual(self.calls,[])

    def test_different_event_date_not_match(self):
        self.assertEqual(self.review(self.order('13371',event_at='2026-11-17T14:00:00+09:00')).status,'clear')

    def test_missing_event_time_requires_review(self):
        values = dict(self.order('13371').fields)
        values.pop('event_at')
        candidate = Order('bellavita','shop','13371','v1',values)
        self.assertEqual(self.review(candidate).status,'review')
        self.assertEqual(self.calls,[])

    def test_missing_sender_key_no_name_fallback(self):
        values = dict(self.order('13371',sender_name='가상 고객').fields)
        values.pop('sender_key')
        candidate = Order('bellavita','shop','13371','v1',values)
        result = self.review(candidate)
        self.assertEqual(result.status,'review')
        self.assertEqual(result.findings[0].reason,'missing-authoritative-sender-key')
        self.assertEqual(self.calls,[])

    def test_same_sender_name_different_key_not_match(self):
        result = self.review(self.order('13371',sender_name='가상 고객',sender_key='sender-2'),
                             self.order(sender_name='가상 고객'))
        self.assertEqual(result.status,'clear')
        self.assertEqual(self.calls,[])

    def test_key_identity_not_display_name(self):
        self.assertEqual(self.review(self.order('13371',sender_name='다른 표시명'),
                                     self.order(sender_name='원 표시명')).status,'suspected')

    def test_missing_address_requires_review(self):
        values = dict(self.order('13371').fields)
        values.pop('address')
        self.assertEqual(self.review(Order('bellavita','shop','13371','v1',values)).status,'review')

    def test_cross_company_candidate_rejected_before_any_resolver(self):
        other = replace(self.order('private'),tenant='hope-sunrise')
        with self.assertRaises(AccessDenied):
            self.reviewer.review(self.user,self.order(),[self.order('13371'),other])
        self.assertEqual(self.calls,[])

    def test_cross_company_subject_rejected_before_resolver(self):
        with self.assertRaises(AccessDenied):
            self.reviewer.review(replace(self.user,tenant='hope-sunrise'),self.order(),[])
        self.assertEqual(self.calls,[])

    def test_no_principal_rejected(self):
        with self.assertRaises(AccessDenied):
            self.reviewer.review(None,self.order(),[])

    def test_same_source_external_id_skipped_even_different_version(self):
        candidate = replace(self.order(),version='v2')
        result = self.review(candidate)
        self.assertEqual(result.findings,())
        self.assertEqual(self.calls,[])

    def test_same_external_id_different_source_is_candidate(self):
        candidate = replace(self.order(),source='erp')
        self.assertEqual(self.review(candidate).status,'suspected')

    def test_empty_candidates_is_clear_without_resolver(self):
        self.assertEqual(self.reviewer.review(self.user,self.order(),[]).status,'clear')
        self.assertEqual(self.calls,[])

    def test_same_candidate_snapshot_deduplicates(self):
        candidate = self.order('13371')
        result = self.reviewer.review(self.user,self.order(),[candidate,candidate])
        self.assertEqual(len(result.findings),1)
        self.assertEqual(result.status,'suspected')

    def test_conflicting_versions_require_review(self):
        a = self.order('13371')
        result = self.reviewer.review(self.user,self.order(),[a,replace(a,version='v2')])
        self.assertEqual(result.status,'review')
        self.assertEqual(result.findings[0].reason,'conflicting-candidate-versions')
        self.assertEqual(self.calls,[])

    def test_same_version_conflicting_fields_require_review(self):
        result = self.reviewer.review(self.user,self.order(),[
            self.order('13371'),self.order('13371',sender_key='different')])
        self.assertEqual(result.status,'review')

    def test_match_does_not_hide_other_review(self):
        self.locations['other'] = Location('ambiguous','bellavita')
        result = self.reviewer.review(self.user,self.order(),[
            self.order('13371'),self.order('13372',address='other')])
        self.assertEqual(result.status,'suspected')
        self.assertEqual({f.decision for f in result.findings},{'suspected','review'})

    def test_only_references_and_reasons_returned(self):
        result = self.review(self.order('13371',sender_name='개인정보 표시명',message='개인정보 문구'))
        serialized = json.dumps(asdict(result),ensure_ascii=False)
        for value in ('개인정보','가상로','sender-1','2026-11-16'):
            self.assertNotIn(value,serialized)

    def test_resolver_target_is_evaluated_once_for_candidates(self):
        self.reviewer.review(self.user,self.order(),[self.order('a',address='A'),self.order('b',address='B')])
        self.assertEqual(sum(address == '가상로 1' for _,address,_ in self.calls),1)

    def test_source_orders_not_mutated(self):
        a,b = self.order(),self.order('13371')
        before = (asdict(a),asdict(b))
        self.review(b,a)
        self.assertEqual((asdict(a),asdict(b)),before)

    def test_oversized_or_generator_candidate_batch_rejected(self):
        for candidates in ([self.order('x')]*1001,iter([])):
            with self.assertRaises(CoreError):
                self.reviewer.review(self.user,self.order(),candidates)

    def test_malformed_candidate_rejected_before_resolution(self):
        with self.assertRaises(CoreError):
            self.reviewer.review(self.user,self.order(),[self.order('x'),{}])
        self.assertEqual(self.calls,[])

    def test_altered_order_fields_revalidated(self):
        candidate = self.order('13371')
        candidate.fields['event_at'] = 'local time without timezone'
        with self.assertRaises(CoreError):
            self.review(candidate)
        self.assertEqual(self.calls,[])

    def test_location_requires_verified_receipt(self):
        with self.assertRaises(CoreError):
            self.location(evidence=None)

    def test_ambiguous_location_cannot_claim_identity(self):
        with self.assertRaises(CoreError):
            self.location(status='ambiguous')

    def test_location_invalid_dimension_rejected(self):
        for value in ('',True,1,'x'*151):
            with self.subTest(value=value), self.assertRaises(CoreError):
                self.location(hall=value)

    def test_resolver_configuration_must_be_callable(self):
        with self.assertRaises(CoreError):
            DuplicateReviewer('https://provider.invalid')
