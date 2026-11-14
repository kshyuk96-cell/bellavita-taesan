"""Read-only order-period settlement workbook; never writes a live ledger."""
from collections import defaultdict
from datetime import datetime,timedelta
from html import escape
import io
import json
import re

from openpyxl import Workbook
from openpyxl.styles import Font,PatternFill
from openpyxl.utils import get_column_letter
import shop_dispatch

HEADERS=['주문번호','접수 일시(KST)','행사 일시(KST)','상품·행사','주문자','보내는 분','배송 담당',
         '결제 상태','배송 단계','원주문금액','승인원금','취소누계','잔액','미결제 주문금액',
         '페이앱 원결제번호','원장 고유참조 수','원장 참조','원장 중복·불일치 확인','점검 메모']


def filters(fields):
    today=datetime.now(shop_dispatch.SEOUL).date()
    clean=dict(fields)
    clean.setdefault('settlement_from',(today-timedelta(days=30)).isoformat())
    clean.setdefault('settlement_to',today.isoformat())
    return shop_dispatch.period(clean,'settlement',maximum=366,today=today)


def refs(c):
    orders=defaultdict(set);shared=defaultdict(set)
    for row in c.execute("SELECT h.order_id,h.receipt FROM shop_handoff h JOIN invitation_orders o ON o.id=h.order_id JOIN shop_offers s ON s.id=o.job WHERE h.state='acked' AND h.receipt IS NOT NULL"):
        try:reference=json.loads(row['receipt'])['ledger']
        except (ValueError,KeyError,TypeError):continue
        if isinstance(reference,str):orders[row['order_id']].add(reference);shared[reference].add(row['order_id'])
    return orders,shared


def entries(c,fields):
    start,end=filters(fields);references,shared=refs(c)
    rows=c.execute('''SELECT o.*,f.stage,a.name AS assignee FROM invitation_orders o JOIN shop_offers s ON s.id=o.job
        LEFT JOIN shop_fulfillment f ON f.order_id=o.id LEFT JOIN shop_assignments a ON a.order_id=o.id
        WHERE substr(o.created,1,10) BETWEEN ? AND ? ORDER BY o.created,o.id LIMIT 10001''',(str(start),str(end))).fetchall()
    if len(rows)>10000:raise ValueError('정산 주문이 10,000건을 초과합니다. 기간을 줄여 주세요.')
    result=[]
    for row in rows:
        data=json.loads(row['details']);events=c.execute('SELECT kind,amount FROM payment_events WHERE order_id=?',(row['id'],)).fetchall()
        kinds={r['kind'] for r in events};original=data['amount']
        approved=original if row['state'] in ('paid','partial_cancel') or kinds.intersection({'paid','partial','cancel'}) else 0
        refunded=original if 'cancel' in kinds else sum(r['amount'] for r in events if r['kind']=='partial')
        # A request cancellation (8/32) before approval is not a monetary refund.
        remaining=approved-refunded;memo=[]
        if not 0<=refunded<=approved:memo.append('승인·취소 금액 대조 필요')
        if row['state']=='cancelled' and not kinds.intersection({'cancel','request_cancel'}):memo.append('이전 취소 내역 대조 필요')
        order_refs=references[row['id']]
        duplicate=len(order_refs)>1 or any(len(shared[ref])>1 for ref in order_refs)
        if duplicate:memo.append('다른 주문과 공유하거나 복수인 원장 참조 확인')
        result.append([row['id'],row['created'][:19],data['event_time'][:19],data['title'],data['buyer'],data['sender'],
                       row['assignee'] or '미배정',row['state'],row['stage'] or '결제 대기',original,approved,refunded,remaining,
                       original if not approved and row['state'] not in ('cancelled',) else 0,row['payment_number'] or '',
                       len(order_refs),'\n'.join(sorted(order_refs)),'확인 필요' if duplicate else '중복 참조 없음' if order_refs else '접수 미확인','; '.join(memo)])
    return start,end,result


def workbook(c,fields):
    start,end,rows=entries(c,fields);book=Workbook();sheet=book.active;sheet.title='주문정산';sheet.append(HEADERS)
    for row in rows:sheet.append(row)
    # Every user-controlled string is explicitly a string, never an Excel formula.
    for row in sheet:
        for cell in row:
            if isinstance(cell.value,str):
                cell.value=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',cell.value);cell.data_type='s'
    sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
    for cell in sheet[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='284D39')
    for i in range(1,len(HEADERS)+1):sheet.column_dimensions[get_column_letter(i)].width=24 if i not in (4,17,19) else 45
    for row in sheet.iter_rows(min_row=2):
        for cell in row[9:14]:cell.number_format='#,##0'
    summary=book.create_sheet('정산기준·합계')
    summary.append(['항목','값']);summary.append(['접수일 시작(KST)',str(start)]);summary.append(['접수일 종료(KST)',str(end)])
    summary.append(['집계 기준','선택 접수일의 쇼핑몰 주문; 취소누계·잔액은 내보내기 시점까지의 전체 반영값'])
    summary.append(['거래일 기준 매출 아님','선택 기간 이후 취소도 해당 주문의 현재 상태에 포함됩니다.'])
    summary.append(['원장 점검 범위','서버 ACK의 고유 원장 참조만 대조합니다. 실제 운영 시트의 행 중복을 검증한 결과가 아닙니다.'])
    summary.append(['주문 수',len(rows)])
    for index,label in ((9,'원주문금액 합계'),(10,'승인원금 합계'),(11,'취소누계 합계'),(12,'잔액 합계'),(13,'미결제 주문금액 합계')):
        summary.append([label,sum(row[index] for row in rows)])
    summary.column_dimensions['A'].width=30;summary.column_dimensions['B'].width=110
    output=io.BytesIO();book.save(output);return output.getvalue(),f'shop-settlement-{start}-{end}.xlsx'


def panel(path,fields):
    start,end=filters(fields)
    return ('<section><h2>기간별 주문 정산 Excel</h2><p>접수일 기준 주문과 내보내기 시점의 취소누계·잔액을 대조합니다. 운영 시트에 쓰지 않습니다.</p>'
            +f'<form method="get" action="{escape(path)}/settlement.xlsx"><label>접수 시작일<input type="date" name="settlement_from" value="{start}" required></label>'
            +f'<label>접수 종료일<input type="date" name="settlement_to" value="{end}" required></label><button>정산 Excel 다운로드</button></form></section>')
