"""Explicit owner confirmation for quarantined origins and revoked source grants.

Called under order_assistance's shared writer lock. No remote operation is performed.
"""
import json,re,sqlite3
from pathlib import Path
import order_intake,order_review_rules,order_origin,customer_sources

LINK=re.compile(r'^접수 연결재확인 ([A-Za-z0-9_-]{1,100})\n원본방: ([A-Za-z0-9_-]{1,100})\n발신자ID: ([A-Za-z0-9_-]{1,100})\n보내는분: ([\s\S]{1,1000})\n확인근거: ([^\n]{4,300})$')
MANUAL=re.compile(r'^접수 직접입력확인 ([A-Za-z0-9_-]{1,100})\n보내는분: ([\s\S]{1,1000})\n확인근거: ([^\n]{4,300})$')

def sender_text(value):
    value=order_review_rules.sender_text(value)
    fields,unlabelled=order_intake.parse_fields('보내는분: '+value)
    if set(fields)!={'sender'} or fields['sender']!=value or any(v.strip() for v in unlabelled) or not any(c.isalpha() for c in value) or re.fullmatch(r'미정|미확인|확인\s*필요|없음',value):
        raise ValueError('확인된 보내는분 문구만 적어주세요. 다른 주문 항목은 별도 보완이 필요합니다.')
    return value

def rewrite_raw(raw,sender):
    if not raw.strip():return raw
    fields,unlabelled=order_intake.parse_fields(order_intake.REQUEST.sub('',raw))
    fields['sender']=sender;labels={}
    for label,key in order_intake.LABEL_ALIASES.items():labels.setdefault(key,label)
    return '주문 접수\n'+'\n'.join(unlabelled)+'\n'+'\n'.join(labels[key]+': '+value for key,value in fields.items())

def restore(c,row,actor,sender,reason,new_source):
    import order_operations as ops
    before=json.loads(row['details'])
    if before.get('cancellation_requested'):return '취소 요청 이력이 있어 연결 확인만으로 재개하지 않았어요.'
    if not before and not row['raw'].strip():return '원문과 주문 상세가 없어 전체 주문 내용을 먼저 확인해야 해요.'
    raw=rewrite_raw(row['raw'],sender)
    aliases={ops.intake_key(text,new_source) for text in (row['raw'],raw) if text.strip()}
    for alias in aliases:
        other=c.execute('SELECT id FROM order_operations WHERE id=? AND id<>?',(alias,row['id'])).fetchone()
        mapped=c.execute('SELECT order_id FROM order_intake_aliases WHERE alias_key=?',(alias,)).fetchone()
        if other or mapped and mapped[0]!=row['id']:return '같은 내용으로 접수된 다른 주문이 있어 중복 검토 후 재개해 주세요.'
    prior=c.execute('SELECT context FROM order_sources WHERE order_id=?',(row['id'],)).fetchone()
    if new_source is None:order_origin.record_manual(c,row['id'],actor,reason)
    else:
        c.execute('INSERT OR REPLACE INTO order_sources VALUES (?,?)',(row['id'],ops.dump(new_source)))
        c.execute('DELETE FROM order_origin_approvals WHERE order_id=?',(row['id'],))
    after={**before,'sender':sender} if before else {}
    c.execute('INSERT INTO order_changes VALUES (?,?,?,?,?)',(row['id'],actor,ops.now(),ops.dump({'state':row['state'],'sender':before.get('sender'),'source':json.loads(prior[0]) if prior else None}),ops.dump({'state':'reconfirm_requested','sender':sender,'source':new_source,'reason':reason})))
    c.execute('UPDATE order_operations SET raw=?,details=?,state=?,attempts=0,retry_after=0 WHERE id=?',(raw,ops.dump(after),'update' if row['order_number'] is not None else 'pending',row['id']))
    for alias in aliases:c.execute('INSERT OR IGNORE INTO order_intake_aliases VALUES (?,?)',(alias,row['id']))
    import order_sharing,order_assistance
    order_sharing.invalidate(c,row['id'])
    order_assistance.record(c,row['id'],after or order_assistance.explicit_details(raw))
    return '명시한 출처·보내는분으로 재개 요청을 기록했어요. 기존 접수ID를 유지하며 원장 반영·공유 완료는 아직 미확인입니다.'

def attempt_restore(c,*args):
    c.execute('SAVEPOINT order_reconfirmation')
    try:result=restore(c,*args)
    except ValueError:
        c.execute('ROLLBACK TO order_reconfirmation');c.execute('RELEASE order_reconfirmation')
        return '원문 또는 설정 검토가 필요해 재개 내용을 저장하지 않았어요.'
    except Exception:
        c.execute('ROLLBACK TO order_reconfirmation');c.execute('RELEASE order_reconfirmation');raise
    c.execute('RELEASE order_reconfirmation');return result

def command(db,owner,message,config_path):
    linked=LINK.fullmatch(message);manual=MANUAL.fullmatch(message)
    if not linked and not manual:
        if message.startswith(('접수 연결재확인','접수 직접입력확인')):return '전체 접수ID와 확인된 보내는분·확인근거를 적어주세요. 연결재확인은 원본방·발신자ID도 필요합니다.'
        return None
    if linked:key,room,sender_id,inscription,reason=linked.groups()
    else:key,inscription,reason=manual.groups()
    if re.search(r'미확인|예정|아직|나중|모름',reason):return '확인 예정이 아닌 실제로 확인한 근거를 적어주세요.'
    try:inscription=sender_text(inscription)
    except ValueError as error:return str(error)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM order_operations WHERE id=? AND owner=? AND kind='kakao'",(key,owner)).fetchone()
        if not row:return '이 직원 계정의 대상 접수를 확인하지 못했어요.'
        if row['state'] not in ('source_hold','origin_hold'):return '출처·연결 확인으로 보류된 주문만 재개할 수 있어요. 이미 재개했다면 중복 처리하지 않습니다.'
        previous=c.execute('SELECT context FROM order_sources WHERE order_id=?',(key,)).fetchone()
        if manual:
            if previous or row['state']!='origin_hold':return '원본방 연결이 있었던 주문은 직접입력으로 우회할 수 없어요. 연결재확인을 사용해 주세요.'
            try:return attempt_restore(c,row,owner,inscription,reason,None)
            except ValueError:return '원문 항목이 충돌해 전체 주문을 검토해야 합니다.'
        if config_path is None or not Path(config_path).is_file():return '현재 고객 연결 설정을 먼저 확인해 주세요.'
        if previous:
            try:old=json.loads(previous[0])
            except ValueError:return '기존 출처 기록이 손상되어 수동 검토가 필요해요.'
            if not isinstance(old,dict):return '기존 출처 기록을 검토해야 해요.'
            if old.get('room')!=room or old.get('sender')!=sender_id:return '기존 주문의 원본방·발신자와 달라 연결을 바꾸지 않았어요.'
        from customer_memory import CustomerMemory
        try:memory=CustomerMemory(config_path)
        except sqlite3.Error:return '고객 설정을 읽지 못해 재개하지 않았어요.'
        try:
            with memory.atomic():
                binding=memory.db.execute('SELECT 1 FROM customer_bindings b JOIN customer_profiles p ON p.id=b.customer_id WHERE b.scope=? AND b.external_id=?',(room,sender_id)).fetchone()
                if not binding:return '현재 확인된 방·발신자 고객 연결이 필요합니다.'
                try:
                    context=customer_sources.context(memory,room,sender_id)
                    return attempt_restore(c,row,owner,inscription,reason,context)
                except ValueError:return '원본 연결 또는 원문 항목을 검토해야 해 재개하지 않았어요.'
        finally:memory.db.close()
