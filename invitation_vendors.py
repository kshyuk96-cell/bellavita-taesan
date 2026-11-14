"""Vendor-specific wedding data readers. Remote JavaScript is never executed."""
import datetime as dt
import re
from invitation_data import LiteralReader, named_values, next_text, nuxt_bindings


def clean(value):
    return ' '.join(str(value or '').split())


def unique(values, label):
    choices = []
    for value in values:
        if isinstance(value, str) and clean(value) and clean(value) not in choices:
            choices.append(clean(value))
    if len(choices) != 1:
        raise ValueError(label + ' 정보를 한 가지로 확인하지 못했어요.')
    return choices[0]


def optional(values):
    choices = {clean(value) for value in values if isinstance(value, str) and clean(value)}
    return next(iter(choices)) if len(choices) == 1 else ''


def result(source, when, venue, address, families):
    if len(families) != 2 or any(not family[2] for family in families):
        raise ValueError('신랑과 신부 이름을 확인해 주세요.')
    return {'name': f'{families[0][2]} {families[1][2]} 결혼', 'when': when,
            'venue': venue, 'address': address, 'families': families, 'source': source,
            'image': '', 'wedding_photo': ''}


def barunson(source, tree, parse_time):
    text = next_text([node.text or '' for node in tree.xpath('//script')])
    get = lambda key: named_values(text, key, quoted=True)
    descriptions = tree.xpath('//meta[@property="og:description"]/@content')
    # This vendor's ISO/schema clock can be a template value. Use the explicit
    # Korean event clock and cross-check the separately declared calendar date.
    when = parse_time(descriptions[0] if descriptions else '')
    dates = get('weddingDate')
    if dates and str(dates[0])[:10] != when.date().isoformat():
        raise ValueError('청첩장 날짜 표기가 달라 확인이 필요해요.')
    clock = optional(get('weddingTime'))
    if clock and parse_time(when.date().isoformat() + ' ' + clock).time() != when.time():
        raise ValueError('청첩장 시간 표기가 달라 확인이 필요해요.')
    venue = unique(get('venueName'), '예식장')
    hall = optional(get('venueHall'))
    if hall and hall not in venue:
        venue += ' / ' + hall
    families = []
    for side in ('groom', 'bride'):
        name = unique(get(side + 'Name'), '예식자')
        if name in {'신랑', '신부', '이름'}:
            raise ValueError('예식자 이름을 확인해 주세요.')
        parents = '·'.join(filter(None, (optional(get(side + 'FatherName')),
                                         optional(get(side + 'MotherName')))))
        families.append((parents, optional(get(side + 'FamilyRole')), name))
    return result(source, when, venue, optional(get('venueAddress')), families)


def makedear(source, tree):
    scripts = [node.text or '' for node in tree.xpath('//script')]
    script = next((text for text in scripts if 'window.__NUXT__=' in text), None)
    if not script:
        raise ValueError('청첩장 데이터를 읽지 못했어요.')
    bindings = nuxt_bindings(script)
    get = lambda key: named_values(script, key, bindings)
    dates = [value for value in get('date') if isinstance(value, dict)
             and all(key in value for key in ('year', 'month', 'date', 'hour', 'minute'))]
    if len(dates) != 1:
        raise ValueError('예식 날짜와 시간을 확인해 주세요.')
    date = dates[0]
    try:
        when = dt.datetime(date['year'], date['month'], date['date'], date['hour'], date['minute'])
    except (ValueError, TypeError):
        raise ValueError('예식 날짜와 시간을 확인해 주세요.')
    venue = unique(get('place'), '예식장')
    detail = optional(get('addressDetailAdd'))
    if detail and detail not in venue:
        venue += ' / ' + detail
    families = []
    for side in ('man', 'woman'):
        parents = '·'.join(filter(None, (optional(get(side + 'FatherName')),
                                         optional(get(side + 'MotherName')))))
        families.append((parents, optional(get(side + 'Title')),
                         unique(get(side + 'Name'), '예식자')))
    return result(source, when, venue, optional(get('address')), families)


def itscard(source, tree, parse_time, read_view):
    script = next((node.text or '' for node in tree.xpath('//script')
                   if re.search(r'const data\s*=', node.text or '')), None)
    if not script:
        raise ValueError('청첩장 기본 정보를 읽지 못했어요.')
    match = re.search(r'const data\s*=\s*', script)
    values = LiteralReader(script, match.end()).value()
    try:
        uid = values[1]['data']['strUID']
    except (TypeError, KeyError, IndexError):
        raise ValueError('청첩장 식별자를 확인해 주세요.')
    from urllib.parse import urlsplit
    if (not isinstance(uid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', uid)
            or urlsplit(source).path.rstrip('/').rsplit('/', 1)[-1] != uid):
        raise ValueError('청첩장 주소와 식별자가 일치하지 않아요.')
    data = read_view(uid)
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError('청첩장 행사 정보를 한 가지로 확인하지 못했어요.')
    card = data[0]
    clock = str(card.get('WDateTime', '')).strip()
    clock = re.sub(r'^낮\s*[:：]?\s*', '오후 ', clock)
    clock = re.sub(r'^(오전|오후)\s*[:：]\s*', r'\1 ', clock)
    when = parse_time(str(card.get('WDate', ''))[:10] + ' ' + clock)
    venue = clean(card.get('WPlace'))
    hall = clean(card.get('WPlaceHall'))
    if hall and hall not in venue:
        venue += ' / ' + hall
    address = ' '.join(filter(None, (clean(card.get('WAddr1')), clean(card.get('WAddr2')))))
    if not venue:
        raise ValueError('예식장 이름을 확인해 주세요.')
    families = []
    for side, role in [('G', 'GroomDv'), ('B', 'BrideDv')]:
        name = clean(card.get(side + 'NameK')) or clean(card.get(side + 'LastName')) + clean(card.get(side + 'FirstName'))
        parents = '·'.join(filter(None, (
            clean(card.get(side + 'FatherLastName')) + clean(card.get(side + 'FatherFirstName')),
            clean(card.get(side + 'MotherLastName')) + clean(card.get(side + 'MotherFirstName')))))
        families.append((parents, clean(card.get(role)), name))
    return result(source, when, venue, address, families)
