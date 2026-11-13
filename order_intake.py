"""Extract confirmed order fields without treating event times as shipping instructions."""
import re
from urllib.parse import parse_qs, urlsplit

from lxml import html

import invitation

LINK = re.compile(r'https://[^\s<>"\[\]]+')
REQUEST = re.compile(r'^(?:주문\s*(?:접수|등록|입력)|(?:카톡|화환)\s*주문)\s*[:：]?\s*')
REGIONS = {
    '부산': '부산', '대구': '대구', '인천': '인천', '대전': '대전', '울산': '울산', '광주': '광주',
    '창원': '창원', '청주': '청주', '익산': '익산', '부안': '부안', '안동': '안동', '포항': '포항',
    '순천': '순천', '경주': '경주', '영주': '영주', '봉화': '봉화',
}
METRO = {'부산', '대구', '인천', '대전', '울산', '광주'}


def source(message):
    links = [match.rstrip('.,)') for match in LINK.findall(message)]
    supported = []
    for link in dict.fromkeys(links):
        parsed = invitation.https_url(link)
        if parsed and parsed.hostname.removeprefix('www.') in invitation.HOSTS | {'e-baro.co.kr'}:
            supported.append(link)
    return supported


def wants_order(message, *, explicit_only=False):
    if re.search(r'(?:기능|버튼|화면|코드).*(?:추가|바꿔|개선|고쳐|삭제)', message):
        return False
    if REQUEST.match(message):
        return True
    if explicit_only:
        return False
    urls = source(message)
    return bool(urls and re.search(r'화환.*(?:보내|부탁|주문|접수)|(?:주문|접수).*화환|(?m:^[^\n]*https://[^\s]+\s*\n[^\n]{2,140}$)', message))


def text(value):
    return str(value or '').strip()


def plain(value):
    return ' '.join(text(value).split())


def bugo(link, raw):
    tree = html.fromstring(raw)
    venue = plain(invitation.text_at(tree, '//header[contains(@class,"obituary-header")]/strong'))
    address = plain(tree.xpath('string((//*[contains(concat(" ",normalize-space(@class)," ")," btn_addr_copy ")]/@data-clipboard-text)[1])'))
    deceased = plain(invitation.text_at(tree, '(//*[@id="top-area-wrap"]//*[contains(@class,"fit-line-text")])[1]'))
    dates = []
    for item in tree.xpath('//li[contains(@class,"info_grid")]'):
        label = ''.join(invitation.text_at(item, './strong').split())
        if label in ('입관', '발인'):
            value = plain(invitation.text_at(item, './span'))
            match = re.search(r'(\d{1,2})월\s*(\d{1,2})일\s*(\d{1,2})시\s*(\d{1,2})분', value)
            if match:
                dates.append(f'{label} {int(match[1]):02d}/{int(match[2]):02d} {int(match[3]):02d}:{int(match[4]):02d}')
    if not (venue and address and re.fullmatch(r'故\s+[가-힣]{2,12}', deceased)):
        raise ValueError('부고장에서 장소와 받는 분을 확인하지 못했어요.')
    return {'event_type': '근조', 'venue': venue, 'address': address, 'recipient': deceased + '님 유가족',
            'note': ' · '.join([deceased, *dates]), 'source': link}


def wedding(link):
    event = invitation.parse_page(link, invitation.get_page(link))
    when = event['when']
    return {'event_type': '결혼', 'event_date': when.date().isoformat(), 'event_time': when.strftime('%H:%M'),
            'venue': event['venue'], 'address': event['address'],
            'recipient': ' · '.join(family[2] for family in event['families']), 'source': link}


def extract(message):
    urls = source(message)
    if len(urls) > 1 or (not urls and LINK.search(message)):
        raise ValueError('주문마다 청첩장 또는 부고장 주소 하나와 보내는 분을 적어주세요.')
    link = urls[0] if urls else ''
    if link:
        parsed = urlsplit(link)
        if parsed.hostname.removeprefix('www.') == 'e-baro.co.kr':
            if parsed.path != '/bugo.php' or not re.fullmatch(r'[A-Za-z0-9]{4,32}', parse_qs(parsed.query).get('code', [''])[0]):
                raise ValueError('부고장 주소를 다시 확인해 주세요.')
            result = bugo(link, invitation.get_page(link, hosts={'e-baro.co.kr'}))
        else:
            result = wedding(link)
    else:
        result = {'event_type': '', 'venue': '', 'address': '', 'recipient': '', 'source': ''}
    details = REQUEST.sub('', message).replace(link, '').strip()
    labels = {label: value.strip() for label, value in re.findall(
        r'(?m)^[ \t]*(보내시는 분|보내는 분|받는 분|배송[ \t]*일|배송[ \t]*(?:시간|시각)|작품|규격|축하문구|축하 문구|행사 구분|설치장소|설치 장소|주소)[ \t]*[:：][ \t]*([^\n]+)$', details)}
    if not link:
        result.update(event_type=labels.get('행사 구분', ''), venue=labels.get('설치장소') or labels.get('설치 장소', ''),
                      address=labels.get('주소', ''), recipient=labels.get('받는 분', ''))
        if not (result['venue'] or result['address']):
            raise ValueError('청첩장·부고장 주소나 설치 장소를 적어주세요.')
    sender = labels.get('보내시는 분') or labels.get('보내는 분')
    if not sender:
        lines = [line.strip() for line in details.splitlines() if line.strip() and ':' not in line
                 and not re.search(r'화환.*(?:부탁|보내|주세요)|^(?:신랑|신부)\s*측', line)]
        if len(lines) == 1 and re.search('[가-힣A-Za-z]', lines[0]):
            sender = lines[0]
    if not sender or len(sender) > 140:
        raise ValueError('보내시는 분의 이름과 직함을 한 줄로 적어주세요.')
    side = re.search(r'(신랑|신부)\s*측', details) if result['event_type'] == '결혼' else None
    delivery_fields = {re.sub(r'[ \t]+', '', label): value for label, value in labels.items() if label.startswith('배송')}
    result.update(sender=sender, recipient=labels.get('받는 분') or (result['recipient'] + (' ' + side[1] + '측' if side else '')),
                  delivery_date=delivery_fields.get('배송일', ''),
                  delivery_time=delivery_fields.get('배송시간') or delivery_fields.get('배송시각', ''), artwork=labels.get('작품', ''),
                  size=labels.get('규격', ''), message=labels.get('축하문구') or labels.get('축하 문구', ''))
    return result


def region(address):
    words = plain(address).split()
    if len(words) < 2:
        return ''
    province = re.sub(r'(?:특별자치도|광역시|특별시|특별자치시|도)$', '', words[0])
    if province in METRO and re.fullmatch(r'[가-힣]+[구군]', words[1]):
        return '' if province == '광주' and words[0] == '광주' and words[1] not in {'동구','서구','남구','북구','광산구'} else province
    if province in {'경남','경북','충남','충북','전남','전북','경상남','경상북','충청남','충청북','전라남','전라북'}:
        return REGIONS.get(words[1].removesuffix('시').removesuffix('군'), '')
    return ''


def report(number, details):
    display = lambda key: details.get(key) or '확인 필요'
    day = details.get('delivery_date')
    clock = details.get('delivery_time')
    delivery = f'{day[5:].replace("-", ".")} {clock}' if day and clock else '확인 필요'
    address = ' / '.join(filter(None, (details.get('venue'), details.get('address'))))
    title = f'{details["channel"]}배송' if details.get('channel') else '배송채널 확인 필요'
    lines = [f'주문번호 {number}', f'{title} <{display("event_type")}>', '', f'배송 일시: {delivery}',
             f'주문작품: {display("artwork")}', f'상품 구분: {display("size")}', f'배송 장소: {address or "확인 필요"}',
             f'받는 분: {display("recipient")}', f'보내시는 분: {display("sender")}', '',
             f'문구 : {display("message")}', f'사진 보낼 곳 : {display("photo_phone")}']
    if delivery != '확인 필요':
        lines.extend(('', f'[{delivery[:5]} {clock[:2]}시{clock[3:]}분], [배송지= {address}, {display("recipient")}], [보내는분= {display("sender")}]'))
    return '\n'.join(lines)
