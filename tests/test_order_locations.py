import copy
import unittest
from unittest.mock import Mock

from order_locations import compare_locations


ROAD = '서울특별시 강남구 테헤란로 10'
ROAD_QUERY = ROAD + ' 3층 그랜드홀'
JIBUN_QUERY = '서울 강남구 역삼동 123 가상웨딩센터 3층 그랜드홀'


def evidence(query, **changes):
    result = dict(status='unique', verified=True, provider='fixture-address-provider',
                  query=query, aliases=[ROAD, '서울 강남구 역삼동 123'],
                  building_id='1168010100101230000000001', canonical_road_address=ROAD,
                  canonical_building_name='가상웨딩센터')
    result.update(changes)
    return result


class OrderLocationTests(unittest.TestCase):
    def compare(self, a=ROAD_QUERY, b=JIBUN_QUERY, resolver=evidence):
        return compare_locations(a, b, resolver=resolver)

    def test_road_jibun_alias_and_ordered_name_variations_match_real_building_and_details(self):
        for other in (JIBUN_QUERY, '그랜드홀 3층 서울 강남구 역삼동 123 가상웨딩센터',
                      '가상웨딩센터 / 서울 강남구 역삼동 123 / 그랜드 홀 / 지상 3층'):
            with self.subTest(other=other):
                self.assertEqual(self.compare(b=other)['status'], 'same')

    def test_explicit_order_details_do_not_need_text_extraction(self):
        a = dict(address=ROAD, venue='가상웨딩센터', floor='3', hall='그랜드홀')
        b = dict(address='서울 강남구 역삼동 123', venue='가상웨딩센터', floor='3F', hall='그랜드 홀')
        self.assertEqual(self.compare(a, b)['status'], 'same')

    def test_basement_notation_matches_but_above_ground_is_different(self):
        self.assertEqual(self.compare('가상센터 지하 1층 로즈홀', '가상센터 B1 로즈홀')['status'], 'same')
        self.assertEqual(self.compare('가상센터 -1층 로즈홀', '가상센터 B1 로즈홀')['status'], 'same')
        self.assertEqual(self.compare('가상센터 지하 1층 로즈홀', '가상센터 1층 로즈홀')['status'], 'different')

    def test_mixed_basement_f_notation_never_loses_basement_prefix(self):
        for notation in ('지하 3F', '지하3F', '-3F', '지하 3 F'):
            with self.subTest(notation=notation):
                self.assertEqual(self.compare('가상센터 ' + notation + ' 그랜드홀',
                                              '가상센터 3F 그랜드홀')['status'], 'different')
                self.assertEqual(self.compare('가상센터 ' + notation + ' 그랜드홀',
                                              '가상센터 B3 그랜드홀')['status'], 'same')

    def test_contradictory_floor_prefix_is_not_silently_interpreted(self):
        for notation in ('지상 B1', '지상B1층', '지상 -1F', '지상 지하1층'):
            with self.subTest(notation=notation):
                self.assertEqual(self.compare('가상센터 ' + notation + ' 그랜드홀',
                                              '가상센터 B1 그랜드홀')['status'], 'needs_review')

    def test_trailing_hall_partition_is_not_discarded_as_same_hall(self):
        for hall in ('그랜드홀', '연회장', 'Grand Hall'):
            for suffixes in ((' A', ' B'), (' 1', ' 2'), ('(A)', '(B)'), (' A구역', ' B구역')):
                with self.subTest(hall=hall, suffixes=suffixes):
                    self.assertEqual(self.compare('가상센터 3층 ' + hall + suffixes[0],
                                                  '가상센터 3층 ' + hall + suffixes[1])['status'], 'needs_review')

    def test_same_building_different_floor_hall_or_wing_is_not_same(self):
        for a, b in ((ROAD_QUERY, ROAD + ' 4층 그랜드홀'),
                     (ROAD_QUERY, ROAD + ' 3층 로즈홀'),
                     (ROAD + ' 제1관 3층 그랜드홀', ROAD + ' 제2관 3층 그랜드홀'),
                     (ROAD + ' 본관 3층 그랜드홀', ROAD + ' 별관 3층 그랜드홀')):
            with self.subTest(a=a, b=b):
                self.assertEqual(self.compare(a, b)['status'], 'different')

    def test_same_building_missing_floor_hall_or_asymmetric_wing_requires_review(self):
        for other in (ROAD, ROAD + ' 3층', ROAD + ' 그랜드홀', ROAD + ' 본관 3층 그랜드홀'):
            with self.subTest(other=other):
                self.assertEqual(self.compare(b=other)['status'], 'needs_review')

    def test_same_raw_text_without_provider_does_not_assert_real_location(self):
        self.assertEqual(compare_locations(ROAD_QUERY, ROAD_QUERY)['status'], 'needs_review')

    def test_coordinate_only_proximity_or_raw_hash_evidence_does_not_match(self):
        samples = [dict(latitude=37.5, longitude=127.0),
                   dict(status='unique', verified=True, provider='fixture', query=ROAD_QUERY,
                        aliases=[ROAD_QUERY], latitude=37.5, longitude=127.0),
                   dict(status='unique', verified=True, provider='fixture', query=ROAD_QUERY,
                        aliases=[ROAD_QUERY], address_hash='a' * 64)]
        for result in samples:
            with self.subTest(result=result):
                self.assertEqual(self.compare(resolver=lambda q: {**result, 'query': q})['status'], 'needs_review')

    def test_ambiguous_failed_falsey_or_truthy_unverified_evidence_is_not_authority(self):
        variants = [None, False, {}, [], evidence(ROAD_QUERY, status='ambiguous'),
                    evidence(ROAD_QUERY, status='failed'), evidence(ROAD_QUERY, verified=False),
                    evidence(ROAD_QUERY, verified=1), evidence(ROAD_QUERY, verified='true'),
                    evidence(ROAD_QUERY, provider=''), evidence(ROAD_QUERY, aliases=[]),
                    evidence(ROAD_QUERY, aliases=['']), evidence(ROAD_QUERY, building_id=''),
                    evidence(ROAD_QUERY, building_id=123), evidence(ROAD_QUERY, building_id='unknown'),
                    evidence(ROAD_QUERY, canonical_road_address='테스트센터'),
                    evidence(ROAD_QUERY, canonical_building_name='')]
        for result in variants:
            with self.subTest(result=result):
                self.assertEqual(self.compare(resolver=lambda _: result)['status'], 'needs_review')

    def test_query_binding_prevents_reusing_unrelated_provider_answer(self):
        self.assertEqual(self.compare(resolver=lambda q: evidence('다른 주소'))['status'], 'needs_review')

    def test_provider_exception_is_not_exposed_or_treated_as_zero_results(self):
        provider = Mock(side_effect=RuntimeError('secret address / private URL'))
        result = self.compare(resolver=provider)
        self.assertEqual(result['status'], 'needs_review')
        self.assertNotIn('secret', result['reason'])
        self.assertNotIn('private', result['reason'])

    def test_inconsistent_provider_building_id_and_address_requires_review(self):
        def inconsistent(query):
            return evidence(query, canonical_road_address=ROAD if query == ROAD_QUERY else '서울특별시 강남구 테헤란로 20')
        self.assertEqual(self.compare(resolver=inconsistent)['status'], 'needs_review')
        def conflicting_id(query):
            return evidence(query, building_id='building-1' if query == ROAD_QUERY else 'building-2')
        self.assertEqual(self.compare(resolver=conflicting_id)['status'], 'needs_review')

    def test_distinct_verified_buildings_do_not_match_even_with_identical_hall_names(self):
        def distinct(query):
            return evidence(query, building_id='building-1' if query == ROAD_QUERY else 'building-2',
                            canonical_road_address=ROAD if query == ROAD_QUERY else '서울특별시 강남구 테헤란로 20')
        self.assertEqual(self.compare(resolver=distinct)['status'], 'different')

    def test_canonical_road_only_can_match_but_provider_local_ids_are_not_cross_source_identity(self):
        self.assertEqual(self.compare(resolver=lambda q: evidence(q, building_id=None))['status'], 'same')
        def unrelated_namespaces(query):
            return evidence(query, provider='provider-a' if query == ROAD_QUERY else 'provider-b',
                            canonical_road_address=None, building_id='local-id-123')
        self.assertEqual(self.compare(resolver=unrelated_namespaces)['status'], 'needs_review')

    def test_conflicting_or_multiple_detail_values_require_review(self):
        inputs = [dict(address=ROAD_QUERY, floor='4', hall='그랜드홀'),
                  dict(address=ROAD + ' 3층 그랜드홀 4층 로즈홀'),
                  dict(address=ROAD + ' 3~4층 그랜드홀'),
                  dict(address=ROAD + ' 3·4층 그랜드홀'),
                  dict(address=ROAD, floor='', hall='그랜드홀'),
                  dict(address=ROAD, floor=True, hall='그랜드홀'),
                  dict(address=ROAD, floor='3', hall='미확인')]
        for value in inputs:
            with self.subTest(value=value):
                self.assertEqual(self.compare(a=value)['status'], 'needs_review')

    def test_named_wedding_building_is_not_an_internal_hall(self):
        query = '서울 강남구 테헤란로 10 루나웨딩홀 3층'
        self.assertEqual(self.compare(query, query, resolver=lambda q: evidence(q, canonical_building_name='루나웨딩홀'))['status'], 'needs_review')
        self.assertEqual(self.compare(query, query, resolver=lambda q: evidence(q, canonical_building_name=None))['status'], 'needs_review')

    def test_nearby_similar_spellings_do_not_create_hall_alias(self):
        self.assertEqual(self.compare('가상센터 3층 그랜드홀', '가상센터 3층 그란드홀')['status'], 'different')

    def test_invalid_or_empty_input_does_not_call_provider(self):
        for value in ('', '미확인', None, False, {'address': False}, {'venue': []}):
            provider = Mock()
            with self.subTest(value=value):
                self.assertEqual(self.compare(a=value, resolver=provider)['status'], 'needs_review')
                provider.assert_not_called()

    def test_input_and_provider_payloads_are_not_mutated(self):
        a = dict(address=ROAD, floor='3', hall='그랜드홀')
        original = copy.deepcopy(a)
        responses = []
        def provider(q):
            item = evidence(q)
            responses.append(copy.deepcopy(item))
            return item
        self.assertEqual(self.compare(a, a, resolver=provider)['status'], 'same')
        self.assertEqual(a, original)
        self.assertEqual(responses[0], responses[1])


if __name__ == '__main__':
    unittest.main()
