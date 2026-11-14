"""Durable operator conveniences; never advance financial or delivery state."""
import datetime as dt
import json
import hashlib
import os
import secrets
import time
from html import escape
from urllib.parse import parse_qsl
import shop_admin_queries

CHECKS={'payment':'결제·취소 대조','wording':'문구·보내는 분 확인','production':'제작 검수 상태 확인','delivery':'행사장·배송 담당 확인','exceptions':'미처리·알림·영수증 확인'}


def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS shop_saved_searches(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,filters TEXT NOT NULL,created TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS shop_operator_handover(order_id TEXT NOT NULL,version INTEGER NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(order_id,version))')
    c.execute('CREATE TABLE IF NOT EXISTS shop_product_changes(product_id TEXT NOT NULL,revision INTEGER NOT NULL,before_details TEXT NOT NULL,after_details TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(product_id,revision))')


def now():return dt.datetime.now(dt.timezone.utc).isoformat()


def save_search(c,f):
    name=f.get('search_name','').strip()
    if not 1<=len(name)<=40:raise ValueError('검색 이름은 1~40자로 입력하세요.')
    encoded=f.get('saved_filters','')
    if len(encoded)>2400:raise ValueError('검색 조건을 확인하세요.')
    pairs=parse_qsl(encoded,keep_blank_values=True,max_num_fields=10)
    if any(k not in shop_admin_queries.KEYS for k,_ in pairs) or len({k for k,_ in pairs})!=len(pairs):raise ValueError('검색 조건을 확인하세요.')
    filters=shop_admin_queries.filters(dict(pairs));filters.pop('page',None)
    if c.execute('SELECT 1 FROM shop_saved_searches WHERE name=?',(name,)).fetchone():raise ValueError('같은 이름이 있습니다. 다른 이름으로 저장하거나 기존 검색을 삭제하세요.')
    if c.execute('SELECT count(*) FROM shop_saved_searches').fetchone()[0]>=20:raise ValueError('저장 검색은 최대 20개입니다. 사용하지 않는 검색을 삭제하세요.')
    c.execute('INSERT INTO shop_saved_searches VALUES (?,?,?,?)',(secrets.token_hex(16),name,json.dumps(filters,ensure_ascii=False),now()))


def delete_search(c,f):
    if not c.execute('DELETE FROM shop_saved_searches WHERE id=?',(f.get('search_id',''),)).rowcount:raise ValueError('검색이 이미 삭제됐습니다. 최신 화면을 확인하세요.')


def searches(c,path,filters):
    body='<section><h2>저장 검색</h2><p>관리자끼리 공유 · 최대 20개 · 새 관리자 링크에서도 유지됩니다. 검색어에 고객 정보가 있으면 사용 후 삭제하세요.</p>'
    for row in c.execute('SELECT * FROM shop_saved_searches ORDER BY created,id'):
        url=path+'?'+shop_admin_queries.query(json.loads(row['filters']))+'#admin-orders'
        body+='<div class="saved-search"><a href="'+escape(url)+'">'+escape(row['name'])+'</a>'+form(path,'delete_search',hidden('search_id',row['id']),'삭제')+'</div>'
    return body+form(path,'save_search',hidden('saved_filters',shop_admin_queries.query(filters))+'<label>현재 조건의 이름<input name="search_name" required maxlength="40" placeholder="예: 이번 주 배송 대기"></label>','현재 검색 저장')+'</section>'


def hidden(name,value):return '<input type="hidden" name="'+name+'" value="'+escape(str(value))+'">'


def form(path,action,fields,label):return '<form method="post" action="'+escape(path)+'">'+hidden('action',action)+fields+'<button>'+label+'</button></form>'


def handover_context(c,oid):
    row=c.execute('SELECT state,details,payment_number FROM invitation_orders WHERE id=?',(oid,)).fetchone()
    values=[tuple(row) if row else None]
    for sql in ('SELECT stage,version FROM shop_fulfillment WHERE order_id=?','SELECT owner,version FROM shop_assignments WHERE order_id=?','SELECT COALESCE(MAX(id),0) FROM shop_artifact_reviews WHERE order_id=?','SELECT COALESCE(MAX(id),0) FROM shop_amendment_events WHERE order_id=?','SELECT event_key,kind,amount FROM payment_events WHERE order_id=? ORDER BY event_key','SELECT version,state,source_hash FROM shop_artifacts WHERE order_id=? ORDER BY version','SELECT id,state FROM shop_messages WHERE order_id=? ORDER BY id','SELECT generation,state,amount FROM shop_cash_receipts WHERE order_id=? ORDER BY generation','SELECT id,state,version FROM shop_service_cases WHERE order_id=? ORDER BY id'):
        values.append([tuple(record) for record in c.execute(sql,(oid,))])
    return hashlib.sha256(json.dumps(values,ensure_ascii=False).encode()).hexdigest()


def save_handover(c,f):
    oid=f.get('order_id','')
    if not c.execute('SELECT 1 FROM invitation_orders o JOIN shop_offers s ON s.id=o.job WHERE o.id=?',(oid,)).fetchone():raise ValueError('쇼핑몰 주문을 찾지 못했습니다.')
    context=handover_context(c,oid)
    if f.get('handover_context')!=context:raise ValueError('주문 상태가 변경됐습니다. 최신 주문을 다시 확인한 뒤 인수인계를 저장하세요.')
    version=c.execute('SELECT COALESCE(MAX(version),0) FROM shop_operator_handover WHERE order_id=?',(oid,)).fetchone()[0]
    if str(version)!=f.get('handover_version'):raise ValueError('인수인계가 변경됐습니다. 최신 기록을 확인하고 다시 작성하세요.')
    data={key:f.get(key,'').strip() for key in ('handover_from','handover_to','handover_note')}
    if any(not 1<=len(data[k])<=60 for k in ('handover_from','handover_to')) or len(data['handover_note'])>1200:raise ValueError('인계자·인수자는 1~60자, 메모는 1200자 이내로 입력하세요.')
    data['context']=context
    data['checks']={key:f.get('check_'+key)=='yes' for key in CHECKS}
    c.execute('INSERT INTO shop_operator_handover VALUES (?,?,?,?)',(oid,version+1,json.dumps(data,ensure_ascii=False),now()))


def handover(c,row,path):
    records=c.execute('SELECT * FROM shop_operator_handover WHERE order_id=? ORDER BY version DESC',(row['id'],)).fetchall()
    latest=records[0] if records else None;data=json.loads(latest['details']) if latest else {};stale=bool(latest and data.get('context')!=handover_context(c,row['id']));checks={} if stale else data.get('checks',{})
    fields=hidden('handover_context',handover_context(c,row['id']))+hidden('order_id',row['id'])+hidden('handover_version',latest['version'] if latest else 0)
    for key,label in CHECKS.items():fields+='<label><input type="checkbox" name="check_'+key+'" value="yes"'+(' checked' if checks.get(key) else '')+'>'+label+'</label>'
    for key,label in [('handover_from','인계자'),('handover_to','인수자')]:fields+='<label>'+label+'<input name="'+key+'" required maxlength="60" value="'+escape(data.get(key,''))+'"></label>'
    fields+='<label>남은 할 일·기한·주의 사항<textarea name="handover_note" maxlength="1200">'+escape(data.get('handover_note',''))+'</textarea></label>'
    body='<details class="operator-handover"><summary>직원 인수인계 점검표 · '+str(sum(bool(v) for v in checks.values()))+'/5 확인</summary><p>관리자가 입력한 확인 기록입니다. 결제·제작 승인·배송 상태는 변경하지 않습니다. 담당자 이름은 로그인 인증과 구분합니다.</p>'+form(path,'save_operator_handover',fields,'인수인계 기록 저장')+'<h4>인수인계 이력</h4>'
    if stale:body+='<p role="status">마지막 인수인계 이후 주문·검수·배송·배정 정보가 변경됐습니다. 확인 항목을 다시 점검하세요.</p>'
    for record in records[:20]:
        d=json.loads(record['details']);checked=' · '.join(label for key,label in CHECKS.items() if d['checks'].get(key)) or '확인 항목 없음'
        body+='<p>'+escape(record['created'][:16]+' · '+d['handover_from']+' → '+d['handover_to'])+'</p><p>'+escape(checked)+'</p><p>'+escape(d['handover_note'])+'</p>'
    if len(records)>20:body+='<p>최근 20개 기록 표시 · 전체 기록은 서버에 보존됩니다.</p>'
    return body+'</details>'


def backup_status(c,clock=time.time):
    current=clock();enabled=os.getenv('SHOP_BACKUP_ENABLED')=='1'
    try:interval=int(os.getenv('SHOP_BACKUP_INTERVAL_SECONDS','86400'))
    except ValueError:interval=86400
    interval=max(60,min(604800,interval));grace=max(300,interval//10)
    latest=c.execute('SELECT * FROM shop_backup_runs ORDER BY id DESC LIMIT 1').fetchone()
    good=c.execute("SELECT * FROM shop_backup_runs WHERE state='verified' ORDER BY id DESC LIMIT 1").fetchone()
    warnings=[]
    if not enabled:warnings.append('자동 로컬 백업 꺼짐')
    if not good:warnings.append('복구 검증을 통과한 로컬 백업 없음')
    elif current-(good['finished'] or good['started'])>interval+grace:warnings.append('로컬 백업 예정 간격 초과 · 누락 확인 필요')
    if latest and latest['state']=='failed':warnings.append('최근 로컬 백업 실패 · 이전 성공 백업 보존')
    if latest and latest['state']=='running' and current-latest['started']>grace:warnings.append('로컬 백업 장시간 진행 · 프로세스 확인 필요')
    external=c.execute('SELECT verified,mode FROM shop_external_backups ORDER BY verified DESC LIMIT 1').fetchone()
    automatic=c.execute("SELECT verified FROM shop_external_backups WHERE mode='automatic_verified' ORDER BY verified DESC LIMIT 1").fetchone()
    if not automatic:warnings.append('자동 외부 백업 검증 기록 없음 · 수동 보관과 구분')
    else:
        try:age=current-dt.datetime.fromisoformat(automatic['verified']).timestamp()
        except (ValueError,TypeError):age=float('inf')
        if age>interval+grace:warnings.append('자동 외부 백업 예정 간격 초과')
    return {'warnings':warnings,'last_local':dt.datetime.fromtimestamp(good['finished'] or good['started'],dt.timezone.utc).isoformat() if good else '없음','last_external':external['verified'] if external else '없음','external_mode':external['mode'] if external else '없음','interval':interval,'grace':grace}


def backups(c):
    status=backup_status(c)
    return '<section class="backup-health"><h2>백업 누락·실패 안내</h2><p>백업 주기 '+str(status['interval'])+'초 · 지연 허용 '+str(status['grace'])+'초 · 시각 UTC</p>'+''.join('<p role="status">'+escape(w)+'</p>' for w in status['warnings'])+'<p>최근 로컬 복구 검증: '+escape(status['last_local'])+'</p><p>최근 외부 검증: '+escape(status['last_external']+' · '+status['external_mode'])+'</p><p>이 화면은 저장된 검증 이력을 대조합니다. 파일 존재·저장소 접속 여부는 복구 검증으로 확인하세요. 자동으로 삭제하거나 재실행하지 않습니다.</p></section>'


def product_history(c,product_id):
    rows=c.execute('SELECT * FROM shop_product_changes WHERE product_id=? ORDER BY revision DESC LIMIT 5',(product_id,)).fetchall()
    body='<details class="product-history"><summary>상품 저장 이력 · 최근 5회</summary><p>기능 도입 이후 변경만 기록합니다. 기존 주문·발급 링크의 스냅샷은 유지합니다.</p>'
    for row in rows:
        old,new=json.loads(row['before_details']),json.loads(row['after_details'])
        body+='<h4>리비전 '+str(row['revision'])+' · '+escape(row['created'][:16])+' UTC</h4>'+comparison(old,new)
    return body+'</details>'


def comparison(before,after):
    labels={'title':'상품명','price':'기본 판매가','images':'사진','groups':'옵션 묶음','options':'상품 옵션','greetings':'기본 문구','cash_tax_type':'과세 구분','public':'전시','available':'주문 가능','category':'분류','description':'안내'}
    rows=[]
    for key in sorted(set(before)|set(after)):
        def effective(d):
            value=d.get(key,{'available':True,'public':False}.get(key,''))
            return value.replace('\r\n','\n') if isinstance(value,str) else value
        if effective(before)==effective(after):continue
        def value(d):
            v=effective(d)
            if isinstance(v,bool):text='예' if v else '아니요'
            elif key=='price':text=f'{v:,}원' if isinstance(v,int) else str(v)
            elif key=='groups' and isinstance(v,list):text='\n'.join(g['label']+': '+', '.join(choice['label']+' (+'+str(choice['amount'])+'원)' for choice in g['choices'].values()) for g in v)
            elif isinstance(v,list):text='\n'.join(str(item) for item in v)
            elif key=='cash_tax_type':text={'TG01':'과세','TG02':'면세','':'미설정'}.get(v,str(v))
            else:text=str(v) if v is not None else ''
            return escape(text or '(없음)')
        rows.append('<tr><th>'+escape(labels.get(key,key))+'</th><td>'+value(before)+'</td><td>'+value(after)+'</td></tr>')
    return '<div class="comparison-wrap"><table><thead><tr><th>항목</th><th>변경 전</th><th>변경 후</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>' if rows else '<p>변경 항목 없음</p>'


def timeline(c,row):
    oid=row['id'];groups={label:[] for label in ('결제·취소','제작·정정','배송·배정','알림·영수증')}
    sources=[
        ('결제·취소','SELECT received AS stamp,kind AS label,payment_number||\' · \'||amount||\'원 · 제공 시각 \'||provider_time AS note FROM payment_events WHERE order_id=? ORDER BY received DESC LIMIT 200'),
        ('결제·취소',"SELECT checked AS stamp,'관리자 원거래 대조' AS label,note FROM payment_checks WHERE order_id=? ORDER BY checked DESC LIMIT 200"),
        ('제작·정정',"SELECT checked AS stamp,'제작 검수 · '||decision AS label,reviewer||' · '||note AS note FROM shop_artifact_reviews WHERE order_id=? ORDER BY checked DESC LIMIT 200"),
        ('제작·정정',"SELECT changed AS stamp,'문구 정정 · '||decision AS label,reviewer||' · '||note AS note FROM shop_amendment_events WHERE order_id=? ORDER BY changed DESC LIMIT 200"),
        ('배송·배정',"SELECT changed AS stamp,'배송 · '||stage AS label,actor AS note FROM shop_stage_history WHERE order_id=? ORDER BY changed DESC LIMIT 200"),
        ('배송·배정',"SELECT changed AS stamp,'담당 배정' AS label,name||' · '||note AS note FROM shop_assignment_history WHERE order_id=? ORDER BY changed DESC LIMIT 200"),
        ('알림·영수증',"SELECT created AS stamp,'알림 생성 · '||event AS label,'현재 상태 '||state||' · '||audience AS note FROM shop_messages WHERE order_id=? ORDER BY created DESC LIMIT 200"),
        ('알림·영수증',"SELECT a.checked AS stamp,'알림 결과 대조 · '||a.decision AS label,a.note FROM shop_message_checks a JOIN shop_messages m ON m.id=a.message_id WHERE m.order_id=? ORDER BY a.checked DESC LIMIT 200"),
        ('알림·영수증',"SELECT updated AS stamp,'영수증 상태 갱신' AS label,state||' · '||amount||'원' AS note FROM shop_cash_receipts WHERE order_id=? ORDER BY updated DESC LIMIT 200"),
        ('알림·영수증',"SELECT checked AS stamp,'영수증 대조 · '||decision AS label,note FROM shop_receipt_checks WHERE order_id=? ORDER BY checked DESC LIMIT 200")]
    labels={'paid':'결제 완료','partial':'부분취소','cancel':'승인취소','request_cancel':'요청취소','waiting':'결제 대기','배송 · confirmed':'배송 · 주문 확인','배송 · shipping':'배송 · 배송 중','배송 · delivered':'배송 · 배송 완료'}
    def stamp(value):
        try:
            parsed=dt.datetime.fromisoformat(value)
            if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=9)))
            return parsed.timestamp()
        except (ValueError,TypeError):return 0
    for category,sql in sources:
        for item in c.execute(sql,(oid,)):
            groups[category].append((item['stamp'],labels.get(item['label'],item['label']),item['note']))
    body='<div class="order-timeline"><h3>업무별 주문 이력</h3><p>통보 수신·업무 기록 시각의 최신순 · 항목별 최근 200건. 알림은 생성 시각과 현재 상태를 표시하며 실제 수신 시각과 구분합니다.</p>'
    for category,events in groups.items():
        body+='<details><summary>'+category+' · '+str(min(len(events),200))+'건</summary>'
        for when,label,note in sorted(events,key=lambda e:stamp(e[0]),reverse=True)[:200]:body+='<p><time>'+escape(when)+'</time> · <strong>'+escape(label)+'</strong><br>'+escape(note or '')+'</p>'
        if not events:body+='<p>기록 없음</p>'
        body+='</details>'
    return body+'</div>'
