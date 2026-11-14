"""Owner-bound keyset pagination over review reasons, without a latest-100 cutoff."""
import base64,hashlib,json,re

FILTERS={'전체','누락','일정','지역','오류','취소','공유','재시도'}
REQUEST=re.compile(r'^주문 검토 대기(?: (전체|누락|일정|지역|오류|취소|공유|재시도))?(?: 다음 ([A-Za-z0-9_-]{1,700}))?$')

def token(owner,category,after):
    payload={'owner':hashlib.sha256(owner.encode()).hexdigest()[:16],'filter':category,'after':list(after)}
    return base64.urlsafe_b64encode(json.dumps(payload,separators=(',',':'),ensure_ascii=False).encode()).decode().rstrip('=')

def decode(value,owner,category):
    try:
        obj=json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
        if obj['owner']!=hashlib.sha256(owner.encode()).hexdigest()[:16]:raise ValueError()
        if obj['filter'] not in FILTERS or (category and category!=obj['filter']):raise ValueError()
        after=obj['after']
        if not isinstance(after,list) or len(after)!=2 or any(not isinstance(v,str) or not 1<=len(v)<=200 for v in after):raise ValueError()
        return obj['filter'],tuple(after)
    except Exception:raise ValueError('다음 페이지 정보가 잘못되었거나 계정·필터가 달라요. 처음부터 조회해 주세요.')

def reason(c,row,category):
    from order_assistance import assess,explicit_details
    data=json.loads(row['details']) or explicit_details(row['raw']);review=assess(data)
    prior=c.execute('SELECT assessment FROM order_triage WHERE order_id=?',(row['id'],)).fetchone()
    issues=review['issues']+(json.loads(prior[0])['issues'] if prior else [])
    codes={issue['code'] for issue in issues}
    missing=bool(review['missing']);cancel=row['state']=='cancel_requested';retry=row['state']=='review_hold'
    shared=c.execute('SELECT 1 FROM order_transmissions WHERE order_id=? AND receipt IS NOT NULL AND verified_at IS NOT NULL',(row['id'],)).fetchone()
    sharing=row['state']=='saved' and not shared
    schedule=any(code.startswith(('delivery_','event_')) and not code.startswith('delivery_region_') for code in codes)
    region=any(code.startswith('delivery_region_') for code in codes)
    flags={'누락':missing,'취소':cancel,'재시도':retry,'공유':sharing,'일정':schedule,'지역':region,'오류':'parse_or_write_error' in codes}
    if category!='전체' and not flags[category]:return None
    if not any(flags.values()) and row['state'] not in ('pending','update'):return None
    parts=[]
    if cancel:parts.append('취소 요청 / 현장 취소 미확인')
    if retry:parts.append('재시도 한도·검토 보류')
    if missing:parts.append('누락: '+', '.join(review['missing']))
    parts.extend(dict.fromkeys(i['message'] for i in issues))
    if row['state'] in ('pending','update'):parts.append('원장 반영 대기')
    if sharing:parts.append('카톡 공유 미확인')
    return ' / '.join(parts)

def command(c,owner,message):
    match=REQUEST.fullmatch(message)
    if not match:return None
    category,cursor=match.groups();after=None
    if cursor:
        try:category,after=decode(cursor,owner,category)
        except ValueError as error:return str(error)
    category=category or '전체';lines=[];last_shown=None;more=False
    while True:
        sql="SELECT * FROM order_operations WHERE owner=? AND kind='kakao'";args=[owner]
        if after:sql+=' AND (created,id)<(?,?)';args.extend(after)
        rows=c.execute(sql+' ORDER BY created DESC,id DESC LIMIT 50',args).fetchall()
        if not rows:break
        for row in rows:
            after=(row['created'],row['id']);why=reason(c,row,category)
            if why is None:continue
            ref='주문 '+str(row['order_number']) if row['order_number'] else '접수 '+row['id']
            line=ref+' — '+why[:190]
            if len(lines)>=10 or sum(len(v)+1 for v in lines)+len(line)>600:more=True;break
            lines.append(line);last_shown=after
        if more or len(rows)<50:break
    if not lines:return '이 계정의 '+category+' 검토 대기가 없어요.'
    text='검토 대기 / '+category+' / 현재 페이지\n'+'\n'.join(lines)
    if more:text+='\n다음: 주문 검토 대기 다음 '+token(owner,category,last_shown)
    return text
