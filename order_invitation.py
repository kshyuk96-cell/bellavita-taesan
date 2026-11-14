"""Order-only invitation reading: preserve inscriptions without checkout inference."""
import re
from urllib.parse import urlsplit,urljoin
from lxml import html as dom
import invitation_vendors

def parse_order_page(source, raw):
    from invitation import parse_time,text_at,https_url,read_itscard_view
    tree = dom.fromstring(raw)
    host = urlsplit(source).hostname.removeprefix('www.')
    meta = {m.get('property'): m.get('content', '') for m in tree.xpath('//meta[@property]')}
    if host == 'letterb.barunsoncard.com':
        return invitation_vendors.barunson(source, tree, parse_time)
    if host == 'makedear.com':
        return invitation_vendors.makedear(source, tree)
    if host == 'mcard.itscard.co.kr':
        return invitation_vendors.itscard(source, tree, parse_time, read_itscard_view)
    image = urljoin(source, meta.get('og:image', '')) if meta.get('og:image') else ''
    if not https_url(image) or not (urlsplit(image).hostname == host or urlsplit(image).hostname.endswith('.' + host)):
        image = ''
    families, address = [], ''
    wedding_photo = ''
    if host == 'bojagicard.com':
        for candidate in tree.xpath('//*[@data-src_original]/@data-src_original'):
            resolved = https_url(urljoin(source, candidate))
            if resolved and (resolved.hostname == host or resolved.hostname.endswith('.' + host)) and resolved.path.startswith('/ecard/photo/'):
                wedding_photo = resolved.geturl()
                break
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
        address = get('placeAddrRoad') or text_at(tree, '//*[contains(@class,"locate_box")]//*[contains(concat(" ",normalize-space(@class)," ")," locate ")]')
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
            'address': address, 'families': families, 'source': source, 'image': image,
            'wedding_photo': wedding_photo}
