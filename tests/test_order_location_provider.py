import contextlib
import copy
from io import StringIO
import unittest
from unittest.mock import Mock, patch

from order_location_provider import ENDPOINT, make_resolver
from order_locations import compare_locations


ROAD = '서울 강남구 테헤란로 10'
LOT = '서울 강남구 역삼동 123'


def payload(road=ROAD, lot=LOT):
    return {'meta': {'total_count': 1, 'pageable_count': 1, 'is_end': True},
            'documents': [{'address': {'address_name': lot, 'b_code': '1168010100'},
                           'road_address': {'address_name': road, 'building_name': '가상웨딩센터'}}]}


class Response:
    def __init__(self, data=None, status=200, url=ENDPOINT, history=(), content=b'{}'):
        self.data = data if data is not None else payload()
        self.status_code, self.url, self.history, self.content = status, url, history, content
        self.closed = False

    def json(self):
        return copy.deepcopy(self.data)

    def close(self):
        self.closed = True


class KakaoAddressProviderTests(unittest.TestCase):
    def test_fixed_authenticated_exact_endpoint_and_original_query_binding(self):
        response = Response()
        get = Mock(return_value=response)
        resolver = make_resolver('fixture-only', get=get)
        query = '가상웨딩센터 그랜드홀 3층 / 서울특별시 강남구 테헤란로 10'
        result = resolver(query)
        self.assertEqual(result['status'], 'unique')
        self.assertIs(result['verified'], True)
        self.assertEqual(result['query'], query)
        self.assertEqual(result['canonical_road_address'], ROAD)
        self.assertNotIn('building_id', result)
        self.assertEqual(result['aliases'], [ROAD, LOT])
        args, kwargs = get.call_args
        self.assertEqual(args, (ENDPOINT,))
        self.assertEqual(kwargs['headers'], {'Authorization': 'KakaoAK fixture-only'})
        self.assertEqual(kwargs['params'], {'query': '서울특별시 강남구 테헤란로 10',
                                          'analyze_type': 'exact', 'size': 2})
        self.assertFalse(kwargs['allow_redirects'])
        self.assertGreater(kwargs['timeout'], 0)
        self.assertLessEqual(kwargs['timeout'], 2)
        self.assertTrue(response.closed)

    def test_road_and_lot_with_reordered_building_and_hall_match_via_real_response(self):
        get = Mock(side_effect=[Response(), Response()])
        resolver = make_resolver('fixture-only', get=get)
        result = compare_locations(ROAD + ' 가상웨딩센터 3층 그랜드홀',
                                   '그랜드홀 3층 가상웨딩센터 / ' + LOT, resolver=resolver)
        self.assertEqual(result['status'], 'same')
        self.assertEqual(get.call_count, 2)

    def test_multiple_full_addresses_are_ambiguous_before_request(self):
        get = Mock()
        resolver = make_resolver('fixture-only', get=get)
        for query in (ROAD + ' / ' + LOT, ROAD + ' 또는 부산 해운대구 해운대로 20'):
            self.assertEqual(resolver(query)['status'], 'ambiguous')
        get.assert_not_called()

    def test_partial_venue_only_or_invalid_queries_never_call_provider(self):
        get = Mock()
        resolver = make_resolver('fixture-only', get=get)
        for query in ('가상웨딩센터 3층 그랜드홀', '테헤란로 10', '서울 테헤란로 10',
                      '', 'x' * 2001, None, {}, ROAD + '\x00private'):
            with self.subTest(query=str(query)[:30]):
                self.assertEqual(resolver(query)['status'], 'unresolved')
        get.assert_not_called()

    def test_nationwide_roads_and_rural_lot_do_not_require_seoul(self):
        for road, lot in (
            ('부산광역시 해운대구 해운대로 20', '부산광역시 해운대구 우동 123'),
            ('경기도 수원시 영통구 광교로 10', '경기도 수원시 영통구 이의동 123'),
            ('전북특별자치도 완주군 봉동읍 봉동로 10', '전북특별자치도 완주군 봉동읍 장기리 123'),
            ('제주특별자치도 제주시 1100로 10', '제주특별자치도 제주시 연동 123'),
            ('세종특별자치시 한누리대로 10', '세종특별자치시 조치원읍 신안리 123'),
        ):
            with self.subTest(road=road):
                get = Mock(return_value=Response(payload(road, lot)))
                resolver = make_resolver('fixture-only', get=get)
                self.assertEqual(resolver(road + ' 3층 그랜드홀')['status'], 'unique')
                self.assertEqual(resolver(lot + ' 3층 그랜드홀')['status'], 'unique')

    def test_ambiguous_meta_or_documents_not_unique(self):
        for changes in ({'total_count': 2}, {'pageable_count': 2}, {'is_end': False}):
            data = payload()
            data['meta'].update(changes)
            result = make_resolver('fixture-only', get=Mock(return_value=Response(data)))(ROAD)
            self.assertNotEqual(result['status'], 'unique')
        data = payload()
        data['documents'] *= 2
        self.assertEqual(make_resolver('fixture-only', get=Mock(return_value=Response(data)))(ROAD)['status'],
                         'ambiguous')

    def test_missing_meta_bad_types_or_missing_canonical_road_are_unresolved(self):
        samples = [None, [], {}, {'documents': []}]
        for meta in ({}, {'total_count': True, 'pageable_count': 1, 'is_end': True},
                     {'total_count': 1, 'pageable_count': 1, 'is_end': 'true'}):
            data = payload()
            data['meta'] = meta
            samples.append(data)
        for road in (None, {}, {'address_name': '가상웨딩센터'}):
            data = payload()
            data['documents'][0]['road_address'] = road
            samples.append(data)
        for data in samples:
            response = Response()
            response.data = data
            result = make_resolver('fixture-only', get=Mock(return_value=response))(ROAD)
            self.assertEqual(result['status'], 'unresolved')

    def test_unique_fuzzy_or_different_building_response_is_not_verified(self):
        get = Mock(return_value=Response(payload('서울 강남구 테헤란로 11', '서울 강남구 역삼동 124')))
        self.assertEqual(make_resolver('fixture-only', get=get)(ROAD)['status'], 'unresolved')

    def test_neighborhood_b_code_is_never_building_identity(self):
        data = payload()
        data['documents'][0]['road_address'] = None
        result = make_resolver('fixture-only', get=Mock(return_value=Response(data)))(LOT)
        self.assertEqual(result['status'], 'unresolved')
        self.assertNotIn('building_id', result)

    def test_redirect_error_status_or_foreign_response_url_are_unresolved(self):
        for response in (Response(status=302), Response(status=500),
                         Response(url='https://evil.invalid/v2/local/search/address.json'),
                         Response(url='http://dapi.kakao.com/v2/local/search/address.json'),
                         Response(history=[object()]), Response(content=b'x' * 100001)):
            get = Mock(return_value=response)
            self.assertEqual(make_resolver('fixture-only', get=get)(ROAD)['status'], 'unresolved')
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            self.assertTrue(response.closed)

    def test_timeout_and_provider_errors_do_not_leak_private_details_or_log(self):
        get = Mock(side_effect=TimeoutError('fixture-secret private address / private-url'))
        output = StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = make_resolver('fixture-only', get=get)(ROAD)
        self.assertEqual(result['status'], 'unresolved')
        self.assertNotIn('fixture-secret', str(result))
        self.assertNotIn('private-url', str(result))
        self.assertEqual(output.getvalue(), '')

    def test_per_request_cache_binds_each_query_and_is_not_mutable_authority(self):
        get = Mock(return_value=Response())
        resolver = make_resolver('fixture-only', get=get)
        one = resolver(ROAD + ' 3층 그랜드홀')
        one['aliases'].clear()
        two = resolver('그랜드홀 3층 / 서울특별시 강남구 테헤란로 10')
        self.assertEqual(two['query'], '그랜드홀 3층 / 서울특별시 강남구 테헤란로 10')
        self.assertEqual(two['aliases'], [ROAD, LOT])
        self.assertEqual(get.call_count, 1)

    def test_request_limit_and_expired_or_late_responses_are_not_success(self):
        get = Mock(return_value=Response())
        resolver = make_resolver('fixture-only', get=get, max_requests=1)
        self.assertEqual(resolver(ROAD)['status'], 'unique')
        self.assertEqual(resolver(LOT)['status'], 'unresolved')
        self.assertEqual(get.call_count, 1)
        with patch('order_location_provider.time.monotonic', side_effect=[0, 10]):
            expired = make_resolver('fixture-only', get=get, deadline_seconds=1)
            self.assertEqual(expired(ROAD)['status'], 'unresolved')
        get.reset_mock()
        with patch('order_location_provider.time.monotonic', side_effect=[0, 0, 0, 10]):
            late = make_resolver('fixture-only', get=get, deadline_seconds=1)
            self.assertEqual(late(ROAD)['status'], 'unresolved')

    def test_no_key_no_calls_and_bad_config_rejected_without_disclosure(self):
        get = Mock()
        for key in (None, '', '  '):
            self.assertIsNone(make_resolver(key, get=get))
        get.assert_not_called()
        for options in ({'max_requests': True}, {'max_requests': 0}, {'timeout': float('nan')},
                        {'timeout': 0}, {'deadline_seconds': float('inf')}, {'get': 'not callable'}):
            with self.assertRaises(ValueError) as caught:
                make_resolver('fixture-secret', **options)
            self.assertNotIn('fixture-secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
