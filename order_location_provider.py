"""Query-bound, conservative Kakao address evidence for order_locations.

Only the fixed address-search API is called. No keyword/coordinate/fuzzy place
search is used. Create a new resolver per business request: its cache, request
count and monotonic deadline are deliberately not shared between employees.
The server supplies its authorized key; Sheets/model input cannot supply keys
or verified results. This module never logs or persists queries or API errors.
"""
import copy
import math
import re
import time
import unicodedata
from urllib.parse import urlsplit


ENDPOINT = 'https://dapi.kakao.com/v2/local/search/address.json'
PROVIDER = 'kakao-address-v2'
_PROVINCES = (
    ('서울특별시', '서울시', '서울'), ('부산광역시', '부산시', '부산'),
    ('대구광역시', '대구시', '대구'), ('인천광역시', '인천시', '인천'),
    ('광주광역시', '광주시', '광주'), ('대전광역시', '대전시', '대전'),
    ('울산광역시', '울산시', '울산'), ('세종특별자치시', '세종시', '세종'),
    ('경기도', '경기'), ('강원특별자치도', '강원도', '강원'),
    ('충청북도', '충북'), ('충청남도', '충남'),
    ('전북특별자치도', '전라북도', '전북'), ('전라남도', '전남'),
    ('경상북도', '경북'), ('경상남도', '경남'),
    ('제주특별자치도', '제주도', '제주'),
)
_NAMES = {alias: names[0] for names in _PROVINCES for alias in names}
_PROVINCE = '(?:' + '|'.join(sorted(_NAMES, key=len, reverse=True)) + ')'
_START = re.compile(r'(?<![가-힣A-Za-z0-9])' + _PROVINCE + r'(?=\s)')
_ADMIN = r'[가-힣0-9]+(?:시|군|구|읍|면|동|리)'
_NUMBER = r'(?:산\s*)?[1-9][0-9]*(?:\s*-\s*[1-9][0-9]*)?'
_ROAD_NUMBER = r'[1-9][0-9]*(?:\s*-\s*[1-9][0-9]*)?'
_END = r'(?=$|[\s,/()])'
_PREFIX = r'(?<![가-힣A-Za-z0-9])' + _PROVINCE + r'\s+'
_ROAD = re.compile(_PREFIX + r'(?:' + _ADMIN + r'\s+){1,4}'
                   + r'[가-힣A-Za-z0-9·.]+(?:대로|로|길)\s+' + _ROAD_NUMBER + _END)
_SEJONG_ROAD = re.compile(r'(?<![가-힣A-Za-z0-9])(?:세종특별자치시|세종시|세종)\s+'
                          + r'[가-힣A-Za-z0-9·.]+(?:대로|로|길)\s+' + _ROAD_NUMBER + _END)
_LOT = re.compile(_PREFIX + r'(?:' + _ADMIN + r'\s+){1,4}'
                  + r'[가-힣0-9]+(?:동|리|가)\s+' + _NUMBER + _END)


def _text(value, limit=2000):
    return (isinstance(value, str) and 0 < len(value.strip()) <= limit and len(value) <= limit
            and not any(ord(c) < 32 for c in value))


def _normalize(value):
    value = unicodedata.normalize('NFKC', value)
    words = value.split()
    if words:
        words[0] = _NAMES.get(words[0], words[0])
    return re.sub(r'\s+', '', ' '.join(words)).casefold()


def _span(query):
    """Return one complete road/lot address, never a venue-only partial query."""
    starts = list(_START.finditer(query))
    if len(starts) > 1:
        return None, 'ambiguous'
    found = {}
    for pattern in (_ROAD, _SEJONG_ROAD, _LOT):
        for match in pattern.finditer(query):
            found[(match.start(), match.end())] = match.group()
    if len(found) != 1:
        return None, 'ambiguous' if found else 'unresolved'
    return next(iter(found.values())), None


def _failure(query, status='unresolved'):
    return {'status': status, 'verified': False, 'provider': PROVIDER, 'query': query,
            'reason': '주소 제공자의 유일한 실제 건물 근거를 확인하지 못했어요.'}


def _evidence(payload, searched):
    if not isinstance(payload, dict):
        return None, 'unresolved'
    meta, documents = payload.get('meta'), payload.get('documents')
    if not isinstance(meta, dict) or not isinstance(documents, list):
        return None, 'unresolved'
    if (type(meta.get('total_count')) is not int or type(meta.get('pageable_count')) is not int
            or type(meta.get('is_end')) is not bool):
        return None, 'unresolved'
    if meta['total_count'] > 1 or meta['pageable_count'] > 1 or len(documents) > 1:
        return None, 'ambiguous'
    if meta['total_count'] != 1 or meta['pageable_count'] != 1 or not meta['is_end'] or len(documents) != 1:
        return None, 'unresolved'
    document = documents[0]
    if not isinstance(document, dict):
        return None, 'unresolved'
    road, lot = document.get('road_address'), document.get('address')
    if not isinstance(road, dict) or not _text(road.get('address_name'), 500):
        return None, 'unresolved'
    canonical = road['address_name']
    if not (_ROAD.fullmatch(canonical) or _SEJONG_ROAD.fullmatch(canonical)):
        return None, 'unresolved'
    aliases = [canonical]
    if isinstance(lot, dict) and _text(lot.get('address_name'), 500) and _LOT.fullmatch(lot['address_name']):
        aliases.append(lot['address_name'])
    # A search API can return a unique approximate suggestion. Require an actual
    # returned full-address alias to exactly match the extracted address, allowing
    # only province aliases and whitespace, never a different building number.
    if _normalize(searched) not in {_normalize(alias) for alias in aliases}:
        return None, 'unresolved'
    result = {'status': 'unique', 'verified': True, 'provider': PROVIDER,
              'aliases': list(dict.fromkeys(aliases)), 'canonical_road_address': canonical}
    # Kakao address.b_code is a legal-neighborhood code, NOT building identity.
    if _text(road.get('building_name'), 200):
        result['canonical_building_name'] = road['building_name']
    return result, None


def make_resolver(key, *, get=None, max_requests=6, timeout=2.0, deadline_seconds=6.0):
    """Return a per-request resolver, or None when no API key is configured.

    Injected get follows requests.get's interface. Timeout bounds each socket
    operation; the monotonic budget refuses additional requests and late results.
    No claims about address resolution are made after a timeout or deadline.
    """
    if key is None or (isinstance(key, str) and not key.strip()):
        return None
    if (not _text(key, 1024) or any(c.isspace() for c in key)
            or type(max_requests) is not int or not 1 <= max_requests <= 40
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10
            or type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds)
            or not 0 < deadline_seconds <= 30):
        raise ValueError('주소 제공자 연결 설정을 확인해 주세요.')
    if get is not None and not callable(get):
        raise ValueError('주소 제공자 연결 설정을 확인해 주세요.')
    expires = time.monotonic() + deadline_seconds
    calls = 0
    cache = {}

    def resolve(query):
        nonlocal calls
        if not _text(query):
            return _failure(query if isinstance(query, str) and len(query) <= 2000 else '')
        searched, status = _span(query)
        if status:
            return _failure(query, status)
        cache_key = _normalize(searched)
        if time.monotonic() >= expires:
            return _failure(query)
        if cache_key in cache:
            return {**copy.deepcopy(cache[cache_key]), 'query': query}
        if calls >= max_requests:
            return _failure(query)
        calls += 1
        response = None
        result = _failure(query)
        try:
            request_get = get
            if request_get is None:
                import requests
                request_get = requests.get
            remaining = expires - time.monotonic()
            if remaining <= 0:
                return result
            response = request_get(ENDPOINT, params={'query': searched, 'analyze_type': 'exact', 'size': 2},
                                   headers={'Authorization': 'KakaoAK ' + key},
                                   timeout=min(timeout, remaining / 2), allow_redirects=False)
            if response.status_code != 200 or getattr(response, 'history', None):
                return result
            url = getattr(response, 'url', ENDPOINT)
            target = urlsplit(url)
            if (target.scheme != 'https' or target.netloc != 'dapi.kakao.com'
                    or target.path != '/v2/local/search/address.json' or target.fragment):
                return result
            content = getattr(response, 'content', b'')
            if not isinstance(content, bytes) or len(content) > 100_000:
                return result
            evidence, failure = _evidence(response.json(), searched)
            if time.monotonic() >= expires:
                return result
            result = {**evidence, 'query': query} if evidence else _failure(query, failure)
        except Exception:
            # Exceptions may contain the key, address, request URL or response.
            result = _failure(query)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
        cache[cache_key] = {k: v for k, v in result.items() if k != 'query'}
        return copy.deepcopy(result)

    return resolve
