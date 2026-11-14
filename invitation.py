"""Invitation creation shared by the Kakao bot and authenticated API."""
import calendar
import datetime as dt
import hashlib
import html
import json
import os
import re
import secrets
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from lxml import html as dom

import checkout
from blogpay import BlogPay, PRICES

SEOUL = ZoneInfo('Asia/Seoul')
HOSTS = {'bojagicard.com', 'feelcard.co.kr', 'salondeletter.com', 'toourguest.com', 'itscard.co.kr', 'itscard.com', 'mcard.itscard.co.kr', 'happyceo.kr', 'letterb.barunsoncard.com', 'makedear.com'}
WHEN = re.compile(r'(20\d\d)[년./-]\s*(\d{1,2})[월./-]\s*(\d{1,2})일?[T\s]*[ (]*(?:[월화수목금토일](?:요일)?\)?)?\s*(오전|오후)?\s*(\d{1,2})(?:\s*시\s*(?:(\d{1,2})\s*분)?|:(\d{1,2}))')
PRODUCT_LOCK = threading.Lock()


class MissingInformation(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def https_url(value):
    try:
        u = urlsplit(value)
        return u if u.scheme == 'https' and u.hostname and u.netloc == u.hostname else None
    except (ValueError, TypeError):
        return None


def get_page(url, *, hosts=HOSTS, request=None):
    u = https_url(url)
    if request is None and (not u or u.hostname.removeprefix('www.') not in hosts):
        raise ValueError('이 청첩장 주소는 아직 읽기 어려워요. 행사 이름, 날짜와 시간, 장소를 적어주세요.')
    request = request or Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    for attempt in range(2):
        try:
            with build_opener(NoRedirect()).open(request, timeout=9) as response:
                raw = response.read(2_000_001)
            break
        except (TimeoutError, URLError) as error:
            if attempt or isinstance(error, HTTPError) and error.code not in (429, 500, 502, 503, 504):
                raise
    if len(raw) > 2_000_000:
        raise ValueError('청첩장을 읽기 어려워요. 행사 이름, 날짜와 시간, 장소를 적어주세요.')
    return raw.decode('utf-8', 'replace')


def text_at(tree, path, **variables):
    matches = tree.xpath(path, **variables)
    return matches[0].text_content().strip() if matches else ''


def read_itscard_view(uid):
    """Read the vendor's public view endpoint only, never RSVP/guestbook writes."""
    if not isinstance(uid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', uid):
        raise ValueError('청첩장 식별자를 확인해 주세요.')
    url = 'https://apis.itscard.co.kr:447/mcard/view/' + uid
    request = Request(url, data=b'', headers={'User-Agent': 'Mozilla/5.0'})
    with build_opener(NoRedirect()).open(request, timeout=9) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('청첩장 정보가 너무 커서 읽지 못했어요.')
    return json.loads(raw)


def parse_time(value, required=True):
    m = WHEN.search(value if isinstance(value, str) else '')
    if not m and not required:
        return None
    if not m:
        raise ValueError('행사 날짜와 시간을 알려주세요. 예: 2026년 12월 12일 오후 2시 30분')
    year, month, day, half, hour, minute, colon_minute = m.groups()
    hour = int(hour)
    if half:
        if not 1 <= hour <= 12:
            raise ValueError('오전이나 오후 시간을 다시 확인해 주세요.')
        hour = hour % 12 + (12 if half == '오후' else 0)
    try:
        return dt.datetime(int(year), int(month), int(day), hour, int(minute or colon_minute or 0))
    except ValueError as error:
        raise ValueError('행사 날짜와 시간을 다시 확인해 주세요.') from error


def photo_urls(source, values):
    urls = []
    for value in values:
        if not value:
            continue
        url = https_url(urljoin(source, value))
        if url and url.geturl() not in urls:
            urls.append(url.geturl())
    return urls[:24]


def to_our_guest(tree):
    # Next.js streams the public invitation as escaped JSON records.
    def find(value):
        if isinstance(value, dict):
            if all(key in value for key in ('coupleInfo', 'weddingDay', 'venueInfo')):
                return value
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            return None
        return next((record for child in children if (record := find(child))), None)

    for script in tree.xpath('//script/text()'):
        match = re.fullmatch(r'self\.__next_f\.push\((.*)\)', script, re.S)
        if not match:
            continue
        chunk = json.loads(match[1])
        if len(chunk) < 2 or not isinstance(chunk[1], str):
            continue
        for line in chunk[1].splitlines():
            if '"coupleInfo"' not in line:
                continue
            try:
                record = find(json.loads(line.partition(':')[2]))
            except ValueError:
                continue
            if record:
                return record
    return {}


def itscard_record(source):
    slug = urlsplit(source).path.strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_-]{2,64}', slug):
        return {}
    # The provider's public card app uses POST to read the record by slug.
    url = f'https://apis.itscard.co.kr:447/mcard/view/{slug}'
    request = Request(url, data=b'', headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    try:
        rows = json.loads(get_page(url, request=request))
    except (OSError, ValueError):
        return {}
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}


def itscard_fields(card):
    families = []
    for side, relation_key in (('G', 'GroomDv'), ('B', 'BrideDv')):
        def name(role):
            last = field(card, side+role+'LastName', 30)
            first = field(card, side+role+'FirstName', 30)
            return (' ' if len(last) > 1 and first else '').join(filter(None, (last, first)))
        parents = [('故 ' if card.get(side+role+'late') == 'Y' else '') + name(role)
                   for role in ('Father', 'Mother') if name(role)]
        families.append(('·'.join(parents), field(card, relation_key, 24), name('')))
    clock = field(card, 'WDateTime', 40).replace('낮:', '오후 ').replace('오후:', '오후 ').replace('오전:', '오전 ')
    date = f"{field(card, 'WDate', 25)} {clock}"
    venue = ' '.join(filter(None, (field(card, 'WPlace', 100), field(card, 'WPlaceHall', 100))))
    address = ' '.join(filter(None, (field(card, 'WAddr1', 120), field(card, 'WAddr2', 120))))
    images = [card.get('MGallery'), card.get('IGallery'), *(card.get(f'SGallery{i}') for i in range(1, 21))]
    photos = ['https://platform-cdn.redprinting.co.kr/Itscard'+image for image in images
              if isinstance(image, str) and image.startswith('/userdata/')]
    return date, venue, address, families, photos


def parse_page(source, raw, extract=None):
    tree = dom.fromstring(raw)
    host = urlsplit(source).hostname.removeprefix('www.')
    meta = {m.get('property'): m.get('content', '') for m in tree.xpath('//meta[@property]')}
    if host in {'letterb.barunsoncard.com','makedear.com'}:
        from order_invitation import parse_order_page
        result=parse_order_page(source,raw)
        if extract is not None:
            return event_fields({},source,result['when'],result['venue'],result['address'],result['families'],[],True)
        result.update(photos=[],greeting='',signature='',note='',host_side='',wreath_restriction=False)
        return result
    description = meta.get('og:description', '')
    title = meta.get('og:title', '')
    cover = meta.get('og:image', '')
    families, photos = [], []
    date, venue, address = '', '', ''
    state = re.search(r'window\.__V5__\s*=\s*(\{.*?\});', raw, re.S) if host == 'bojagicard.com' else None
    if host == 'happyceo.kr':
        links = [link for link in tree.xpath('//a/@href') if https_url(link)
                 and urlsplit(link).hostname == 'mcard.itscard.co.kr']
        if len(set(links)) == 1:
            event = parse_page(links[0], get_page(links[0]), extract)
            event['source'] = source
            return event
    if host in ('mcard.itscard.co.kr', 'itscard.co.kr') and (card := itscard_record(source)):
        date, venue, address, families, photos = itscard_fields(card)
        cover = photos[0] if photos else cover
    elif state:
        card = json.loads(state[1])['invite']
        date = card['wedding']['dateRaw']
        location = card['location']
        venue = ' '.join(dict.fromkeys(filter(None, (location.get('venue') or card['wedding']['venue'], location.get('hall')))))
        address = location.get('address', '')
        photos = [p.get('src') for p in card['gallery']['photos']]
        for family in (card['greeting']['families'][s] for s in ('groom', 'bride')):
            parents = []
            for parent in (family['father'], family['mother']):
                if parent.get('name'):
                    mark = '故 ' if family.get('memorialEnabled') and parent.get('memorial') else ''
                    parents.append(mark + parent['name'])
            families.append(('·'.join(parents), family['self'].get('ord', ''), family['self']['name']))
    elif host == 'bojagicard.com':
        photos = tree.xpath('//*[@data-src_original]/@data-src_original')
        found = re.search(r"var wday = '([^']+)'", raw)
        date = found[1] if found else description
        venue = text_at(tree, '//*[contains(concat(" ",normalize-space(@class)," ")," map_venue_name ")]')
        address = text_at(tree, '//*[contains(concat(" ",normalize-space(@class)," ")," map_venue_name ")]/following-sibling::*[1]')
        for side in ('a', 'b'):
            def get(field):
                return text_at(tree, '//*[@data-mcdi_chain=$key]', key=f'insa-{side}-{field}')
            families.append(('·'.join(filter(None, (get('father-name'), get('mother-name')))),
                             text_at(tree, '//*[@data-ord_side=$side]', side=side), get('self-name')))
    elif host == 'feelcard.co.kr':
        date = description
        match = WHEN.search(description)
        venue = description[match.end():].strip().replace('엘리에나 호텔', '엘리에나호텔') if match else ''
        address = text_at(tree, '//*[@data-preview-shared="wedding_addr"]')
        photos = tree.xpath('//*[@data-full-src]/@data-full-src')
        for side in ('groom', 'bride'):
            def get(field):
                return text_at(tree, '//*[@data-preview-shared=$key and not(@hidden)]', key=f'{side}_{field}')
            families.append(('·'.join(filter(None, (get('father_name'), get('mother_name')))), get('ship'), get('name')))
    elif host == 'salondeletter.com':
        def get(field):
            return text_at(tree, '//*[@var=$key]', key=field)
        date = description
        venue = get('placeName')
        if get('hallName') and get('hallName') not in venue:
            venue = (venue + ' ' + get('hallName')).strip()
        address = get('placeAddrRoad')
        photos = [url.split('?', 1)[0] for url in tree.xpath('//*[contains(@class,"gallery")]//img/@src')]
        for side in ('groom', 'bride'):
            def name(role):
                return get(f'{side}{role}LastName') + get(f'{side}{role}FirstName')
            # The site's ornamental chrysanthemum icon alone is not a memorial flag.
            parents = []
            for role in ('Dad', 'Mom'):
                parent_name = name(role)
                marker = tree.xpath('//*[@var=$key]/../*[normalize-space(text())="故" and not(@hidden) and not(contains(translate(@style," ",""),"display:none"))]',
                                    key=f'{side}{role}LastName')
                if parent_name:
                    parents.append(('故 ' if marker else '') + parent_name)
            families.append(('·'.join(parents), get(side+'RelationName'), name('')))
    elif host == 'toourguest.com' and (record := to_our_guest(tree)):
        day = record['weddingDay']
        half = {'AM': '오전', 'PM': '오후'}.get(day['ampm'], day['ampm'])
        date = f"{day['year']}년 {day['month']}월 {day['date']}일 {half} {day['hour']}시 {day['minute']}분"
        photos = [p.get('originFile') for p in record.get('galleryBucketInfo', [])]
        couple = record['coupleInfo']
        for side in ('groom', 'bride'):
            def name(role):
                return (couple.get(f'{side}{role}LastName') or '') + (couple.get(f'{side}{role}FirstName') or '')
            parents = [('故 ' if couple.get(side + role + 'Rip') is True else '') + name(role)
                       for role in ('Dad', 'Mom') if name(role)]
            families.append(('·'.join(parents), couple.get(side+'Relation') or '', name('')))
        place = record['venueInfo']
        venue = ' '.join(filter(None, (place.get('venueName'), place.get('venueDetail'))))
        address = place.get('address', '')
    else:
        date = description
        for script in tree.xpath('//script[@type="application/ld+json"]/text()'):
            try:
                structured = json.loads(script)
            except ValueError:
                continue
            entries = structured if isinstance(structured, list) else [structured]
            entries = [child for entry in entries if isinstance(entry, dict)
                       for child in (entry.get('@graph') or [entry])]
            record = next((entry for entry in entries if isinstance(entry, dict)
                           and 'Event' in str(entry.get('@type', '')) and entry.get('startDate')), None)
            if not record:
                continue
            title = field(record, 'name', 80) or title
            date = record['startDate']
            place = record.get('location')
            if isinstance(place, dict):
                venue = field(place, 'name', 140)
                address = place.get('address', '')
                if isinstance(address, dict):
                    address = ' '.join(filter(None, (field(address, key, 100) for key in
                                                      ('addressRegion', 'addressLocality', 'streetAddress'))))
                if not isinstance(address, str):
                    address = ''
            elif isinstance(place, str):
                venue = place[:140]
            media = record.get('image')
            photos = [media] if isinstance(media, str) else media if isinstance(media, list) else []
            photos = [item for item in photos if isinstance(item, str)]
            break

    wedding = bool(families) or bool(re.search(r'결혼|웨딩|wedding', title, re.I))
    valid_families = len(families) == 2 and all(all(family) for family in families)
    when = parse_time(date, required=False)
    details = {}
    if extract and (not when or not venue or (wedding and not valid_families)):
        if wedding and not families:
            names = re.search(r'([가-힣 ]{2,30})\s*[♡♥❤&💍]+\s*([가-힣 ]{2,30})', title)
            if names:
                families = [('', '', name.strip().split('의 결혼')[0].split(' 결혼')[0]) for name in names.groups()]
        # Read only visible invitation text. Provider configuration can contain private data.
        for element in tree.xpath('//script|//style|//noscript|//*[@hidden]'):
            element.drop_tree()
        visible = ' '.join(tree.text_content().split())[:8500]
        details = extract(f'공개 초대장 제목: {title}\n공개 안내: {description}\n공개 본문: {visible}')
        when = when or parse_time(details.get('date', ''), required=False)
        venue = venue or field(details, 'venue', 140)
        address = address or field(details, 'address', 200)
        wedding = wedding or details.get('kind') == 'wedding'
        if wedding and not valid_families:
            inferred = read_families(details)
            families = [tuple(x or y for x, y in zip(current, supplemental))
                        for current, supplemental in zip(families or [('', '', '')] * 2, inferred)]
    images = photo_urls(source, [cover, *photos, *tree.xpath('//main//img/@src')])
    if wedding and photos:
        # Preserve the original full-size image for the existing checkout photo review.
        details['wedding_photo'] = next(iter(photo_urls(source, photos)), '')
    return event_fields(details, source, when, venue, address, families, images, wedding,
                        fallback_name='' if wedding else title)


def field(fields, key, limit):
    value = fields.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ''


def read_families(fields):
    return [(field(fields, f'{side}_parents', 90), field(fields, f'{side}_relation', 24),
             field(fields, f'{side}_name', 40)) for side in ('groom', 'bride')]


def event_fields(fields, source, when, venue, address, families, images, wedding, fallback_name=''):
    name = (' '.join(family[2] for family in families) + ' 결혼' if wedding and families
            else field(fields, 'name', 80) or fallback_name)
    missing = [label for value, label in ((name, '행사 이름'), (when, '날짜와 시간'), (venue, '장소')) if not value]
    if wedding:
        for side, (parents, relation, person) in zip(('신랑', '신부'), families or [('', '', '')] * 2):
            missing.extend(label for value, label in ((person, side+' 이름'), (parents, side+' 측 혼주 이름'),
                                                     (relation, side+' 측 원문 관계')) if not value)
    if missing:
        raise MissingInformation('빠진 정보만 적어주세요. ' + ', '.join(missing))
    return {'name': name, 'when': when, 'venue': venue, 'address': address or '', 'families': families,
            'source': source, 'image': next(iter(images), ''), 'photos': images,
            'wedding_photo': field(fields, 'wedding_photo', 1000),
            'greeting': field(fields, 'greeting', 600), 'signature': field(fields, 'signature', 100).removesuffix('.'),
            'note': field(fields, 'note', 500), 'host_side': field(fields, 'host_side', 8),
            'wreath_restriction': fields.get('wreath_restriction') is True}


def parse_source(source, extract):
    urls = list(dict.fromkeys(re.findall(r'https://[^\s<>"\]]+', source)))
    if len(urls) > 1:
        raise MissingInformation('청첩장이나 초대장 링크를 한 개만 보내주세요.')
    source_url = urls[0].rstrip(').,') if urls else ''
    if source_url and not https_url(source_url):
        raise MissingInformation('초대장 주소를 확인해 주세요.')
    supplement = source.replace(urls[0], '', 1).strip() if urls else source
    configured = set(filter(None, (host.strip().lower() for host in os.getenv('INVITATION_PUBLIC_HOSTS', '').split(','))))
    hosts = HOSTS | configured
    if source_url and urlsplit(source_url).hostname.removeprefix('www.') in hosts:
        try:
            event = parse_page(source_url, get_page(source_url, hosts=hosts), lambda text: extract(text + '\n' + supplement))
            if supplement:
                fields = extract(supplement)
                for key, size in (('address', 200), ('host_side', 8), ('note', 500)):
                    if value := field(fields, key, size):
                        event[key] = value
                if fields.get('wreath_restriction') is True:
                    event['wreath_restriction'] = True
            return event
        except (OSError, ValueError) as error:
            if isinstance(error, MissingInformation) and not supplement:
                raise
    if not supplement:
        raise MissingInformation('행사 이름, 날짜와 시간, 장소를 적어주세요. 예식이면 신랑신부와 혼주 이름, 원문 관계도 알려주세요.')
    fields = extract(supplement)
    wedding = fields.get('kind') == 'wedding'
    return event_fields(fields, source_url, parse_time(fields.get('date', ''), required=False), field(fields, 'venue', 140),
                        field(fields, 'address', 200), read_families(fields) if wedding else [], [], wedding)


def display(event):
    when = event['when']
    weekday = '월화수목금토일'[when.weekday()]
    date = f'{when.year}년 {when.month}월 {when.day}일({weekday})'
    clock = f'{"오전" if when.hour < 12 else "오후"} {when.hour % 12 or 12}시'
    if when.minute:
        clock += f' {when.minute}분'
    product = f"{event['name']} 축하 쌀빛그림화환 ({when.month}월 {when.day}일 {weekday} {clock} {event['venue']})"
    return date, clock, product


def content(event):
    families = event['families']
    if families:
        side = 1 if event.get('host_side') == 'bride' else 0
        parents, relation, name = families[side]
        first_name = name[1:] if len(name) == 3 else name
        particle = '이가' if (ord(first_name[-1]) - 0xac00) % 28 else '가'
        greeting = (f'사랑하는 제 {relation} {first_name}{particle} 사랑과 믿음으로 한 가정을 이루는 소중한 첫걸음을 내딛습니다.\n'
                    '두 사람의 뜻 깊은 출발을 따뜻한 마음으로 축복해 주시면 더없는 기쁨이 되겠습니다')
        return ('결혼식에 초대드립니다.', greeting, f'{parents} 올림', f'{parents}의 {relation} {name} 예식')
    return (f"{event['name']}에 초대드립니다.", event.get('greeting') or '귀한 걸음으로 함께해 주시면 감사하겠습니다.',
            event.get('signature', ''), event['name'])


def thumbnail(event, output):
    from importlib.util import find_spec
    from PIL import Image, ImageDraw, ImageFont

    canvas = Image.new('RGB', (1200, 630), '#eee6d7')
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((56, 39, 1144, 591), fill='#fffdf7', outline='#e3d4bb', width=2)
    draw.line((555, 99, 645, 99), fill='#a58a5b', width=3)
    title, _, signature, _ = content(event)
    families = event['families']
    if families:
        side = 1 if event.get('host_side') == 'bride' else 0
        _, relation, name = families[side]
        short_name = name[1:] if len(name) == 3 else name
        title = f'저희 {relation} {short_name}의\n결혼식에 초대드립니다.'
    serif = Path('/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc')
    package = find_spec('koreanize_matplotlib')
    fallback = Path(next(iter(package.submodule_search_locations))) / 'fonts' if package else None
    for value, y, size, weight in ((title, 235, 64, 'regular'), (signature, 472, 88, 'bold')):
        if not value:
            continue
        font_path = serif if serif.exists() else fallback / (
            'NanumGothicBold.ttf' if weight == 'bold' else 'NanumGothic.ttf')
        for points in range(size, 13, -2):
            font = ImageFont.truetype(str(font_path), points, index=1 if serif.exists() else 0)
            if max(draw.textlength(line, font=font) for line in value.splitlines()) <= 976:
                break
        draw.multiline_text((600, y), value, font=font, fill='#24271f', anchor='mm', align='center', spacing=19)
    canvas.save(output, format='JPEG', quality=91, optimize=True)


def kakao_copy(event, date, clock, price, url):
    _, greeting, signature, _ = content(event)
    parts = [
        greeting + (' 😊🙏' if event['families'] else ''),
        f"🗓일시: {date} {clock}\n📍장소: {event['venue']}" + (f'\n{signature} 🌸' if signature else ''),
        url,
        f'🌾 화환 안내🌾 · {price:,}원\n'
        + ('본 예식홀은 3단화환 반입 불가합니다. ' if event.get('wreath_restriction') else '')
        + '화환으로 축하의 마음을 전하고자 하시는 분께서는 쌀빛그림화환으로 함께해 주시면 감사하겠습니다.',
        '쌀빛그림화환은\n'
        + ('🖼 웨딩 축하 액자 전시 후 ' if event['families'] else '🖼 액자화환 설치 후 ')
        + '🍚 쌀로 교환하여 🤍 도움이 필요한 곳에 기부하는 🌱 친환경 · ESG 화환입니다.',
        '화환 문의 ☎ 031-8067-8580',
    ]
    return '\n\n'.join(parts)


def page(event, date, clock, order_url, preview_image, url):
    e = html.escape
    title, greeting, signature, og = content(event)
    image = event['image']
    source = event['source']
    wedding = bool(event['families'])
    label = '모바일 청첩장 보러가기' if wedding else '원본 초대장 보러가기'
    original = f'<a class="action original" href="{e(source)}">{label}</a>' if source else ''
    photo = f'<img class="portrait" src="{e(image)}" alt="{"신랑신부" if wedding else "행사"} 사진">' if image else ''
    family = ''.join(f'<p>{e(p)}{"의 " if p else ""}{e(r)} <strong>{e(n)}</strong></p>' for p,r,n in event['families'])
    note = f"<p class='note'>{e(event['note'])}</p>" if event.get('note') else ''
    letter = ''.join(f'<p>{e(line)}</p>' for line in greeting.splitlines() if line.strip())
    sign = f'<p class="signature">{e(signature)}</p>' if signature else ''
    wreath = ('쌀빛그림화환은 신랑신부를 위한 웨딩액자 설치 후 쌀로 교환하여 혼주님 명의로 '
              '도움이 필요한 곳에 쌀을 기부해드리는 친환경 기부화환입니다.' if wedding else
              '쌀빛그림화환은 설치 후 쌀로 교환하여 주최 측 명의로 도움이 필요한 곳에 기부하는 친환경 기부화환입니다.')
    asset = '/checkout-art/sunflower-easel.webp'
    samples = f'<img class="sample" src="{asset}" alt="해바라기 액자화환 설치 예시" loading="lazy">'
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>{e(title)}</title><meta property="og:type" content="website"><meta property="og:url" content="{e(url)}">
<meta property="og:title" content="{e(og)}"><meta property="og:description" content="{e(date+' '+clock)}">
<meta property="og:image" content="{e(preview_image)}"><meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630"><meta property="og:image:type" content="image/jpeg">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700&family=Noto+Serif+KR:wght@400;500;600;700&display=swap">
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#e9e5db;color:#24271f;font:20px/1.75 "Noto Sans KR",system-ui,sans-serif}}
main{{max-width:590px;margin:auto;background:#f9f7ef;box-shadow:0 0 55px #342e2418}}p,h1,h2,dd{{margin:0}}
section{{padding:38px clamp(19px,6vw,39px);text-align:center}}h1,h2,.letter,dd,.shop{{font-family:"Noto Serif KR",serif;word-break:keep-all;overflow-wrap:anywhere}}
.invitation{{background:#eee6d7;padding:clamp(20px,6vw,38px)}}.letter{{min-height:clamp(415px,93vw,510px);display:flex;flex-direction:column;text-align:left;padding:clamp(23px,6vw,42px);background:#fffdf7;border:1px solid #e3d4bb;box-shadow:0 12px 23px #57452c12}}
.letter::before{{content:"";width:41px;height:1px;background:#a58a5b;margin-bottom:23px}}h1{{font-size:clamp(24px,6vw,34px);line-height:1.3;font-weight:500;margin-bottom:22px}}
.letter p{{font-size:clamp(17px,4.7vw,22px);line-height:1.9;margin-bottom:15px}}.letter .signature{{margin:auto 0 0;padding-top:23px;text-align:right;font-size:clamp(22px,5.8vw,29px);font-weight:700}}
.portrait{{display:block;width:100%;height:auto}}h2{{font-size:clamp(29px,6.6vw,38px);font-weight:600;margin-bottom:22px}}
.family{{font-size:clamp(18px,4.8vw,23px);margin-bottom:20px;word-break:keep-all}}dl{{margin:0 auto;max-width:460px}}dl div{{padding:13px 0 18px;border-bottom:1px solid #e1dace}}
dt{{color:#555640;font-size:18px;font-weight:600}}dd{{font-size:clamp(22px,5.6vw,29px);line-height:1.55}}dd small{{display:block;font:600 clamp(17px,4.6vw,21px)/1.6 "Noto Sans KR",sans-serif;margin-top:5px}}
.clock{{display:block;white-space:nowrap}}.action{{display:flex;align-items:center;justify-content:center;min-height:64px;width:min(100%,360px);margin:17px auto 0;padding:15px 20px;border-radius:7px;background:#304737;color:white;text-decoration:none;font:700 clamp(18px,5vw,23px)/1.4 "Noto Sans KR",sans-serif}}
.original{{min-height:86px}}.shop{{display:block;color:inherit;text-decoration:none;background:#edf0e5;border-top:1px solid #e1dace}}.shop p{{font-size:clamp(16px,4.3vw,20px);line-height:1.95;margin:0 auto 13px;max-width:440px}}
.sample{{display:block;width:min(100%,206px);height:auto;margin:22px auto}}.note{{margin-top:14px;white-space:pre-line}}
a:focus-visible{{outline:3px solid #877147;outline-offset:3px}}.shop:hover .action,.action:hover{{background:#435d49}}
</style></head><body><main>
<section class="invitation"><article class="letter"><h1>{e(title)}</h1>{letter}{sign}</article></section>{photo}
<section class="details"><h2>{'예식' if wedding else '행사'} 안내</h2><div class="family">{family}</div>
<dl><div><dt>일시</dt><dd><time datetime="{event['when'].replace(tzinfo=SEOUL).isoformat()}">{e(date)}
<span class="clock">{e(clock)}</span></time></dd></div><div><dt>장소</dt><dd>{e(event['venue'])}
<small>{e(event['address'])}</small></dd></div></dl>{note}{original}</section>
<a class="shop" href="{e(order_url)}" aria-label="쌀빛그림화환 안내 및 주문하기"><section><h2>쌀빛그림화환 안내</h2>
<p>{wreath}</p><p>화환으로 축하의 마음을 전하고자 하시는 분께서는<br>쌀빛그림화환으로 함께해 주시면 감사하겠습니다.</p>
{samples}<span class="action">쌀빛그림화환 주문하기</span></section></a>
</main></body></html>'''


def find_links(product, root):
    shop, token = os.getenv('BLOGPAY_SHOP_ID'), os.getenv('BLOGPAY_API_TOKEN')
    if not shop or not token:
        raise ValueError('쇼핑몰 연결을 확인해야 해요. 관리자에게 알려주세요.')
    with PRODUCT_LOCK:
        auto_copy = os.getenv('BLOGPAY_INVITATION_AUTO_CREATE') == '1'
        template = os.getenv('BLOGPAY_INVITATION_TEMPLATE_ID', '').strip() if auto_copy else ''
        if auto_copy and not template:
            raise ValueError('블로그페이 원본 상품 번호를 설정해 주세요.')
        if template and (not template.isascii() or not template.isdecimal()):
            raise ValueError('블로그페이 원본 상품 번호를 확인해 주세요.')
        links = BlogPay(shop, token).ensure_links(product, root, int(template) if template else None)
        return links


def find_order_products(product):
    shop, token = os.getenv('BLOGPAY_SHOP_ID'), os.getenv('BLOGPAY_API_TOKEN')
    if not shop or not token:
        raise ValueError('쇼핑몰 연결을 확인해야 해요. 관리자에게 알려주세요.')
    client = BlogPay(shop, token)
    with PRODUCT_LOCK:
        rows = client.existing_products(product)
    if set(rows) != set(PRICES) or len({row['goodNum'] for row in rows.values()}) != len(PRICES):
        raise ValueError(f'아래 이름으로 복사한 상품 두 개를 등록해 주세요. 가격은 93,500원과 99,000원이에요.\n\n{product}')
    return {price: checkout.product_snapshot(client, row) for price, row in rows.items()}


def make(source, root, origin, extract, photo=None, *, direct_blogpay=False):
    if photo:
        raise ValueError('사진 첨부는 아직 연결 중이에요. 청첩장 링크나 행사 정보만 먼저 보내주세요.')
    event = parse_source(source, extract)
    when = event['when'].date()
    year, month = (when.year + 1, 1) if when.month == 12 else (when.year, when.month + 1)
    expiry = dt.date(year, month, min(when.day, calendar.monthrange(year,month)[1]))
    if dt.datetime.now(SEOUL).date() > expiry:
        raise ValueError('이 행사의 초대장 공유 기간이 지났어요.')
    if not https_url(origin):
        raise ValueError('초대장 공개 주소를 설정해야 해요. 관리자에게 알려주세요.')
    date, clock, product = display(event)
    products = None
    if not direct_blogpay and checkout.enabled():
        if not all(checkout.payment_config().values()):
            raise ValueError('초대장 결제 연결을 먼저 마쳐야 해요. 관리자에게 알려주세요.')
        if not re.search(r'[가-힣\d]+(?:대로|로|길|동|읍|면|리)\s*\d', event['address']):
            raise MissingInformation('설치 주소를 확인해 주세요. 예식장의 도로명 또는 지번 주소를 적어주세요.')
        products = find_order_products(product)
    else:
        links = find_links(product, root)
        if set(links) != set(PRICES) or len(set(links.values())) != len(PRICES):
            raise ValueError('이 행사에 연결할 93,500원과 99,000원 주문서를 모두 찾지 못했어요. 상품 이름과 가격을 확인해 주세요.')
    job = secrets.token_hex(16)
    folder = root / 'invitations' / job
    folder.mkdir(parents=True, exist_ok=False)
    thumbnail(event, folder / 'thumbnail.jpg')
    preview = f'{origin}/i/{job}/thumbnail.jpg'
    items = []
    for price in PRICES:
        url = f'{origin}/i/{job}/{price}.html'
        order_url = f'{origin}/i/{job}/{price}/order' if products else links[price]
        if products:
            snapshot = {**products[price], 'date': date, 'clock': clock, 'venue': event['venue'],
                        'address': event['address'], 'source': event['source'],
                        'event_time': event['when'].replace(tzinfo=SEOUL).isoformat()}
            if event.get('wedding_photo'):
                snapshot['wedding_photo'] = event['wedding_photo']
            if 'wedding_crop' in event:
                snapshot['wedding_crop'] = event['wedding_crop']
            (folder / f'{price}.order.json').write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        (folder / f'{price}.html').write_text(page(event, date, clock, order_url, preview, url), encoding='utf-8')
        message = kakao_copy(event, date, clock, price, url)
        items.append({'price':price,'url':url,'message':message})
    result = {'jobId':job,'status':'done','items':items,'expires':expiry.isoformat()}
    (folder / 'result.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    return result


def get_result(root, job):
    try:
        return json.loads((root / 'invitations' / job / 'result.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
