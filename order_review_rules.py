"""Conservative location edits, explicit order boundaries and inscription choices."""
import re

BOUNDARY=re.compile(r'(?m)^\s*(?:\[주문\s*([1-9][0-9]*)\]|주문\s*([1-9][0-9]*)\s*[:：])\s*$')

def split_orders(message):
    matches=list(BOUNDARY.finditer(message))
    if not matches:return [message]
    if len(matches)<2:raise ValueError('여러 주문은 [주문1], [주문2]처럼 각각 나눠 적어주세요.')
    numbers=[int(m[1] or m[2]) for m in matches]
    if len(set(numbers))!=len(numbers):raise ValueError('주문 구분 번호가 중복되어 확인이 필요해요.')
    prefix=message[:matches[0].start()].strip()
    if prefix and not re.fullmatch(r'주문\s*(?:접수|분리 미리보기)\s*[:：]?' ,prefix):raise ValueError('여러 주문 앞의 공통 문구를 각 주문에 나눠 적어주세요.')
    parts=[message[m.end():matches[i+1].start() if i+1<len(matches) else len(message)].strip() for i,m in enumerate(matches)]
    if any(not part for part in parts):raise ValueError('비어 있는 주문을 확인해 주세요.')
    if len(parts)>10:raise ValueError('한 번에 최대 10개 주문만 분리 확인할 수 있어요.')
    return parts

def check_location_change(before,fields):
    changed={key for key in ('venue','address') if key in fields and fields[key].strip()!=str(before.get(key) or '').strip()}
    if changed and (before.get('venue') or before.get('address')):
        if not all(str(fields.get(key) or '').strip() for key in ('venue','address')):
            raise ValueError('장소 변경은 설치장소와 주소를 함께 적어주세요. 이전 장소·주소를 섞지 않았어요.')

def sender_text(value):
    value=value.strip();lines=value.splitlines()
    if not lines:return value
    mode=re.fullmatch(r'\[(모두 인쇄|선택:\s*([1-9][0-9]*))\]',lines[0].strip())
    if not mode and re.match(r'^\[(?:선택|모두)',lines[0].strip()):raise ValueError('직함 선택 형식을 확인해 주세요.')
    if mode:
        choices=[line.strip() for line in lines[1:] if line.strip()]
        if len(choices)<2:raise ValueError('직함 후보를 두 줄 이상 나눠 적어주세요.')
        if mode[2]:
            index=int(mode[2])-1
            if index>=len(choices):raise ValueError('선택한 직함 번호를 확인해 주세요.')
            value=choices[index]
        else:value='\n'.join(choices)
    if re.search(r'또는|혹은|둘\s*중|중에서|하나만|하나\s*선택',value):
        raise ValueError('보내는분 직함을 모두 인쇄할지 하나만 선택할지 확인해 주세요. [모두 인쇄] 또는 [선택: 2] 다음 줄부터 후보를 적어주세요.')
    return value

def preview(message):
    from order_intake import REQUEST,parse_fields,source
    from order_assistance import question
    parts=split_orders(message)
    if len(parts)==1:return None
    lines=['여러 주문 분리 미리보기 / 아직 접수·등록하지 않았어요.']
    for i,part in enumerate(parts,1):
        fields,unlabelled=parse_fields(REQUEST.sub('',part))
        urls=source('\n'.join(unlabelled+[fields.get('source','')]))
        lines.append(f'주문 {i}: 링크 {len(urls)}개 / '+question(fields))
    lines.append('각 주문의 링크·장소·보내는분을 확인한 뒤 주문 접수로 각각 보내주세요.')
    return '\n'.join(lines)
