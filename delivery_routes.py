"""Read-only delivery planning and per-employee conversation state."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path


SCRIPT = Path(__file__).with_name('delivery') / 'run.js'
EPOCH = date(1899, 12, 30)
ROAD = re.compile(r'(?:서울(?:특별시)?|인천(?:광역시)?|경기(?:도)?|충남|충청남도)\s+[^(),\n]{0,55}?(?:대로|번길|로|길)\s*\d+(?:-\d+)?')
FOLLOW = re.compile(r'다시.{0,8}(?:짜|계산|돌려)|재계산|(?:10\s*(?:km|키로|킬로미터)|이동\s*시간).*(?:시간|분)|(?:[가-힣]{2,3}(?:님|은|는|을|를)?|\d+번)\s*(?:빼고|제외)|(?:\d+번|이동\s*시간).*(?:시간|분)|배송원\s*전원|(?:조건|제외)\s*초기화|모두\s*투입', re.I)
COMPARE = re.compile(r'(?:(?:아까|최근|저장한|직전)\s*)?(?:구로\s*)?배송\s*(?:루트|동선)(?:에서|랑|와|의)?\s*(?:뭐가\s*바뀌었(?:어|나요)|바뀐\s*거(?:\s*있어)?|변경(?:\s*(?:사항|비교|확인))?|차이(?:\s*(?:보여\s*줘|확인))?|비교(?:해\s*줘)?)[.!?]?')
GURO_ADDRESS = '서울 구로구 디지털로31길 62, 816호'
GURO_POINT = [37.4863155666722, 126.892484748869]
BOARD_RANGES = ("'배송관리'!A3:D", "'배송관리'!G3:G", "'배송관리'!K3:K", "'배송관리'!O3:O")


def setup(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS route_sessions (
        owner TEXT PRIMARY KEY, changed REAL NOT NULL, settings TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS route_points (
        address TEXT PRIMARY KEY, latitude REAL NOT NULL, longitude REAL NOT NULL, checked REAL NOT NULL)''')


def session(db, owner):
    with db() as conn:
        row = conn.execute('SELECT settings FROM route_sessions WHERE owner=? AND changed>?',
                           (owner, time.time() - 86400)).fetchone()
    return json.loads(row[0]) if row else None


def wants_route(message, previous):
    if re.search(r'(?:배송\s*(?:루트|동선)).*(?:버튼|기능|화면|코드).*(?:고쳐|추가|삭제|바꿔|개선)', message):
        return False
    return bool(re.search(r'배송\s*(?:루트|동선)|(?:미배정|배송원).{0,18}(?:배정해|짜줘)', message)
                or (previous and (FOLLOW.search(message) or re.fullmatch(
                    r'(?:이번|다음)?\s*(?:주\s*)?(?:토요일|일요일|주말)|(?:\d{1,2}월\s*)?\d{1,2}일|(?:\d{4}-)?\d{1,2}[-/]\d{1,2}', message))))


def requested_days(message, today):
    match = re.search(r'(?<![\d.])(?:(\d{4})\s*[-년./]\s*)?(\d{1,2})\s*[-월/]\s*(\d{1,2})(?:\s*일)?(?![\d.])', message)
    if match:
        try:
            day = date(int(match[1] or today.year), int(match[2]), int(match[3]))
        except ValueError:
            raise ValueError('배송 날짜를 다시 확인해 주세요.') from None
        if match[1] is None and day < today and today.month == 12 and day.month == 1:
            day = day.replace(year=today.year + 1)
        return [day.isoformat()]
    match = re.fullmatch(r'(\d{1,2})일', message)
    if match:
        for offset in (0, 32):
            month = today.replace(day=1) + timedelta(days=offset)
            try:
                day = month.replace(day=int(match[1]))
            except ValueError:
                continue
            if day >= today:
                return [day.isoformat()]
        raise ValueError('배송 날짜를 다시 확인해 주세요.')
    for offset, word in enumerate(('오늘', '내일', '모레')):
        if word in message:
            return [(today + timedelta(days=offset)).isoformat()]
    monday = today - timedelta(days=today.weekday())
    if re.search(r'다음\s*주|다음\s*(?:토|일)요일', message):
        monday += timedelta(days=7)
    if '주말' in message or ('토요일' in message and '일요일' in message):
        weekend = (monday + timedelta(days=n) for n in (5, 6))
        return [day.isoformat() for day in weekend if day >= today]
    for n, word in enumerate(('월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일')):
        if word in message:
            day = monday + timedelta(days=n)
            if day < today and '이번' not in message:
                day += timedelta(days=7)
            return [day.isoformat()]
    return []


def settings_for(message, previous, today):
    settings = {'days': [], 'nearGap': 120, 'delay': 0, 'excluded': [], 'skipOrders': [], 'manualTimes': {}, **(previous or {})}
    days = requested_days(message, today)
    if days:
        if days != settings['days']:
            settings.update(skipOrders=[], manualTimes={})
        settings['days'] = days
    if re.search(r'\d+\s*건.*(?:가상|시뮬레이션)|(?:가상|시뮬레이션).*\d+\s*건', message):
        raise ValueError('카톡에서는 배송관리의 주문으로 배정안을 짤 수 있어요. 가상 주문은 별도 테스트에서 계산해 주세요.')
    if re.search(r'동대문|수원\s*(?:센터|출고)|전국', message):
        raise ValueError('지금은 구로센터 출고 건의 배송루트를 계산할 수 있어요.')
    near = re.search(r'10\s*(?:km|키로|킬로미터).*?(?:(\d+)\s*시간(?:\s*(\d+)\s*분)?|(\d+)\s*분)', message, re.I)
    if near:
        settings['nearGap'] = int(near[1] or 0) * 60 + int(near[2] or near[3] or 0)
        if not 30 <= settings['nearGap'] <= 240:
            raise ValueError('10킬로미터 이내 재픽업 간격은 30분에서 4시간 사이로 말씀해 주세요.')
    delay = re.search(r'이동\s*시간.*?(\d+)\s*분\s*(?:추가|더|늘)', message)
    if delay:
        settings['delay'] = int(delay[1])
        if not 0 <= settings['delay'] <= 60:
            raise ValueError('이동시간에 더할 시간은 0분부터 60분까지로 말씀해 주세요.')
    skip = re.search(r'\b(\d{1,6})번\s*(?:주문\s*)?(?:은|는|을|를)?\s*(?:빼|제외)', message)
    manual = re.search(r'\b(\d{1,6})번.*?(?:(\d+)\s*시간(?:\s*(\d+)\s*분)?|(\d+)\s*분)', message) if not delay and not near else None
    if skip:
        settings['skipOrders'] = list(dict.fromkeys([*settings['skipOrders'], skip[1]]))
    if manual and not skip:
        minutes = int(manual[2] or 0) * 60 + int(manual[3] or manual[4] or 0)
        if not 1 <= minutes <= 720:
            raise ValueError('편도 이동시간은 1분에서 12시간 사이로 말씀해 주세요.')
        settings['manualTimes'] = {**settings['manualTimes'], manual[1]: minutes}
    if '조건 초기화' in message:
        settings.update(nearGap=120, delay=0, excluded=[], skipOrders=[], manualTimes={})
    if re.search(r'배송원\s*전원|제외\s*초기화|모두\s*투입', message):
        settings['excluded'] = []
    excluded = re.search(r'\b([가-힣]{2,3})(?:\s*(?:님|은|는|을|를|만))?\s*(?:빼고|제외)', message)
    if excluded and not skip:
        settings['excluded'] = list(dict.fromkeys([*settings['excluded'], excluded[1]]))
    if FOLLOW.search(message) and not any([near, delay, excluded, skip, manual]) and not re.search(
            r'다시\s*(?:짜|계산|돌려)|재계산|전원|초기화|모두\s*투입', message):
        raise ValueError('바꿀 조건을 한 가지씩 말씀해 주세요. 예를 들어 “이동시간 10분 추가”처럼요.')
    return settings


def value(row, index):
    return row[index] if index < len(row) else ''


def clean(value):
    return str(value).strip()


def truth(value):
    return value is True or clean(value).lower() == 'true'


def minute(value):
    if type(value) in (float, int) and 0 <= value < 1:
        return round(value * 1440)
    match = re.fullmatch(r'(?:(오전|오후)\s*)?(\d{1,2}):(\d\d)(?::\d\d)?', clean(value))
    if match and int(match[2]) < 24 and int(match[3]) < 60:
        hour = int(match[2])
        return ((hour % 12 + 12 * (match[1] == '오후')) if match[1] else hour) * 60 + int(match[3])
    return None


def index_rows(rows):
    index = {}
    for row in rows:
        key = clean(value(row, 0))
        if key:
            index.setdefault(key, []).append(row)
    return index


def point(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (ValueError, TypeError):
        return None
    return [lat, lon] if 32 <= lat <= 39 and 124 <= lon <= 132 else None


def lookup_point(address, key, deadline):
    from requests import get
    for query in dict.fromkeys([*ROAD.findall(address), address]):
        if time.monotonic() >= deadline:
            return None
        try:
            response = get('https://dapi.kakao.com/v2/local/search/address.json',
                           params={'query': query}, headers={'Authorization': 'KakaoAK ' + key}, timeout=(2, 3))
            if response.status_code != 200:
                return None
            data = response.json()
            matches = data.get('documents', [])
            if len(matches) == 1 and data.get('meta', {}).get('is_end') is not False:
                return point(matches[0].get('y'), matches[0].get('x'))
        except (OSError, ValueError):
            return None
    return None


def board_rows(view, channels, done, owners, days):
    if (view[:1] != [['주문번호', '배송일', '행사시간', '배송주소']] or channels[:1] != [['배송채널']]
            or owners[:1] != [['배송원']] or done[:1] != [['배송 완료']]):
        raise ValueError('배송관리의 열 제목을 확인해 주세요.')
    wanted = {(date.fromisoformat(day) - EPOCH).days for day in days}
    return [(i, row) for i, row in enumerate(view[1:], 1) if value(row, 1) in wanted and value(row, 0)]


def board_snapshot(selected, channels, done, owners):
    by_day = {}
    for i, row in selected:
        channel = clean(value(value(channels, i), 0))
        number = clean(value(row, 0))
        if channel not in ('구로', '구로센터') or not re.fullmatch(r'[1-9][0-9]{0,5}', number):
            continue
        day = (EPOCH + timedelta(days=row[1])).isoformat()
        entries = by_day.setdefault(day, {})
        flag = value(value(done, i), 0)
        assigned = bool(clean(value(value(owners, i), 0)))
        address = clean(value(row, 3))
        event = minute(value(row, 2))
        if number in entries or type(flag) is not bool:
            entries[number] = None
        else:
            address_key = hashlib.sha256(address.encode()).hexdigest() if address else None
            entries[number] = [event, address_key, not flag and not assigned]
    return by_day


def compare_saved(result, read):
    baseline = result.get('route_board')
    days = result.get('route_report', {})
    if baseline is None or not days:
        return '최근 배정안에는 비교할 기록이 없어요. 배송루트를 한 번 계산한 뒤 다시 확인해 주세요.'
    view, channels, done, owners = read(BOARD_RANGES)
    selected = board_rows(view, channels, done, owners, days)
    current = board_snapshot(selected, channels, done, owners)
    lines = []
    for day in days:
        before, after = baseline.get(day, {}), current.get(day, {})
        added = {number for number in after.keys() - before.keys() if after[number] and after[number][2]}
        common = before.keys() & after.keys()
        uncertain = {number for number, row in after.items() if row is None}
        uncertain.update(number for number in common if before[number] is None)
        changed = {number for number in common - uncertain if before[number][:2] != after[number][:2]}
        missing = {number for number in before.keys() - after.keys() if before[number] and before[number][2]}
        pending = {number for number, row in after.items() if row and row[2]}
        lines.append(f'{day[5:]} 구로 배송관리 · 현재 미배정 {len(pending)}건')
        for label, numbers in (('새로 보이는 미배정 번호', added), ('행사시간·장소 변경', changed),
                               ('목록에서 빠진 번호', missing), ('상태 확인 필요', uncertain)):
            if numbers:
                first = ', '.join(f'{number}번' for number in sorted(numbers, key=int)[:5])
                lines.append(f'{label} {len(numbers)}건 · {first}')
    lines.append('주문번호는 행 이동으로 바뀔 수 있어요. 새로 보이거나 빠진 번호는 원장과 대조해 주세요.')
    return '\n'.join(lines)


def workbook(read, days):
    base_ranges = [*BOARD_RANGES, "'배송비DB'!A1:V", "'배송상태DB'!A2:C", "'배송원DB'!A1:A", "'배송원DB'!E1:M",
                   "'배송센터DB'!A1:C", "'배송채널DB'!A1:D"]
    view, channels, done, owners, fees, statuses, names, workers, centers, mapping = read(base_ranges)
    if not fees or value(fees[0], 21) != '직접 입력 배송원' or names[:1] != [['배송원명']]:
        raise ValueError('배송비DB 또는 배송원DB의 열 제목을 확인해 주세요.')
    selected = board_rows(view, channels, done, owners, days)
    if len(selected) > 160:
        raise ValueError('한 번에 확인할 배송 건이 많아요. 배송 날짜를 하루씩 말씀해 주세요.')
    indices = sorted({int(row[0]) for _, row in selected if re.fullmatch(r'[1-9]\d{0,5}', clean(row[0])) and int(row[0]) >= 5})
    ranges = [f"'액자화환'!B{row}:I{row}" for row in indices]
    ledger = {str(i): rows[0] for i, rows in zip(indices, read(ranges) if ranges else []) if rows}
    origin = next((clean(value(row, 1)) for row in centers[1:]
                   if value(row, 0) in ('구로', '구로센터') and truth(value(row, 2))), '')
    if not origin:
        raise ValueError('배송센터DB에서 구로센터 주소를 확인해 주세요.')
    return channels, done, owners, fees, statuses, names, workers, mapping, selected, ledger, origin


def calculate(settings, today, now, read, db):
    days = settings['days']
    channels, done, owners, fees, statuses, names, roster, mapping, selected, ledger, origin = workbook(read, days)
    snapshot = board_snapshot(selected, channels, done, owners)
    fees = index_rows(fees[1:])
    statuses = index_rows(statuses)
    channel_center = {clean(row[0]): clean(value(row, 1)) for row in mapping[1:]
                      if row and truth(value(row, 3))}
    duplicates = Counter((clean(row[0]), row[1]) for _, row in selected)
    busy = {day: set() for day in days}
    orders = {day: [] for day in days}
    points = {origin: GURO_POINT} if origin == GURO_ADDRESS else {}
    for i, row in selected:
        order = clean(row[0])
        day = (EPOCH + timedelta(days=row[1])).isoformat()
        fee_rows = fees.get(order, [])
        fee = fee_rows[0] if len(fee_rows) == 1 else []
        shown = value(value(owners, i), 0)
        if (truth(value(value(done, i), 0)) or (fee and any(truth(value(fee, col)) for col in (13, 14)))
                or any(truth(value(status, 2)) for status in statuses.get(order, []))):
            continue
        assigned = {clean(shown), *(clean(value(f, col)) for f in fee_rows for col in (4, 21))} - {''}
        if assigned:
            busy[day].update(assigned)
            continue
        channel = clean(value(value(channels, i), 0))
        if channel not in ('구로', '구로센터') or (channel_center.get(channel) or channel) not in ('구로', '구로센터'):
            continue
        address = clean(value(row, 3))
        event = minute(value(row, 2))
        source = ledger.get(order, [])
        issue = ''
        if duplicates[order, row[1]] > 1:
            issue = '주문번호 중복 확인 필요'
        elif not fee or (value(fee, 1), clean(value(fee, 2)), clean(value(fee, 5))) != (row[1], channel, address):
            issue = '배송비DB 주문 대조 필요'
        elif clean(value(fee, 3)) not in ('', '구로', '구로센터'):
            continue
        elif (value(source, 0), minute(value(source, 1)), clean(value(source, 4)), clean(value(source, 7))) != (row[1], event, address, channel):
            issue = '주문 원장과 배송관리 대조 필요'
        elif not address or event is None or event >= 1440:
            issue = '배송지 또는 행사시간 확인 필요'
        elif clean(value(fee, 20)) == address and (coords := point(value(fee, 18), value(fee, 19))):
            points[address] = coords
        if order in settings['skipOrders']:
            issue = '이번 계산에서 제외'
        orders[day].append({'id': f'{order}:{i}', 'order': order, 'day': day, 'event': event or 0,
                            'address': address, 'qty': 1, 'origin': origin, 'center': '구로센터',
                            'wedding': bool(re.search('예식|결혼', clean(value(source, 5)))), 'issue': issue})
    active_orders = {o['order'] for items in orders.values() for o in items}
    if (set(settings['manualTimes']) | set(settings['skipOrders'])) - active_orders:
        raise ValueError('현재 배송일의 구로 미배정 목록에서 주문번호를 찾지 못했어요.')
    return ({day: plan_day(settings, day, today, now, orders[day], busy[day], names, roster, points, origin, db)
             for day in days}, snapshot)


def resolve_points(addresses, points, db):
    missing = sorted(addresses - points.keys())
    if not missing:
        return
    cutoff = time.time() - 7 * 86400
    placeholders = ','.join('?' for _ in missing)
    with db() as conn:
        cached = conn.execute(f'SELECT address,latitude,longitude FROM route_points WHERE checked>? AND address IN ({placeholders})',
                              (cutoff, *missing)).fetchall()
    points.update({address: [lat, lon] for address, lat, lon in cached})
    missing = sorted(addresses - points.keys())
    key = os.getenv('KAKAO_REST_API_KEY', '')
    if not key or not missing:
        return
    deadline = time.monotonic() + 10
    with ThreadPoolExecutor(max_workers=8) as pool:
        fresh = {address: result for address, result in zip(missing, pool.map(lambda a: lookup_point(a, key, deadline), missing)) if result}
    points.update(fresh)
    if fresh:
        with db() as conn:
            conn.execute('DELETE FROM route_points WHERE checked<=?', (cutoff,))
            conn.executemany('INSERT OR REPLACE INTO route_points VALUES (?,?,?,?)',
                             ((address, *coords, time.time()) for address, coords in fresh.items()))


def plan_day(settings, day, today, now, orders, busy, names, roster, known, origin, db):
    name_counts = Counter(clean(row[0]) for row in names[1:] if row and clean(row[0]))
    unknown = set(settings['excluded']) - set(name_counts)
    if unknown:
        raise ValueError('배송원DB에서 이름을 찾지 못했어요. 배송원 이름을 확인해 주세요.')
    workers = []
    for i, row in enumerate(names[1:], 1):
        name = clean(value(row, 0))
        data = value(roster, i)
        use = value(data, 0)
        if not name or name_counts[name] != 1 or (clean(use) and not truth(use)) or name in busy or name in settings['excluded']:
            continue
        capacity = clean(value(data, 6))
        start = value(data, 7)
        if (capacity and (not capacity.isdecimal() or not 1 <= int(capacity) <= 6)) or (start != '' and minute(start) is None):
            continue
        days_off = clean(value(data, 3) or value(data, 4))
        start_minute = max(minute(start) or 0, now if day == today.isoformat() else 0)
        rules = {'daysOff': ['일월화수목금토'.index(d[0]) for d in re.split(r'[,/\s]+', days_off)
                            if re.fullmatch(r'[일월화수목금토](?:요일)?', d)],
                 'start': start_minute, 'priorPickup': truth(value(data, 8)), 'priorDeparture': start_minute or None}
        preferred = clean(value(data, 2) or value(data, 5))
        if preferred:
            rules['preferred'] = [] if preferred == '없음' else [s.strip() for s in re.split(r'[,;\n]+', preferred) if s.strip()]
        workers.append({'name': name, 'capacity': int(capacity or 2), 'rules': rules, 'enabled': True})
    held = len(busy & set(name_counts))
    if not orders:
        return {'routes': [], 'missed': [], 'held': held}
    points = known.copy()
    valid = [o for o in orders if not o['issue']]
    if valid:
        resolve_points({origin, *(o['address'] for o in valid)}, points, db)
        if origin not in points:
            raise ValueError('구로센터 좌표를 확인하지 못했어요. 카카오 지도 주소 검색 연결을 확인해 주세요.')
    orders = [{**o, 'issue': o['issue'] or ('' if o['address'] in points else '배송지 좌표 확인 필요')} for o in orders]
    node = shutil.which('node')
    if not node:
        import nodejs_wheel as packaged_node
        node = str(Path(packaged_node.__file__).parent / 'bin' / 'node')
    manual = {o['id']: settings['manualTimes'][o['order']] for o in orders if o['order'] in settings['manualTimes']}
    payload = {'orders': orders, 'workers': workers, 'points': points, 'nearGap': settings['nearGap'], 'manualTimes': manual,
               'delay': settings['delay'], 'start': max(480, now) if day == today.isoformat() else 480}
    result = subprocess.run([node, str(SCRIPT)], input=json.dumps(payload, ensure_ascii=False), text=True,
                            capture_output=True, check=True, timeout=10)
    return {**json.loads(result.stdout), 'held': held}


def format_result(day, result, settings):
    assigned = sum(len(v['orders']) for w in result['routes'] for v in w['visits'])
    missed = result['missed']
    lines = [f'{day[5:7]}월 {day[8:]}일 구로 배송루트 · 배정안 {assigned}건 / 미배정 {len(missed)}건',
             f'10킬로미터 이내 재픽업 {settings["nearGap"]}분' + (f' · 이동 구간마다 {settings["delay"]}분 추가' if settings['delay'] else '')]
    if result['held']:
        lines.append(f'기존 일정이 있는 배송원 {result["held"]}명은 새 배정에서 보류했어요.')
    if settings['manualTimes']:
        lines.append('편도 이동시간을 입력한 주문은 첫 출발 구간에 반영했어요.')
    if not assigned and not missed:
        lines.append('확인된 구로 미배정 주문이 없어요.')
    if any(item['reason'] == '배송지 좌표 확인 필요' for item in missed) and not os.getenv('KAKAO_REST_API_KEY'):
        lines.append('주소 좌표를 추가로 확인하려면 카카오 지도 연결이 필요해요.')
    for worker in result['routes']:
        visits = [f'{"·".join(v["orders"])} {v["done"] // 60:02d}:{v["done"] % 60:02d}' + (' 재픽업' if v['repickup'] else '')
                  for v in worker['visits']]
        lines.append(f'{worker["name"]} · 최대 {worker["maxLoad"]}개\n' + ' → '.join(visits))
    if missed:
        lines += ['미배정 사유', *(f'{item["order"]}번 · {item["reason"][:54]}' for item in missed)]
    lines.append('시간은 거리 기반 설치 완료 추정이에요. 운영 시트 반영은 대표 검토 후 진행해요.')
    return lines


def run(message, owner, db, read, now):
    previous = session(db, owner)
    settings = settings_for(message, previous, now.date())
    if any(day < now.date().isoformat() for day in settings['days']):
        raise ValueError('오늘 이후 배송 날짜를 말씀해 주세요.')
    result, board = (calculate(settings, now.date(), now.hour * 60 + now.minute, read, db)
                     if settings['days'] else (None, None))
    with db() as conn:
        conn.execute('''INSERT INTO route_sessions VALUES (?, ?, ?)
            ON CONFLICT(owner) DO UPDATE SET changed=excluded.changed,settings=excluded.settings''',
                     (owner, time.time(), json.dumps(settings, ensure_ascii=False)))
    if result is None:
        return {'summary': '어느 날 구로 배송루트를 짤까요? “다음 주 토요일”처럼 말씀해 주세요.',
                'route_buttons': ['이번 주말', '다음 주말']}
    lines = [line for day, plan in result.items() for line in format_result(day, plan, settings)]
    pages, page = [], ''
    for line in (line[i:i + 800] for line in lines for i in range(0, len(line), 800)):
        if page and len(page) + len(line) > 870:
            pages.append(page)
            page = ''
        page = f'{page}\n{line}' if page else line
    if page:
        pages.append(page)
    return {'summary': pages[0], 'route_pages': pages, 'route_report': result, 'route_board': board,
            'route_buttons': ['다시 계산', '이동시간 10분 추가', '배송루트 변경 확인']}
