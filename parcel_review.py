"""Explain entries in the existing parcel exception list without changing it."""
import re

RANGES = ("'택배관리'!M6:O17", "'지명'!A1:A1")
HEADERS = ['미분류 주문번호', '배송주소', '미분류 사유']
REQUEST = re.compile(r'(?:택배(?:관리)?\s*(?:지역\s*)?(?:미분류|주소\s*확인\s*필요)|미분류\s*(?:택배|배송지))'
                     r'(?:\s*(?:목록|건수|조회|보여\s*줘|확인(?:해\s*줘)?|왜|이유|몇\s*건(?:이야)?|알려\s*줘))*[?.!]?')
REASONS = {
    '지명 시트 확인 필요': '등록된 지명으로 분류하지 못했어요',
    '주소 없음': '배송주소 확인이 필요해요',
}


def candidates(address, keywords):
    if not isinstance(address, str):
        return []
    words = re.findall(r'[가-힣]+', address)
    geographic = {'특별자치도', '특별자치시', '특별시', '광역시', '시', '군', '구', '도'}
    # Compare only complete region tokens at the start of the address.
    matches = []
    for word in words[:4]:
        trailing = next((suffix for suffix in sorted(geographic, key=lambda x: (-len(x), x))
                         if word.endswith(suffix) and len(word) > len(suffix)), '')
        key = word[:-len(trailing)] if trailing else word
        if len(key) >= 2 and key in keywords and key not in matches:
            matches.append(key)
        if not trailing and key not in keywords:
            break
    return matches[:2]


def summarize(read):
    parcel, region = read(RANGES)
    if len(parcel) < 2 or parcel[1] != HEADERS:
        raise ValueError('택배관리의 미분류 목록 제목을 확인해 주세요.')
    notice = parcel[0][0] if parcel[0] else ''
    total = re.fullmatch(r'주소\s*확인\s*필요\s*[:：]?\s*([0-9,]+)\s*건', str(notice).strip())
    if not total:
        raise ValueError('택배관리의 미분류 집계를 확인해 주세요.')
    packed = region[0][0] if region and region[0] else None
    if not isinstance(packed, str) or not packed.strip():
        raise ValueError('지명 시트의 지역 목록을 확인해 주세요.')
    keywords = {part.strip() for part in packed.split(',') if re.fullmatch(r'[가-힣]{2,}', part.strip())}
    if not keywords:
        raise ValueError('지명 시트의 지역 목록을 확인해 주세요.')
    count = int(total[1].replace(',', ''))
    if not count:
        if any(row and row[0] for row in parcel[2:]):
            raise ValueError('택배관리의 미분류 집계와 목록이 달라요. 시트에서 확인해 주세요.')
        return '택배관리에서 미분류로 표시된 주문은 없어요.'
    lines = [f'택배관리 미분류 {count}건']
    shown = 0
    for i, row in enumerate(parcel[2:], 8):
        if not row or not re.fullmatch(r'[1-9][0-9]{0,6}', str(row[0])):
            continue
        order, address, reason = (row + ['', ''])[:3]
        note = REASONS.get(reason, '택배관리에서 분류 사유를 확인해 주세요')
        picks = candidates(address, keywords)
        lines.append(f'{i}행 · 주문번호 {order} · {note}' +
                     (f' · 지명 후보 {", ".join(picks)}' if picks else ' · 확인된 지명 후보 없음'))
        shown += 1
        if shown == 3 or shown == count:
            break
    if count > shown:
        lines.append('나머지는 택배관리의 미분류 목록에서 확인해 주세요.')
    return '\n'.join(lines)
