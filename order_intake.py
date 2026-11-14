"""Extract confirmed order fields without treating event times as shipping instructions."""
import re
from urllib.parse import parse_qs, urlsplit

from lxml import html

import invitation
from delivery_regions import delivery_alert

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


LABEL_ALIASES = {
    '보내시는분':'sender','보내는분':'sender','보내는사람':'sender','발신자':'sender',
    '받는분':'recipient','받으시는분':'recipient','수신자':'recipient',
    '배송일':'delivery_date','배송날짜':'delivery_date','배송일자':'delivery_date',
    '배송시간':'delivery_time','배송시각':'delivery_time',
    '작품':'artwork','규격':'size','사이즈':'size','액자사이즈':'size',
    '축하문구':'message','축하·위로문구':'message','문구':'message',
    '행사구분':'event_type','행사종류':'event_type',
    '설치장소':'venue','설치주소':'address','설치장소주소':'address','배송지':'address','주소':'address',
    '사진보낼곳':'photo_phone','사진보내실곳':'photo_phone','청첩장':'source','부고장':'source','초대장':'source'}
LABEL_NAMES=sorted(LABEL_ALIASES,key=len,reverse=True)
LABEL_TOKEN='|'.join('[ \t]*'.join(re.escape(c) for c in label) for label in LABEL_NAMES)
INLINE_SEPARATOR=re.compile(r'[ \t]*[,;|/][ \t]*(?=(?:'+LABEL_TOKEN+r')[ \t]*(?:[:：]|[은는][ \t]+))')
LABEL_PATTERN=re.compile(r'^[ \t]*(?P<label>'+ '|'.join('[ \t]*'.join(re.escape(c) for c in label) for label in LABEL_NAMES)+r')[ \t]*(?:(?:[:：])[ \t]*(?P<colon>.*)|(?:은|는)[ \t]+(?P<spoken>.*)|$)')


def parse_fields(details):
    details=INLINE_SEPARATOR.sub('\n',details)
    fields={};unlabelled=[];active=None;lines=[]
    def finish():
        if active is None:return
        value='\n'.join(lines).strip()
        if active in fields and fields[active] and value and fields[active]!=value:
            raise ValueError('같은 항목에 서로 다른 내용이 있어 이번 주문의 문구를 확인해 주세요.')
        fields[active]=value or fields.get(active,'')
    for line in details.splitlines():
        match=LABEL_PATTERN.fullmatch(line)
        if match:
            candidate=LABEL_ALIASES[re.sub(r'[ \t]+','',match['label'])]
            if candidate==active and not any(line.strip() for line in lines) and match['colon'] is None and match['spoken'] is None:
                lines.append(line);continue
            finish();active=LABEL_ALIASES[re.sub(r'[ \t]+','',match['label'])]
            lines=[match['colon'] if match['colon'] is not None else match['spoken'] or '']
        elif active is not None:lines.append(line)
        else:unlabelled.append(line)
    finish()
    return fields,unlabelled


def extract(message):
    preliminary,unlabelled_source=parse_fields(REQUEST.sub('',message))
    # URLs printed inside sender/message/address fields are not automatically new invitations.
    link_text='\n'.join(unlabelled_source+[preliminary.get('source','')])
    urls = source(link_text)
    if len(urls)>1 or (not urls and LINK.search(link_text)):
        raise ValueError('주문마다 청첩장 또는 부고장 주소 하나와 보내는 분을 적어주세요.')
    link=urls[0] if urls else ''
    if link:
        parsed=urlsplit(link)
        if parsed.hostname.removeprefix('www.')=='e-baro.co.kr':
            if parsed.path!='/bugo.php' or not re.fullmatch(r'[A-Za-z0-9]{4,32}',parse_qs(parsed.query).get('code',[''])[0]):
                raise ValueError('부고장 주소를 다시 확인해 주세요.')
            result=bugo(link,invitation.get_page(link,hosts={'e-baro.co.kr'}))
        else:result=wedding(link)
    else:result={'event_type':'','venue':'','address':'','recipient':'','source':''}
    details=REQUEST.sub('',message).replace(link,'').strip()
    fields,unlabelled=parse_fields(details)
    if not link:
        result.update({key:fields.get(key,'') for key in ('event_type','venue','address','recipient')})
        if not (result['venue'] or result['address']):raise ValueError('청첩장·부고장 주소나 설치 장소를 적어주세요.')
    elif not result.get('address'):result['address']=fields.get('address','')
    sender=fields.get('sender','')
    if not sender:
        lines=[line.strip() for line in unlabelled if line.strip() and ':' not in line
               and not re.search(r'화환.*(?:부탁|보내|주세요)|^(?:신랑|신부)\s*측',line)
               and not re.fullmatch(r'(?:안녕하세요|안녕하십니까|감사합니다|감사해요|고맙습니다|네\s*감사합니다)[\s~!.,😊🙏]*',line)]
        if len(lines)==1 and re.search('[가-힣A-Za-z]',lines[0]):sender=lines[0]
        elif lines and all(re.search(r'대표|회장|군수|CFO|CEO|지회장|사무총장',line,re.I) for line in lines):sender='\n'.join(lines)
    if not sender or len(sender)>1000:raise ValueError('보내시는 분의 이름과 직함을 확인해 주세요.')
    side=re.search(r'(신랑|신부)\s*측',details) if result['event_type']=='결혼' else None
    result.update(sender=sender,recipient=fields.get('recipient') or (result['recipient']+(' '+side[1]+'측' if side else '')),
                  delivery_date=fields.get('delivery_date',''),delivery_time=fields.get('delivery_time',''),
                  artwork=fields.get('artwork',''),size=fields.get('size',''),message=fields.get('message',''),photo_phone=fields.get('photo_phone',''))
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
    alert=delivery_alert(' / '.join(filter(None,(details.get('venue'),details.get('address')))))
    if alert['message']:lines.extend(('',alert['message']))
    return '\n'.join(lines)
