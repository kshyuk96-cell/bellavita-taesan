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
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from lxml import html as dom

from blogpay import BlogPay, PRICES

SEOUL = ZoneInfo('Asia/Seoul')
WREATH = '쌀빛그림화환은 설치 후 쌀로 교환해 혼주님 명의로 도움이 필요한 곳에 기부하는 친환경 화환입니다.'
HOSTS = {'bojagicard.com', 'feelcard.co.kr', 'salondeletter.com', 'toourguest.com', 'itscard.co.kr', 'itscard.com'}
WHEN = re.compile(r'(20\d\d)[년./-]\s*(\d{1,2})[월./-]\s*(\d{1,2})일?\s*[ (]*(?:[월화수목금토일](?:요일)?\)?)?\s*(오전|오후)?\s*(\d{1,2})(?:\s*시\s*(?:(\d{1,2})\s*분)?|:(\d{1,2}))')
PRODUCT_LOCK = threading.Lock()
PRODUCT_CACHE = {}
HOST_ROLE = re.compile(r'장남|차남|막내아들|아들|장녀|차녀|막내딸|딸')


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


def get_page(url):
    u = https_url(url)
    if not u or u.hostname.removeprefix('www.') not in HOSTS:
        raise ValueError('이 청첩장 주소는 아직 읽기 어려워요. 행사 이름, 날짜와 시간, 장소를 적어주세요.')
    with build_opener(NoRedirect()).open(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=9) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('청첩장을 읽기 어려워요. 행사 이름, 날짜와 시간, 장소를 적어주세요.')
    return raw.decode('utf-8', 'replace')


def text_at(tree, path, **variables):
    matches = tree.xpath(path, **variables)
    return matches[0].text_content().strip() if matches else ''


def parse_time(value):
    m = WHEN.search(value)
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


def parse_page(source, raw):
    tree = dom.fromstring(raw)
    host = urlsplit(source).hostname.removeprefix('www.')
    meta = {m.get('property'): m.get('content', '') for m in tree.xpath('//meta[@property]')}
    image = urljoin(source, meta.get('og:image', '')) if meta.get('og:image') else ''
    if not https_url(image) or not (urlsplit(image).hostname == host or urlsplit(image).hostname.endswith('.' + host)):
        image = ''
    families, address = [], ''
    if host == 'bojagicard.com':
        found = re.search(r"var wday = '([^']+)'", raw)
        when = parse_time(found[1] if found else meta.get('og:description', ''))
        venue_el = tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," map_venue_name ")]')
        venue = venue_el[0].text_content().strip() if venue_el else ''
        sibling = venue_el[0].getnext() if venue_el else None
        address = sibling.text_content().strip() if sibling is not None else ''
        for side in ('a','b'):
            def get(field):
                return text_at(tree, '//*[@data-mcdi_chain=$key]', key=f'insa-{side}-{field}')
            parents = '·'.join(filter(None, (get('father-name'), get('mother-name'))))
            families.append((parents, text_at(tree, '//*[@data-ord_side=$side]', side=side), get('self-name')))
    elif host == 'feelcard.co.kr':
        description = meta.get('og:description', '')
        when = parse_time(description)
        venue = description[WHEN.search(description).end():].strip()
        venue = venue.replace('엘리에나 호텔', '엘리에나호텔')
        address = text_at(tree, '//*[@class="section_traffic__venue-address"]')
        for side in ('groom','bride'):
            def get(field):
                return text_at(tree, '//*[@data-preview-shared=$key and not(@hidden)]', key=f'{side}_{field}')
            families.append(('·'.join(filter(None, (get('father_name'),get('mother_name')))), get('ship'), get('name')))
    elif host == 'salondeletter.com':
        def get(field):
            return text_at(tree, '//*[@var=$key]', key=field)
        when = parse_time(meta.get('og:description', ''))
        venue = get('placeName')
        if get('hallName') and get('hallName') not in venue:
            venue += ' ' + get('hallName')
        address = get('placeAddrRoad')
        for side in ('groom','bride'):
            def name(role):
                return get(f'{side}{role}LastName') + get(f'{side}{role}FirstName')
            families.append(('·'.join(filter(None, (name('Dad'),name('Mom')))), get(side+'RelationName'), name('')))
    else:
        when = parse_time(meta.get('og:description', ''))
        venue = meta.get('og:description', '')[WHEN.search(meta.get('og:description', '')).end():].strip()
        names = re.match(r'([가-힣]{2,8})\s*[♡♥❤&·ㆍ]+\s*\ufe0f?\s*([가-힣]{2,8})', meta.get('og:title',''))
        if names:
            families = [('', '', n) for n in names.groups()]
    if not venue:
        raise ValueError('행사 장소를 찾지 못했어요. 예식장 이름과 홀을 적어주세요.')
    if len(families) != 2 or any(not f[2] for f in families):
        raise ValueError('신랑과 신부 이름을 확인해 주세요.')
    return {'name': f'{families[0][2]} {families[1][2]} 결혼', 'when': when, 'venue': venue,
            'address': address, 'families': families, 'source': source, 'image': image}


def parse_source(source, extract):
    if source.startswith(('https://','http://')) and not re.search(r'\s', source):
        try:
            return parse_page(source, get_page(source))
        except OSError as error:
            raise ValueError('청첩장에 연결하지 못했어요. 잠시 뒤 다시 보내거나 행사 정보를 적어주세요.') from error
    fields = extract(source)
    missing = [label for key, label in (('name','행사 이름'),('date','날짜와 시간'),('venue','장소'))
               if not isinstance(fields.get(key), str) or not fields[key].strip()]
    if missing:
        raise MissingInformation('빠진 정보만 적어주세요. ' + ', '.join(missing))
    return {'name': fields['name'].strip()[:80], 'when': parse_time(fields['date']),
            'venue': fields['venue'].strip()[:140], 'address': '', 'families': [],
            'source': '', 'image': '', 'greeting': str(fields.get('greeting') or '')[:600],
            'signature': str(fields.get('signature') or '')[:100].removesuffix('.'),
            'note': str(fields.get('note') or '')[:500]}


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
    if families and all(families[0]) and HOST_ROLE.fullmatch(families[0][1]):
        parents, relation, name = families[0]
        return ('결혼식에 초대드립니다.', f'저희 {relation} {name}의 결혼식에 초대드립니다.\n두 사람의 첫걸음을 함께 축복해 주시면 감사하겠습니다.',
                f'{parents} 올림', f'{parents}의 {relation} {name} 예식')
    title = '결혼식에 초대드립니다.' if families else f"{event['name']}에 초대드립니다."
    return title, event.get('greeting') or '귀한 걸음으로 함께해 주시면 감사하겠습니다.', event.get('signature',''), event['name']


def page(event, date, clock, order_url, fallback_image):
    e = html.escape
    title, greeting, signature, og = content(event)
    image = event['image']
    source = event['source']
    original = f'<a class="button" href="{e(source)}">모바일 청첩장 보러가기</a>' if source else ''
    photo = f'<img class="photo" src="{e(image)}" alt="청첩장 사진">' if image else ''
    family = ''.join(f'<p>{e(p)}{"의 " if p else ""}{e(r)} <strong>{e(n)}</strong></p>' for p,r,n in event['families'])
    note = e(event.get('note',''))
    wreath = (WREATH if event['families'] else
              '쌀빛그림화환은 설치 후 쌀로 교환해 주최 측 명의로 도움이 필요한 곳에 기부하는 친환경 화환입니다.')
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>{e(title)}</title><meta property="og:title" content="{e(og)}">
<meta property="og:description" content="{e(date+' '+clock)}"><meta property="og:image" content="{e(image or fallback_image)}">
<style>body{{margin:0;background:#eee6d7;color:#262b23;font:20px/1.65 system-ui}}
main{{max-width:560px;margin:auto;padding:16px}}section,.shop{{background:#fffdf7;margin:0 0 16px;padding:24px;border-radius:12px}}
h1{{font-size:clamp(25px,5vw,38px);margin-top:0}}p{{white-space:pre-line}}
.signature{{margin-top:3rem;text-align:right;font-size:1.24em;font-weight:650}}
.details,.shop{{text-align:center}}.clock{{white-space:nowrap}}.time{{font-size:1.2em;font-weight:650}}
.photo{{display:block;width:100%;height:auto;border-radius:12px;margin-bottom:16px}}
.button,.shop{{display:block;color:#173528}}.button{{padding:12px;border:1px solid;margin-top:20px}}
.shop{{background:#e7eee0}}</style></head><body><main>
<section><h1>{e(title)}</h1><p>{e(greeting)}</p><p class="signature">{e(signature)}</p></section>{photo}
<section class="details"><h2>행사 안내</h2>{family}<p class="time">{e(date)}<br><span class="clock">{e(clock)}</span></p>
<p>{e(event['venue'])}<br>{e(event['address'])}</p><p>{note}</p>{original}</section>
<a class="shop" href="{e(order_url)}"><h2>쌀빛그림화환 안내</h2><p>{wreath}</p><strong>쌀빛그림화환 주문하기</strong></a>
</main></body></html>'''


def find_links(product):
    shop, token = os.getenv('BLOGPAY_SHOP_ID'), os.getenv('BLOGPAY_API_TOKEN')
    if not shop or not token:
        raise ValueError('쇼핑몰 연결을 확인해야 해요. 관리자에게 알려주세요.')
    with PRODUCT_LOCK:
        cached = PRODUCT_CACHE.get(product)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        links = BlogPay(shop, token).existing_links(product)
        if set(links) == set(PRICES):
            PRODUCT_CACHE[product] = (time.monotonic() + 300, links)
        return links


def make(source, root, origin, extract, photo=None):
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
    links = find_links(product)
    if set(links) != set(PRICES) or len(set(links.values())) != len(PRICES):
        raise ValueError('이 행사에 연결할 93,500원과 99,000원 주문서를 모두 찾지 못했어요. 상품 이름과 가격을 확인해 주세요.')
    job = secrets.token_hex(16)
    folder = root / 'invitations' / job
    folder.mkdir(parents=True, exist_ok=False)
    _, greeting, signature, _ = content(event)
    items = []
    for price in PRICES:
        url = f'{origin}/i/{job}/{price}.html'
        (folder / f'{price}.html').write_text(page(event,date,clock,links[price],origin+'/guide.png'), encoding='utf-8')
        message = '\n\n'.join(filter(None, [greeting, f"🗓 일시 {date} {clock}\n📍 장소 {event['venue']}", signature,
                                  url, f'🌾 화환 안내 · {price:,}원\n쌀빛그림화환으로 축하의 마음을 전해 주세요.',
                                  '화환 문의 ☎ 031-8067-8580']))
        items.append({'price':price,'url':url,'message':message})
    result = {'jobId':job,'status':'done','items':items,'expires':expiry.isoformat()}
    (folder / 'result.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    return result


def get_result(root, job):
    try:
        return json.loads((root / 'invitations' / job / 'result.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
