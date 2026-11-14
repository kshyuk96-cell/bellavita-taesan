"""Product/event/rice-weight money aggregates reconciled to order settlement."""
from collections import defaultdict
from html import escape
import io
import json
import re
from openpyxl import Workbook
from openpyxl.styles import Font
import shop_settlement

METRICS=['주문수','승인 주문수','취소 반영 주문수','원주문금액','승인원금','취소누계','잔액','미결제 주문금액']


def aggregate(c,fields):
    criteria={k:v for k,v in [('settlement_from',fields.get('sales_from')),('settlement_to',fields.get('sales_to'))] if v}
    start,end,money=shop_settlement.entries(c,criteria);by_id={r[0]:r for r in money};groups={k:defaultdict(lambda:[0]*8) for k in ('product','event','weight')}
    for row in c.execute('SELECT o.*,s.product_id,p.details AS product_details FROM invitation_orders o JOIN shop_offers s ON s.id=o.job LEFT JOIN shop_products p ON p.id=s.product_id WHERE substr(o.created,1,10) BETWEEN ? AND ?',(str(start),str(end))):
        if row['id'] not in by_id:continue
        entry=by_id[row['id']];data=json.loads(row['details']);snapshot=data.get('product_snapshot') or {}
        title=json.loads(row['product_details'])['title'] if row['product_details'] else row['product_id']
        product=row['product_id']+' · '+title+' (현재 상품명)'
        event=(snapshot.get('event_name') or '행사명 미분리 · '+data['title'])+' · '+data['event_time'][:16]+' · '+data['venue']
        weight=dict(data['options']).get('상품 선택','');match=re.search(r'(?<!\d)(\d+(?:\.\d+)?)\s*(?:kg|㎏|키로|킬로)',weight,re.I)
        weight=match[1]+'kg' if match else '중량 확인 필요'
        values=[1,int(entry[10]>0),int(entry[11]>0),*entry[9:14]]
        for axis,key in (('product',product),('event',event),('weight',weight)):
            groups[axis][key]=[a+b for a,b in zip(groups[axis][key],values)]
    expected=[len(money),sum(r[10]>0 for r in money),sum(r[11]>0 for r in money),*[sum(r[index] for r in money) for index in range(9,14)]]
    for grouped in groups.values():
        totals=[sum(row[i] for row in grouped.values()) for i in range(8)]
        if totals!=expected:raise ValueError('판매 집계와 주문 정산 합계가 일치하지 않습니다.')
    return {'start':str(start),'end':str(end),'groups':{axis:dict(sorted(values.items())) for axis,values in groups.items()},'totals':expected,'reconciled':True}


def workbook(c,fields):
    result=aggregate(c,fields);book=Workbook();book.remove(book.active)
    for axis,title in [('product','상품 집계'),('event','행사 집계'),('weight','중량 집계')]:
        sheet=book.create_sheet(title);sheet.append(['구분']+METRICS)
        for key,values in result['groups'][axis].items():sheet.append([key]+values)
        sheet.append(['합계']+result['totals']);sheet.freeze_panes='B2';sheet.auto_filter.ref=sheet.dimensions;sheet.column_dimensions['A'].width=60
        for cell in sheet[1]:cell.font=Font(bold=True)
        for row in sheet.iter_rows(min_row=2):
            for cell in row[4:]:cell.number_format='#,##0'
    notes=book.create_sheet('대조 기준');notes.append(['접수일 범위',result['start']+' ~ '+result['end']]);notes.append(['주문 정산 합계 대조','일치'])
    notes.append(['금액 기준','접수일 기준 주문의 내보내기 시점 승인·취소·잔액. 거래일 매출표가 아닙니다.'])
    notes.append(['상품 기준','상품 ID로 묶으며 표시명은 현재 상품명입니다. 원주문 스냅샷은 수정하지 않습니다.'])
    notes.append(['행사 기준','신규 링크의 행사명·일시·장소. 이전 링크에 분리된 행사명이 없으면 원문 링크명을 그대로 표시합니다.'])
    notes.append(['중량 기준','주문 당시 상품 선택. 부분취소 금액으로 실제 제작 수량·쌀 재고를 추정하지 않습니다.'])
    notes.append(['연결 범위','조회·Excel 사본만 생성하며 운영 시트·재고·결제 상태에 쓰지 않습니다.'])
    for sheet in book:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value,str):cell.value=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',cell.value);cell.data_type='s'
    output=io.BytesIO();book.save(output);return output.getvalue(),f'shop-sales-{result["start"]}-{result["end"]}.xlsx'


def panel(c,path,fields):
    start,end=shop_settlement.filters({k:v for k,v in [('settlement_from',fields.get('sales_from')),('settlement_to',fields.get('sales_to'))] if v})
    result=f'<section><h2>상품·행사·중량별 판매 집계</h2><form method="get" action="{escape(path)}"><input type="hidden" name="sales_view" value="yes"><label>접수 시작일<input type="date" name="sales_from" value="{start}" required></label><label>접수 종료일<input type="date" name="sales_to" value="{end}" required></label><button>판매 집계 조회</button></form>'
    result+=f'<p><a href="{escape(path)}/sales.xlsx?sales_from={start}&amp;sales_to={end}">판매 집계 Excel 사본</a></p>'
    if fields.get('sales_view')=='yes':
        data=aggregate(c,fields);result+='<p>주문 정산 합계 일치 · 승인 '+format(data['totals'][4],',')+'원 · 취소 '+format(data['totals'][5],',')+'원 · 잔액 '+format(data['totals'][6],',')+'원</p>'
        for axis,label in [('product','상품'),('event','행사'),('weight','중량')]:
            result+='<details><summary>'+label+'별 집계</summary>'
            for key,values in list(data['groups'][axis].items())[:200]:result+='<p>'+escape(key)+f' · {values[0]}주문 · 승인 {values[4]:,}원 · 취소 {values[5]:,}원 · 잔액 {values[6]:,}원</p>'
            result+='<p>화면은 구분별 최대 200개이며 Excel은 전체입니다.</p></details>'
    return result+'</section>'
