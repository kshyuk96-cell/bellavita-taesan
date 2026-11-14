"""Text field parser copied from the validated order intake; no network imports."""
import re

LABEL_ALIASES = {
    '보내시는분':'sender','보내는분':'sender','보내는사람':'sender','발신자':'sender',
    '받는분':'recipient','받으시는분':'recipient','수신자':'recipient',
    '배송일':'delivery_date','배송날짜':'delivery_date','배송일자':'delivery_date',
    '배송시간':'delivery_time','배송시각':'delivery_time',
    '작품':'artwork','규격':'size','사이즈':'size','액자사이즈':'size',
    '축하문구':'message','축하·위로문구':'message','문구':'message',
    '행사구분':'event_type','행사종류':'event_type','수량':'quantity','개수':'quantity',
    '설치장소':'venue','설치주소':'address','설치장소주소':'address','배송지':'address','주소':'address',
    '사진보낼곳':'photo_phone','사진보내실곳':'photo_phone','청첩장':'source','부고장':'source','초대장':'source'}
LABEL_NAMES=sorted(LABEL_ALIASES,key=len,reverse=True)
HSPACE=r'[^\S\r\n]'
LABEL_TOKEN='|'.join((HSPACE+'*').join(re.escape(c) for c in label) for label in LABEL_NAMES)
BULLET=r'(?:[-•·▪▶▷📍📝📦👤]'+HSPACE+r'*)?'
INLINE_SEPARATOR=re.compile(HSPACE+r'*[,;|/]'+HSPACE+r'*(?=(?:'+LABEL_TOKEN+r')'+HSPACE+r'*(?:[:：]|[은는]'+HSPACE+r'*))')
LABEL_PATTERN=re.compile(r'^'+HSPACE+r'*'+BULLET+r'(?P<label>'+LABEL_TOKEN+r')'+HSPACE+r'*(?:(?:[:：])'+HSPACE+r'*(?P<colon>.*)|(?:은|는)'+HSPACE+r'*(?P<spoken>.*)|$)')


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
            candidate=LABEL_ALIASES[re.sub(r'\s+','',match['label'])]
            if candidate==active and not any(line.strip() for line in lines) and match['colon'] is None and match['spoken'] is None:
                lines.append(line);continue
            finish();active=LABEL_ALIASES[re.sub(r'\s+','',match['label'])]
            lines=[match['colon'] if match['colon'] is not None else match['spoken'] or '']
        elif active is not None:lines.append(line)
        else:unlabelled.append(line)
    finish()
    return fields,unlabelled
