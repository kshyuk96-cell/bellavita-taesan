"""Conservative comparison of real order venues using an injected address provider.

No network requests, geocoding, proximity matching or model calls occur here.
``resolver(query)`` is a TRUSTED server adapter: it must obtain the actual unique
address-provider response, not pass through fields supplied by a model/user/Sheet.
Merely setting ``verified=True`` does not authenticate evidence. The adapter owns
provider authentication, response verification and query binding.

Provider contract: {status: 'unique', verified: True, provider: nonempty namespace,
query: exact input query, aliases: [actual provider aliases], building_id: optional
full unique building ID, canonical_road_address: optional full canonical road
address, canonical_building_name: optional actual provider building name}.
At least a building ID or canonical road address is required. Coordinates, local
hashes and similar text are never identity evidence. Names of internal halls must
not be included in canonical_building_name. Inputs may be strings or mappings
with address/venue plus optional explicit floor/hall/wing fields. Those detail
fields are order data, never authority. Missing/ambiguous details remain review.
"""
import re
from collections.abc import Mapping


_FLOOR = re.compile(r'(?<![A-Za-z0-9])(?:(지하|지상|-)\s*)?(\d{1,3})\s*(?:층|F)(?![가-힣A-Za-z0-9])|(?<![A-Za-z0-9])B\s*(\d{1,3})(?:\s*층)?(?![A-Za-z0-9])', re.I)
_HALL = re.compile(r'(?<![가-힣A-Za-z0-9])([가-힣A-Za-z][가-힣A-Za-z0-9_-]*\s*(?:홀|연회장)|\d{1,4}\s*호|[A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*\s+Hall)(?![가-힣A-Za-z0-9])', re.I)
_WING = re.compile(r'(?<![가-힣A-Za-z0-9])(본관|별관|신관|동관|서관|남관|북관|(?:제\s*)?[A-Za-z0-9]+\s*(?:관|동))(?![가-힣A-Za-z0-9])')
_FLOOR_RANGE = re.compile(r'\d+\s*(?:~|∼|-|–|,|·|및|부터|/)\s*\d+\s*(?:층|F)', re.I)
_FLOOR_CONTRADICTION = re.compile(r'지상\s*(?:B\s*\d|지하|-\s*\d)', re.I)
_HALL_PARTITION = re.compile(r'(?:홀|연회장|hall)\s*[-(/\[]?\s*(?:[A-Za-z]|[0-9]+)(?:\s*(?:구역|실))?\s*[)\]]?(?=$|[\s,;/])', re.I)
_PLACEHOLDERS = {'0', 'none', 'null', 'unknown', 'n/a', '미확인', '확인필요', '미정'}


def _clean(value):
    return ' '.join(value.split()) if isinstance(value, str) else ''


def _useful(value, limit=2000):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 and c not in '\t\r\n' for c in value)
            and re.sub(r'\s+', '', value).casefold() not in _PLACEHOLDERS)


def _result(status, reason):
    return {'status': status, 'reason': reason}


def _input(value):
    if isinstance(value, str):
        return {'query': value, 'floor': None, 'hall': None, 'wing': None}
    if not isinstance(value, Mapping):
        raise ValueError('주소 입력 형식을 확인해 주세요.')
    for field in ('address', 'venue', 'floor', 'hall', 'wing'):
        if field in value and value[field] is not None and not isinstance(value[field], str):
            raise ValueError('주소 또는 상세 위치 형식을 확인해 주세요.')
    query = ' / '.join(dict.fromkeys(_clean(value.get(key)) for key in ('address', 'venue') if _clean(value.get(key))))
    return {'query': query, **{key: value.get(key) for key in ('floor', 'hall', 'wing')}}


def _resolve(query, resolver):
    answer = resolver(query)
    if not isinstance(answer, Mapping) or answer.get('status') != 'unique' or answer.get('verified') is not True:
        raise ValueError('주소 제공자의 유일한 검증 결과가 필요해요.')
    if answer.get('query') != query or not _useful(answer.get('provider'), 200):
        raise ValueError('조회한 주소와 연결된 제공자 근거가 필요해요.')
    aliases = answer.get('aliases')
    if not isinstance(aliases, list) or not 1 <= len(aliases) <= 100 or any(not _useful(item) for item in aliases):
        raise ValueError('주소 제공자의 실제 별칭 근거가 필요해요.')
    building_id = answer.get('building_id')
    road = answer.get('canonical_road_address')
    if building_id is not None and (not _useful(building_id, 128)
                                    or not re.fullmatch(r'[A-Za-z0-9_-]+', building_id)):
        raise ValueError('주소 제공자의 전체 건물 식별값을 확인해 주세요.')
    if road is not None and (not _useful(road) or not re.search(r'(?:로|길)\s*\d+(?:-\d+)?(?:\s|$|[,()])', road)
                             or not re.search(r'[가-힣]+(?:시|도|군|구)', road)):
        raise ValueError('전체 표준 도로명 주소를 확인해 주세요.')
    if not building_id and not road:
        raise ValueError('좌표나 장소명만으로 같은 건물을 확정할 수 없어요.')
    name = answer.get('canonical_building_name')
    if name is not None and not _useful(name):
        raise ValueError('표준 건물명을 확인해 주세요.')
    return {'provider': answer['provider'], 'building_id': building_id,
            'road': _clean(road), 'building_name': name}


def _floor_token(value):
    value = _clean(value)
    if re.fullmatch(r'-?[1-9][0-9]{0,2}', value):
        return str(int(value))
    match = _FLOOR.fullmatch(value)
    if not match:
        raise ValueError('층 표기를 하나로 확인하지 못했어요.')
    number = int(match[2] or match[3])
    if number == 0:
        raise ValueError('층 표기를 확인해 주세요.')
    return str(-number if match[1] in ('지하', '-') or match[3] else number)


def _name_token(value):
    if not _useful(value, 100):
        raise ValueError('홀·관 상세 위치를 확인해 주세요.')
    return re.sub(r'\s+', '', value).casefold()


def _details(item, resolved):
    query = item['query']
    # Exact provider building name is not an internal hall identity.
    if resolved['building_name']:
        query = query.replace(resolved['building_name'], ' ')
    if _FLOOR_RANGE.search(query) or _FLOOR_CONTRADICTION.search(query):
        raise ValueError('여러 층에 걸친 상세 위치는 확인이 필요해요.')
    if _HALL_PARTITION.search(query):
        raise ValueError('홀·연회장 안의 세부 구역을 확인해야 해요.')
    floors = {_floor_token(match[0]) for match in _FLOOR.finditer(query)}
    halls = {_name_token(match[1]) for match in _HALL.finditer(query)
             if not re.search(r'웨딩\s*홀|wedding\s*hall', match[1], re.I)}
    wings = {_name_token(match[1]) for match in _WING.finditer(query)}
    fields = {'floor': floors, 'hall': halls, 'wing': wings}
    for key, values in fields.items():
        explicit = item[key]
        if explicit is not None:
            token = _floor_token(explicit) if key == 'floor' else _name_token(explicit)
            values.add(token)
        if len(values) > 1:
            raise ValueError('한 주문에 서로 다른 층·홀·관이 있어 확인이 필요해요.')
    return {key: next(iter(values)) if values else None for key, values in fields.items()}


def compare_locations(left, right, *, resolver=None):
    """Return same/different/needs_review; never infer identity from proximity."""
    if not callable(resolver):
        return _result('needs_review', '검증된 주소 제공자 연결이 없어 실제 장소를 대조하지 못했어요.')
    try:
        first, second = _input(left), _input(right)
        if not all(_useful(item['query']) for item in (first, second)):
            return _result('needs_review', '주소 또는 설치 장소가 비어 있거나 불명확해요.')
        a, b = _resolve(first['query'], resolver), _resolve(second['query'], resolver)
        da, db = _details(first, a), _details(second, b)
    except Exception:
        # Provider exceptions may contain private URLs/addresses: never echo them.
        return _result('needs_review', '주소 조회 근거 또는 층·홀 상세 위치를 하나로 확인하지 못했어요.')
    same_provider = a['provider'] == b['provider']
    same_id = same_provider and a['building_id'] and b['building_id'] and a['building_id'] == b['building_id']
    same_road = bool(a['road'] and b['road'] and a['road'] == b['road'])
    conflicting_ids = same_provider and a['building_id'] and b['building_id'] and a['building_id'] != b['building_id']
    if (same_id and a['road'] and b['road'] and not same_road) or (same_road and conflicting_ids):
        return _result('needs_review', '건물 식별값과 표준 주소 근거가 서로 어긋나요.')
    if conflicting_ids or (a['road'] and b['road'] and not same_road and not same_id):
        return _result('different', '주소 제공자가 확인한 실제 건물이 달라요.')
    if not same_id and not same_road:
        return _result('needs_review', '서로 다른 제공자의 건물 식별값을 같은 장소로 연결할 근거가 없어요.')
    for key in ('floor', 'hall', 'wing'):
        if da[key] is not None and db[key] is not None and da[key] != db[key]:
            return _result('different', '같은 건물이지만 명시된 층·홀·관이 달라요.')
    if any(da[key] is None or db[key] is None for key in ('floor', 'hall')):
        return _result('needs_review', '같은 건물 근거는 있지만 층 또는 홀을 양쪽 모두 확인해야 해요.')
    if (da['wing'] is None) != (db['wing'] is None):
        return _result('needs_review', '본관·별관 등 상세 위치가 한쪽에만 있어 확인이 필요해요.')
    return _result('same', '검증된 실제 건물과 명시된 층·홀 상세 위치가 같아요.')
