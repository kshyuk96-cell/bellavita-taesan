"""Rank order volumes by the installation location entered in the ledger."""
import csv
import os
import re
from read_resilience import SchemaReadError
import unicodedata
from collections import Counter
from pathlib import Path

from delivery_counts import ALL_CENTERS, CENTER, DAYS
from delivery_routes import EPOCH, value


SUBJECT = re.compile(r'장소|설치\s*일정|설치\s*건|배송지|배송\s*일정|예식장|행사장|어디|같은\s*곳')
RANK = re.compile(r'(?:가장|제일)\s*많|최다|가장 몰|제일 몰|몰린|많이 몰')
EXCLUDED = re.compile(r'개발|기능|수정|바꿔|개선|취소|삭제|입고|재고|택배|미완료|완료된|안\s*된|지난|어제|이번\s*주|다음\s*주|'
                      r'\d+\s*(?:월|일)|[월화수목금토일]요일|배송\s*루트')


def request(message):
    if not (SUBJECT.search(message) and RANK.search(message)) or EXCLUDED.search(message):
        return None
    if not re.search(r'주문|설치|배송|건수|예식장|행사장', message):
        return None
    offsets = [i for i, day in enumerate(DAYS) if day in message]
    centers = [m[1] for m in CENTER.finditer(re.sub(r'오늘|내일|모레', ' ', message))
               if m[1] not in ('전체', '모든')]
    if len(offsets) > 1 or len(centers) > 1:
        return None
    return offsets[0] if offsets else 0, centers[0] if centers else None


def venue_key(label):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', label).casefold())


def aliases():
    """Use only aliases explicitly reviewed by the operator, never fuzzy names."""
    file = os.getenv('TAESAN_VENUE_ALIASES')
    if not file:
        return {}
    results = {}
    with Path(file).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['alias', 'venue']:
            raise ValueError('설치장소 별칭 파일의 열 제목을 확인해 주세요.')
        for row in reader:
            alias = (row['alias'] or '').strip()
            venue = (row['venue'] or '').strip()
            key = venue_key(alias)
            if not key or not venue or len(venue) > 160 or (key in results and results[key] != venue):
                raise ValueError('설치장소 별칭 파일에 빈 값이나 서로 다른 장소가 연결돼 있어요.')
            results[key] = venue
    if any(venue_key(venue) in results and results[venue_key(venue)] != venue for venue in results.values()):
        raise ValueError('설치장소 별칭을 최종 행사장 이름에 바로 연결해 주세요.')
    return results


def rank(day, center, read):
    if center is not None and (not isinstance(center, str) or len(center) > 30):
        raise ValueError('어느 센터 설치를 확인할까요?')
    center_key = re.sub(r'\s+', '', center or '')
    filtered = center_key not in ALL_CENTERS
    ranges = ["'액자화환'!B4:B", "'액자화환'!F4:F"]
    if filtered:
        ranges.append("'액자화환'!I4:I")
    dates, venues, *source = read(ranges)
    if dates[:1] != [['배송일']] or venues[:1] != [['설치장소']]:
        raise SchemaReadError('원장의 배송일과 설치장소 열을 확인해 주세요.')
    if filtered:
        channels = [re.sub(r'\s+', '', str(value(row, 0))) for row in source[0]]
        if channels[:1] != ['배송채널']:
            raise SchemaReadError('원장의 배송채널 열을 확인해 주세요.')
        if not any(center_key in key for key in channels[1:]):
            raise ValueError('배송센터 이름을 확인해 주세요.')
    serial = (day - EPOCH).days
    counts = Counter()
    labels = {}
    missing = 0
    confirmed_aliases = aliases()
    for i, row in enumerate(dates[1:], 1):
        if value(row, 0) != serial or (filtered and center_key not in value(channels, i)):
            continue
        venue = value(value(venues, i), 0)
        if not isinstance(venue, str) or not venue.strip() or venue.lstrip().startswith('#'):
            missing += 1
            continue
        label = ' '.join(unicodedata.normalize('NFKC', venue).split())
        label = confirmed_aliases.get(venue_key(label), label)
        key = venue_key(label)
        labels.setdefault(key, label[:160])
        counts[key] += 1
    prefix = f'{day.month}월 {day.day}일 {center.strip() if filtered else "전체 센터"}'
    if not counts:
        if missing:
            return f'{prefix} 주문 {missing}건의 설치장소가 비어 있거나 확인이 필요해요.'
        return f'{prefix}에 등록된 설치 주문이 없어요.'
    maximum = max(counts.values())
    leaders = [labels[key] for key, n in counts.items() if n == maximum]
    extra = f' 외 {len(leaders) - 4}곳' if len(leaders) > 4 else ''
    if len(leaders) > 1:
        summary = f'{prefix} 공동 1위는 {", ".join(leaders[:4])}{extra}예요. 각 {maximum}건 들어왔어요.'
    else:
        summary = f'{prefix}에서 주문이 가장 많은 설치장소는 {leaders[0]}예요. {maximum}건 들어왔어요.'
    if missing:
        summary += f' 설치장소를 확인해야 하는 주문 {missing}건이 있어요.'
    return summary
