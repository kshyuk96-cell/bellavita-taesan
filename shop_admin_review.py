"""Read-only admin search assistance, contact display and bounded filtered CSV."""
import csv
from html import escape
import io
import json
import re

CSV_FIELDS=[('order_id','주문번호'),('created','접수 일시'),('event_time','행사 일시'),('title','상품·행사'),('buyer','주문자'),('phone','휴대폰(가림)'),('sender','보내는 분'),('message','직접 입력 문구'),('venue','행사장'),('address','설치 주소'),('amount','원주문금액'),('state','결제 상태'),('stage','배송 단계')]


def selected_columns(fields):
    allowed=[key for key,_ in CSV_FIELDS]
    if 'columns' not in fields:return allowed
    raw=fields['columns']
    if len(raw)>300:raise ValueError('내보낼 항목을 확인해 주세요.')
    selected=raw.split(',')
    if 'order_id' not in selected or len(set(selected))!=len(selected) or any(key not in allowed for key in selected):
        raise ValueError('주문번호를 포함해 올바른 내보내기 항목을 선택해 주세요.')
    return [key for key in allowed if key in selected]


def normalized(value):
    return re.sub(r'[-\s()]','',str(value)).casefold()


def highlight(value,term):
    value=str(value);needle=normalized(term)
    if not needle:return escape(value)
    letters=[];positions=[]
    for index,char in enumerate(value):
        part=normalized(char)
        letters.extend(part);positions.extend([index]*len(part))
    haystack=''.join(letters);spans=[];start=0
    while (found:=haystack.find(needle,start))>=0:
        spans.append((positions[found],positions[found+len(needle)-1]+1));start=found+len(needle)
    output=[];cursor=0
    for start,end in spans:
        if start<cursor:continue
        output.extend((escape(value[cursor:start]),'<mark class="order-match">'+escape(value[start:end])+'</mark>'));cursor=end
    return ''.join(output)+escape(value[cursor:])


def masked(phone):
    value=re.sub(r'[\s-]','',phone)
    return value[:3]+'-****-'+value[-4:] if len(value)>=10 else '연락처 확인'


def phone_control(phone):
    return '<span class="admin-phone" data-phone="'+escape(phone)+'"><span class="phone-value">'+escape(masked(phone))+'</span> <button type="button" class="phone-toggle" aria-expanded="false">전화번호 확인</button></span>'


def installation(order,order_id):
    text='\n'.join(('주문번호 '+order_id,'행사 일시 '+order['event_time'][:16].replace('T',' '),'장소 '+order['venue'],'주소 '+order['address']))
    return '<div class="installation-copy"><button type="button" data-install-copy="'+escape(text)+'">설치 정보 복사</button><label>설치 정보 · 연락처·내부 메모 제외<textarea class="installation-source" readonly>'+escape(text)+'</textarea></label><p class="installation-copy-status" role="status"></p></div>'


def status_badge(state,queues):
    if state=='cancelled':kind,text='cancelled','취소 · 제작·배송 보류'
    elif state=='partial_cancel':kind,text='hold','부분취소 · 잔액·재개 조건 확인'
    elif state in ('requesting','failed'):kind,text='hold','결제 대조 필요 · 새 결제 요청 금지'
    elif 'production' in queues:kind,text='hold','제작 보류·검수 확인'
    else:return ''
    return '<span class="order-badge order-badge-'+kind+'">'+text+'</span>'


def search_assistance(path,meta):
    import shop_admin_queries as queries
    f=meta['filters']
    labels={'q':'검색어','order':'주문번호','state':'결제 상태','stage':'배송 상태','attention':'확인 항목','from_date':'행사 시작일','to_date':'행사 종료일','task':'업무 상태','task_owner':'담당자','task_priority':'우선순위','sort':'정렬'}
    choices={'state':queries.STATES,'stage':queries.STAGES,'attention':queries.ATTENTION,'sort':queries.SORTS,'task':{'open':'미처리','overdue':'기한 초과','soon':'24시간 이내','done':'완료'},'task_priority':{'high':'높음','normal':'보통','low':'낮음'}}
    chips='<nav class="active-order-filters" aria-label="적용 중인 검색 조건">'
    for key,label in labels.items():
        if not f.get(key):continue
        clean=dict(f);clean[key]='';clean['page']='1';clean['expanded']=''
        # Removing one end of a date interval keeps the remaining valid bound.
        text=choices.get(key,{}).get(f[key],f[key])
        chips+='<a href="'+escape(path+'?'+queries.query(clean))+'#admin-orders" aria-label="'+escape(label+' 해제')+'">'+escape(label+' · '+text)+' ×</a>'
    chips+='</nav>'
    help='<details class="order-search-help"><summary>주문 검색 사용 예시</summary><ul><li>이름·문구 일부 또는 정확한 주문번호로 검색합니다.</li><li>휴대폰은 공백·하이픈 없이도 찾습니다. 검색 결과의 연락처는 기본 가림 표시입니다.</li><li>행사일과 배송 중을 함께 선택하면 해당 행사에 설치할 주문을 찾습니다. 날짜는 접수일이 아닌 행사일입니다.</li><li>행사 임박순은 앞으로 열릴 행사부터, 지난 행사는 그 뒤 최근순으로 표시합니다.</li><li>강조는 표시된 이름·문구·상품 등에만 적용하며 가린 전화번호를 드러내지 않습니다.</li></ul></details>'
    export='<p><a class="filtered-order-export" data-export-count="'+str(meta['total'])+'" data-export-fields="'+escape(json.dumps(CSV_FIELDS,ensure_ascii=False))+'" href="'+escape(path+'/orders.csv?'+queries.query(f,page='',expanded=''))+'">검색 결과 전체 CSV 받기</a> · 조건·건수와 항목을 확인한 뒤 받습니다. 최대 10,000건, 연락처 일부 가림 · 고객정보가 포함되므로 사용 후 관리하세요.</p>'
    empty=''
    if meta['total']==0:
        clean=dict(f,from_date='',to_date='',page='1',expanded='')
        empty='<aside class="empty-order-help"><p>검색 조건을 조금씩 줄여 보세요. 행사일은 접수일과 다릅니다.</p><a href="'+escape(path+'?'+queries.query(clean))+'#admin-orders">행사일 조건 없이 다시 찾기</a> · <a href="'+escape(path)+'#admin-orders">모든 조건 초기화</a></aside>'
    return chips+help+export+empty


def csv_cell(value):
    value=str(value)
    return "'"+value if value.lstrip().startswith(('=','+','-','@','\t','\r')) else value


def export_csv(c,fields):
    import shop_admin_queries as queries
    selected=selected_columns(fields)
    f,sql,args=queries.parts(fields);order,order_args=queries.ordering(f)
    rows=c.execute('SELECT o.*,f.stage'+sql+' ORDER BY '+order+' LIMIT 10001',[*args,*order_args]).fetchall()
    if len(rows)>10000:raise ValueError('내보낼 주문이 10,000건을 초과합니다. 검색 조건을 좁혀 주세요.')
    stream=io.StringIO(newline='');writer=csv.writer(stream)
    labels=dict(CSV_FIELDS);writer.writerow([labels[key] for key in selected])
    for row in rows:
        data=json.loads(row['details'])
        values=(row['id'],row['created'],data['event_time'],data['title'],data['buyer'],masked(data['buyer_phone']),data['sender'],data['message'],data['venue'],data['address'],data['amount'],queries.STATES.get(row['state'],row['state']),queries.STAGES.get(row['stage'] or '','미확인'))
        record=dict(zip((key for key,_ in CSV_FIELDS),values))
        writer.writerow([csv_cell(record[key]) for key in selected])
    return stream.getvalue().encode('utf-8-sig'),'shop-search-orders.csv'
