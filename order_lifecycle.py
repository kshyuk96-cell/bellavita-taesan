"""Explicit reference-based amendments, cancellation requests and additional orders."""
import datetime as dt
import json,re,uuid
import order_operations as ops
import order_intake
import order_review_rules
import order_sharing

COMMAND=re.compile(r'^(주문 수정|주문 취소|추가 주문) ([1-9][0-9]*)(?:\n([\s\S]+))?$')

def command(db,owner,message):
    from order_lock import writer_lock
    with writer_lock(db):return _command_locked(db,owner,message)

def _command_locked(db,owner,message):
    match=COMMAND.fullmatch(message)
    if not match:
        if re.search(r'아까.*(?:수정|취소)|같은\s*(?:걸|거).*하나\s*더|주문\s*(?:수정|취소)|추가\s*주문',message):
            return '대상 주문번호를 적어주세요. 수정: 주문 수정 21380 + 다음 줄 수정 항목 / 취소: 주문 취소 21380 / 추가: 추가 주문 21380 + 다음 줄 요청ID: 고유번호'
        return None
    action,number,body=match.groups();body=body or ''
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        rows=c.execute("SELECT * FROM order_operations WHERE order_number=? AND owner=? AND kind='kakao'",(int(number),owner)).fetchall()
        if len(rows)!=1:return '이 직원 계정에서 접수한 대상 주문 하나를 확인하지 못했어요. 주문번호를 확인해 주세요.'
        row=rows[0];before=json.loads(row['details'])
        if action=='주문 취소':
            if body.strip():return '취소 요청에는 주문번호만 적어주세요.'
            if row['state']=='cancel_requested':return '이미 취소 요청을 기록했어요. 현장·배송 취소 완료는 아직 미확인입니다.'
            if row['state'] not in ('saved','update','review_hold'):return '처리 상태를 확인한 뒤 취소를 요청해 주세요.'
            order_sharing.invalidate(c,row['id'])
            after={**before,'cancellation_requested':True}
            c.execute('INSERT INTO order_changes VALUES (?,?,?,?,?)',(row['id'],owner,ops.now(),ops.dump(before),ops.dump(after)))
            c.execute("UPDATE order_operations SET state='cancel_requested',details=? WHERE id=?",(ops.dump(after),row['id']))
            return '취소 요청을 기록하고 자동 원장 수정을 멈췄어요. 시트·카톡 공유·현장 배송 취소 완료는 아직 미확인입니다.'
        if row['state']!='saved':return '수정·취소·등록 처리가 진행 중이에요. 상태 확인 후 요청해 주세요.'
        if action=='추가 주문':
            request=re.fullmatch(r'요청ID: ([A-Za-z0-9_-]{4,80})',body.strip())
            if not request:return '같은 행사에 추가하는 주문은 요청ID: 고유번호를 다음 줄에 적어주세요. 재전송은 같은 ID, 별도 추가는 다른 ID를 사용해 주세요.'
            key=str(uuid.uuid5(uuid.NAMESPACE_URL,'bellavita:additional:'+owner+':'+row['id']+':'+request[1]))
            if c.execute('SELECT 1 FROM order_operations WHERE id=?',(key,)).fetchone():return '이 요청ID의 추가 주문은 이미 보관했어요. 중복 등록하지 않았습니다.'
            after={k:v for k,v in before.items() if k not in ('order_number','cancellation_requested','regional_alert','delivery_region')}
            after['note']=' · '.join(filter(None,(before.get('note'),f'같은 행사 추가 요청 / 원주문 {number}')))
            ops.stage(c,key,owner,'kakao','',after,ops.now())
            c.execute('INSERT INTO order_changes VALUES (?,?,?,?,?)',(key,owner,ops.now(),ops.dump({'parent_order':row['id']}),ops.dump(after)))
            return '같은 행사·장소·문구의 별도 추가 주문을 보관했어요. 원장 등록 완료 여부는 주문 접수 상태로 확인해 주세요.'
        try:fields,unlabelled=order_intake.parse_fields(body)
        except ValueError:return '같은 수정 항목에 다른 값이 있어 확인이 필요해요.'
        if any(line.strip() for line in unlabelled) or not fields or set(fields)-set(ops.EDIT):
            return '수정할 항목만 이름: 값 형식으로 적어주세요. 예: 주소: 부산광역시 해운대구 …'
        if any(not value.strip() for value in fields.values()):return '비어 있는 수정값을 확인해 주세요.'
        try:
            order_review_rules.check_location_change(before,fields)
            if 'sender' in fields:fields['sender']=order_review_rules.sender_text(fields['sender'])
        except ValueError as error:return str(error)
        after={**before,**fields}
        try:
            if after.get('delivery_date'):dt.date.fromisoformat(after['delivery_date'])
        except ValueError:return '배송일을 YYYY-MM-DD 형식의 실제 날짜로 적어주세요.'
        if after.get('delivery_time') and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',after['delivery_time']):return '배송시각을 HH:MM으로 확인해 주세요.'
        if after==before:return '기존 주문과 같은 값이라 추가 수정하지 않았어요.'
        order_sharing.invalidate(c,row['id'])
        c.execute('INSERT INTO order_changes VALUES (?,?,?,?,?)',(row['id'],owner,ops.now(),ops.dump(before),ops.dump(after)))
        c.execute("UPDATE order_operations SET details=?,state='update',attempts=0,retry_after=0 WHERE id=?",(ops.dump(after),row['id']))
        return '기존 주문번호에 수정 요청을 보관했어요. 원장 반영은 아직 미확인입니다.'
