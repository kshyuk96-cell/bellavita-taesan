"""Actual local-code runners for a fixed, synthetic regression catalog.

These adapters call production comparison, failure classification and access-guard
functions with synthetic data. They do not call models, Sheets or address APIs.
No runner returns the expected field: replay compares actual code behavior with
the separately registered expectation. Unknown fixtures need their own reviewed
runner and are intentionally unsupported by this catalog.
"""
import failure_regressions
import order_locations
import read_resilience
import staff_read_guard


def _case(case, fixture_id, category):
    if not isinstance(case, dict) or set(case) != {'case_id', 'category', 'parameters', 'expected'}:
        raise ValueError('지원하는 가상 회귀 사례 형식을 확인해 주세요.')
    if case['case_id'] != fixture_id or case['category'] != category:
        raise ValueError('회귀 사례와 실행기가 일치하지 않아요.')
    # Reuse the registry's bounded enum/numeric validation. Its expected field is
    # validated for schema only; actual outcomes below are independently computed.
    try:
        validated = failure_regressions._validate_case({
            key: case[key] for key in ('category', 'parameters', 'expected')})
    except (TypeError, KeyError):
        raise ValueError('가상 회귀 매개변수를 확인해 주세요.') from None
    return validated['parameters']


def _floor(number, notation):
    if notation == 'korean':
        return f'지하 {abs(number)}층' if number < 0 else f'{number}층'
    if notation == 'korean_f':
        return f'지하 {abs(number)}F' if number < 0 else f'{number}F'
    if notation == 'f':
        return f'{number}F'
    if notation == 'b' and number < 0:
        return f'B{abs(number)}'
    raise ValueError('가상 층 표기를 확인해 주세요.')


def _synthetic_address_evidence(query):
    return {'status': 'unique', 'verified': True, 'provider': 'synthetic-regression-only',
            'query': query, 'aliases': ['서울특별시 강남구 가상로 10'],
            'canonical_road_address': '서울특별시 강남구 가상로 10',
            'canonical_building_name': '가상센터'}


def _location(case, fixture_id):
    params = _case(case, fixture_id, 'locations')
    left_hall = '그랜드홀 A' if params['hall_suffix'] else '그랜드홀'
    right_hall = '그랜드홀 B' if params['hall_suffix'] else '그랜드홀'
    left = f'가상센터 {_floor(params["left_floor"], params["left_notation"])} {left_hall}'
    right = f'가상센터 {_floor(params["right_floor"], params["right_notation"])} {right_hall}'
    return order_locations.compare_locations(left, right, resolver=_synthetic_address_evidence)['status']


def _revocation(case):
    params = _case(case, 'permission-after-revocation', 'grounding')
    if params['failure'] != 'permission_revoked':
        raise ValueError('이 실행기는 조회 중 권한 회수 사례만 지원해요.')
    checks = iter((True, False))
    try:
        staff_read_guard.guarded_read(lambda: {'synthetic_rows': 1}, authorized=lambda: next(checks))
    except PermissionError:
        return 'permission_denied'
    return 'needs_review'


def _timeout(case):
    params = _case(case, 'timeout-is-not-zero', 'grounding')
    if params['failure'] != 'timeout':
        raise ValueError('이 실행기는 조회 시간 초과 사례만 지원해요.')
    failure = read_resilience.classify_read_error(TimeoutError())
    return 'unavailable' if failure.category == 'transport' and failure.retryable else 'needs_review'


def build_runners():
    return {
        'basement-F-vs-above': lambda case: _location(case, 'basement-F-vs-above'),
        'hall-suffix-review': lambda case: _location(case, 'hall-suffix-review'),
        'permission-after-revocation': _revocation,
        'timeout-is-not-zero': _timeout,
    }
