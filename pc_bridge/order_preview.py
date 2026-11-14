"""Local text-only review. No HTTP, invitation fetch, customer defaults or Sheet writes."""
import datetime as dt
import re
from preview_fields import parse_fields
from order_review_rules import split_orders,sender_text
from delivery_regions import delivery_alert

LABELS={'event_type':'행사 구분','sender':'보내는 분','recipient':'받는 분',
        'delivery_date':'배송일','delivery_time':'배송시각','venue':'장소','address':'주소',
        'artwork':'작품','size':'규격','message':'문구'}


def preview(text):
    if not isinstance(text,str) or not text.strip() or len(text)>35000:
        raise ValueError('분석할 주문 내용을 35000자 이내로 입력해 주세요.')
    if len(split_orders(text))!=1:raise ValueError('주문을 하나씩 나눠 분석해 주세요.')
    fields,_=parse_fields(re.sub(r'^주문\s*(?:접수|등록|입력)\s*[:：]?\s*','',text))
    warnings=[]
    fields['sender']=sender_text(fields.get('sender',''))
    if not fields.get('event_type'):
        candidates=[]
        for kind,pattern in [('근조',r'근조|부고|장례'),('결혼',r'결혼|청첩장|신랑측|신부측'),('일반 행사',r'개업|취임|준공|행사')]:
            if re.search(pattern,text):candidates.append(kind)
        if len(candidates)==1:fields['event_type']=candidates[0]
        elif len(candidates)>1:warnings.append('행사 구분이 여러 개입니다. 하나를 명시해 주세요.')
    if fields.get('event_type') not in {'근조','결혼','일반 행사','행사'}:
        warnings.append('근조·결혼·일반 행사 중 행사 구분을 확인해 주세요.')
    if fields.get('quantity') and not re.fullmatch(r'1\s*(?:개|건|점)?',fields['quantity']):
        warnings.append('여러 개 또는 불명확한 수량: 주문을 나눠 확인해 주세요.')
    if fields.get('delivery_date'):
        try:dt.date.fromisoformat(fields['delivery_date'])
        except ValueError:warnings.append('배송일은 실제 날짜의 YYYY-MM-DD 형식으로 확인해 주세요.')
    if fields.get('delivery_time') and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',fields['delivery_time']):
        warnings.append('배송시각을 HH:MM 형식으로 확인해 주세요.')
    missing=[label for key,label in LABELS.items() if key not in {'address','venue','message'} and
             (not fields.get(key) or re.fullmatch(r'미확인|미정|확인\s*필요|없음',fields[key]))]
    if not (fields.get('venue') or fields.get('address')):missing.append('설치 장소·주소')
    urls=re.findall(r'https?://[^\s<>]+',text)
    if urls:warnings.append('링크 본문은 조회하지 않았습니다. 링크만으로 행사 일시·장소·보내는 분을 추정하지 않습니다.')
    region=delivery_alert(fields.get('address') or fields.get('venue',''))
    if region['message']:warnings.append(region['message'])
    lines=['[주문 분석 미리보기 / 시트 미입력]']
    lines.extend(label+': '+(fields.get(key) or '미확인') for key,label in LABELS.items())
    if missing:lines.append('미확인: '+', '.join(missing))
    lines.extend(warnings)
    return {'fields':fields,'missing':missing,'warnings':warnings,'region':region,
            'text':'\n'.join(lines),'sheet_written':False,'links_fetched':False}


def fresh_messages(snapshot,seen,operator_senders=()):
    """Copied transcript must expose explicit message boundaries; never merge history."""
    import hashlib
    header=re.compile(r'(?m)^\[([^\]\r\n]{1,100})\] \[([^\]\r\n]{1,100})\] ')
    matches=list(header.finditer(snapshot))
    if not matches:raise ValueError('복사는 됐지만 메시지 경계를 확인하지 못했습니다. 주문 한 건을 직접 붙여넣어 분석해 주세요.')
    result=[]
    for i,m in enumerate(matches):
        body=snapshot[m.end():matches[i+1].start() if i+1<len(matches) else len(snapshot)].strip()
        identity=hashlib.sha256((m[1]+'\n'+m[2]+'\n'+body).encode()).hexdigest()
        if identity in seen:continue
        seen.add(identity)
        if m[1] in operator_senders or body.startswith(('[태산이','태산이 수신 테스트')):continue
        result.append({'id':identity,'sender':m[1],'text':body})
    return result
