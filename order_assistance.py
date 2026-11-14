"""Deterministic clarification, schedule checks and owner-scoped draft assistance."""
import datetime as dt
import json,re
from zoneinfo import ZoneInfo
import order_intake
import order_review_rules
from delivery_regions import delivery_alert

SEOUL=ZoneInfo('Asia/Seoul')
REQUIRED=(('sender','보내는 분'),('recipient','받는 분'),('event_type','행사 구분'),('delivery_date','배송일'),('delivery_time','배송시각'),('artwork','작품'),('size','규격'))
PLACEHOLDER=re.compile(r'^(?:미정|미확인|확인\s*필요|모름|없음|추후|나중에|\?+)$')

def present(value):return bool(str(value or '').strip()) and not PLACEHOLDER.fullmatch(str(value).strip())

def schedule(details,now=None):
    now=now or dt.datetime.now(SEOUL)
    if now.tzinfo is None:raise ValueError('Timezone-aware current time required')
    now=now.astimezone(SEOUL);issues=[];times={}
    for prefix,label in (('delivery','배송'),('event','행사')):
        day=details.get(prefix+'_date');clock=details.get(prefix+'_time')
        if not present(day) and not present(clock):continue
        try:
            date=dt.date.fromisoformat(day) if present(day) else None
            if present(clock) and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',str(clock)):raise ValueError()
        except (ValueError,TypeError):
            issues.append({'code':prefix+'_invalid','message':label+' 날짜·시간 형식 확인 필요','hold':True});continue
        if date and date<now.date():issues.append({'code':prefix+'_past','message':label+' 날짜가 과거입니다. 날짜 확인 필요','hold':True})
        if date and present(clock):times[prefix]=dt.datetime.combine(date,dt.time.fromisoformat(clock),SEOUL)
    delivery=times.get('delivery');event=times.get('event')
    if delivery:
        hours=(delivery-now).total_seconds()/3600
        if hours<0 and delivery.date()>=now.date():issues.append({'code':'delivery_elapsed','message':'배송 예정 시각이 지났습니다. 진행 상태 확인 필요','hold':True})
        elif 0<=hours<=3:issues.append({'code':'delivery_soon','message':'배송 예정까지 3시간 이내입니다. 현장 마감 확인 필요','hold':False})
    if delivery and event and delivery>=event:
        issues.append({'code':'delivery_not_before_event','message':'배송 예정이 행사 시작과 같거나 이후입니다. 배송 마감 확인 필요','hold':True})
    elif present(details.get('delivery_date')) and present(details.get('event_date')):
        try:
            if dt.date.fromisoformat(details['delivery_date'])>dt.date.fromisoformat(details['event_date']):
                issues.append({'code':'delivery_after_event_date','message':'배송일이 행사일 이후입니다. 날짜 확인 필요','hold':True})
        except (ValueError,TypeError):pass
    return issues

def assess(details,now=None):
    missing=[label for key,label in REQUIRED if not present(details.get(key))]
    if not any(present(details.get(key)) for key in ('venue','address')):missing.append('설치 장소·주소')
    elif not present(details.get('address')):missing.append('상세 주소')
    issues=schedule(details,now)
    alert=delivery_alert(' / '.join(str(details.get(k) or '') for k in ('venue','address')))
    if alert['message']:issues.append({'code':'delivery_region_'+alert['status'],'message':alert['message'],'hold':False})
    return {'missing':missing,'issues':issues,'hold':any(i['hold'] for i in issues)}

def question(details,now=None):
    result=assess(details,now);parts=[]
    if result['missing']:parts.append('이번 주문에서 '+', '.join(result['missing'])+'만 알려주세요.')
    parts.extend(dict.fromkeys(issue['message'] for issue in result['issues']))
    return '\n'.join(parts) or '추가로 확인할 누락 항목이 없어요.'

def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS order_triage (order_id TEXT PRIMARY KEY, assessment TEXT NOT NULL)')
    c.execute('''CREATE TABLE IF NOT EXISTS order_fragments (order_id TEXT, message_id TEXT, body TEXT NOT NULL,
                 PRIMARY KEY(order_id,message_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS order_transmissions (order_id TEXT, destination TEXT, receipt TEXT,
                 verified_at TEXT, PRIMARY KEY(order_id,destination))''')

def record(c,key,details,error=False):
    result=assess(details)
    if error:result['issues'].append({'code':'parse_or_write_error','message':'주문 해석 또는 원장 연결 확인 필요','hold':True});result['hold']=True
    c.execute('INSERT OR REPLACE INTO order_triage VALUES (?,?)',(key,json.dumps(result,ensure_ascii=False)))
    return result

REF=re.compile(r'^(주문|접수) (상태|확인질문|보완|문구추가) ([A-Za-z0-9_-]{1,100})(?:\n([\s\S]+))?$')

def locate(c,owner,kind,ref):
    if kind=='주문':
        if not ref.isdecimal():return None
        rows=c.execute("SELECT * FROM order_operations WHERE owner=? AND kind='kakao' AND order_number=?",(owner,int(ref))).fetchall()
    else:rows=c.execute("SELECT * FROM order_operations WHERE owner=? AND kind='kakao' AND id=?",(owner,ref)).fetchall()
    return rows[0] if len(rows)==1 else None

def explicit_details(raw):
    try:return order_intake.parse_fields(order_intake.REQUEST.sub('',raw))[0]
    except ValueError:return {}

def command(db,owner,message):
    import order_operations as ops
    if message=='주문 검토 대기':
        with db() as c:
            rows=c.execute("SELECT * FROM order_operations WHERE owner=? AND kind='kakao' ORDER BY created DESC LIMIT 100",(owner,)).fetchall()
            lines=[]
            for row in rows:
                data=json.loads(row['details']) or explicit_details(row['raw']);review=assess(data)
                prior=c.execute('SELECT assessment FROM order_triage WHERE order_id=?',(row['id'],)).fetchone()
                issues=review['issues']+(json.loads(prior[0])['issues'] if prior else [])
                if row['state']=='cancel_requested':reason='취소 요청 / 현장 취소 미확인'
                elif review['missing'] or issues:reason='누락: '+', '.join(review['missing']) if review['missing'] else ''
                elif row['state'] in ('pending','update'):reason='원장 반영 대기'
                elif row['state']=='saved' and not c.execute('SELECT 1 FROM order_transmissions WHERE order_id=? AND receipt IS NOT NULL AND verified_at IS NOT NULL',(row['id'],)).fetchone():reason='카톡 공유 미확인'
                else:continue
                messages=list(dict.fromkeys(i['message'] for i in issues))
                reason=' / '.join(filter(None,(reason,*messages)))
                ref='주문 '+str(row['order_number']) if row['order_number'] else '접수 '+row['id']
                line=ref+' — '+reason[:230]
                if sum(len(item)+1 for item in lines)+len(line)>800:
                    lines.append('추가 항목은 개별 주문 상태로 확인해 주세요.');break
                lines.append(line)
                if len(lines)>=10:break
        return '검토 대기 (최근 접수 100건 중 최대 10건)\n'+'\n'.join(lines) if lines else '최근 접수 100건에서 확인할 검토 대기가 없어요.'
    match=REF.fullmatch(message)
    if not match:return None
    kind,action,ref,body=match.groups();body=body or ''
    with db() as c:
        c.execute('BEGIN IMMEDIATE');row=locate(c,owner,kind,ref)
        if row is None:return '이 직원 계정의 대상 주문을 하나로 확인하지 못했어요. 주문번호 또는 전체 접수ID를 확인해 주세요.'
        parsed_details=json.loads(row['details'])
        details=parsed_details or explicit_details(row['raw'])
        if action=='상태':
            labels={'pending':'접수·원장 등록 대기','update':'수정·원장 반영 대기','saved':'원장 반영 확인','cancel_requested':'취소 요청·현장 취소 미확인'}
            receipts=c.execute('SELECT destination FROM order_transmissions WHERE order_id=? AND receipt IS NOT NULL AND verified_at IS NOT NULL',(row['id'],)).fetchall()
            shared=', '.join(r[0] for r in receipts) if receipts else '미확인'
            phase=labels.get(row['state'],'처리 상태 확인 필요')
            review=c.execute('SELECT assessment FROM order_triage WHERE order_id=?',(row['id'],)).fetchone()
            needs_review=assess(details)['hold'] or (review and json.loads(review[0])['hold'])
            if needs_review and row['state'] in ('pending','update'):phase+=' / 검토 보류'
            return phase+'\n카톡 공유: '+shared+'\n'+question(details)
        if action=='확인질문':return question(details)
        if row['state']=='cancel_requested':return '취소 요청 중이라 메시지를 합치지 않았어요.'
        message_id=None
        if action=='문구추가':
            first,sep,body=body.partition('\n')
            mid=re.fullmatch(r'메시지ID: ([A-Za-z0-9_-]{4,100})',first)
            if not mid or not sep:return '메시지ID: 고유번호 다음 줄에 보내는분: 추가 직함을 적어주세요. 같은 메시지는 같은 ID를 사용해 주세요.'
            message_id=mid[1]
            previous=c.execute('SELECT body FROM order_fragments WHERE order_id=? AND message_id=?',(row['id'],message_id)).fetchone()
            if previous:return '같은 메시지ID를 이미 합쳤어요.' if previous[0]==body else '같은 메시지ID에 다른 내용이 있어 확인이 필요해요.'
        if row['state'] not in ('saved','pending'):return '수정 처리가 진행 중이에요. 완료 후 메시지를 합쳐주세요.'
        try:fields,unlabelled=order_intake.parse_fields(body)
        except ValueError:return '같은 항목에 다른 값이 있어 확인이 필요해요.'
        if not fields or any(line.strip() for line in unlabelled) or set(fields)-set(ops.EDIT):return '보완할 항목을 이름: 값 형식으로 적어주세요.'
        if any(not present(v) for v in fields.values()):return '보완할 실제 값을 확인해 주세요.'
        if action=='문구추가' and set(fields)!={'sender'}:return '문구추가는 보내는분 직함만 합칠 수 있어요. 다른 항목은 보완으로 적어주세요.'
        try:
            order_review_rules.check_location_change(details,fields)
            if 'sender' in fields:fields['sender']=order_review_rules.sender_text(fields['sender'])
        except ValueError as error:return str(error)
        after={**details,**fields}
        if action=='문구추가':after['sender']='\n'.join(dict.fromkeys(str(details.get('sender') or '').splitlines()+fields['sender'].splitlines()))
        if assess(after)['hold']:return '일정 확인이 필요해 저장을 보류했어요.\n'+question(after)
        if after==details:return '기존 내용과 같아 변경하지 않았어요.'
        if not parsed_details:
            if action=='문구추가':return '원문 해석 전에는 보내는분 전체를 보완으로 적어주세요.'
            try:original,unlabelled=order_intake.parse_fields(order_intake.REQUEST.sub('',row['raw']))
            except ValueError:return '원문에 충돌하는 항목이 있어 수동 검토가 필요해요.'
            labels={}
            for label,key in order_intake.LABEL_ALIASES.items():labels.setdefault(key,label)
            corrected='주문 접수\n'+'\n'.join(unlabelled)+'\n'+'\n'.join(labels[key]+': '+value for key,value in {**original,**fields}.items())
            c.execute('INSERT INTO order_changes VALUES (?,?,?,?,?)',(row['id'],owner,ops.now(),ops.dump(original),ops.dump(after)))
            c.execute("UPDATE order_operations SET raw=?,details='{}',retry_after=0 WHERE id=?",(corrected,row['id']))
            record(c,row['id'],after)
            return '접수 원문을 보완했어요. 링크와 행사 정보는 다시 해석하며, 원장 반영은 아직 미확인입니다.\n'+question(after)
        c.execute('INSERT INTO order_changes VALUES (?,?,?,?,?)',(row['id'],owner,ops.now(),ops.dump(details),ops.dump(after)))
        if message_id:c.execute('INSERT INTO order_fragments VALUES (?,?,?)',(row['id'],message_id,body))
        c.execute('UPDATE order_operations SET details=?,state=?,retry_after=0 WHERE id=?',(ops.dump(after),'update' if row['state']=='saved' else 'pending',row['id']))
        record(c,row['id'],after)
        return '지정한 주문에 보완 내용을 합쳤어요. 원장 반영은 아직 미확인입니다.\n'+question(after)
