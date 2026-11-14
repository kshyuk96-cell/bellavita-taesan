"""Inspect read-only native form cells; do not evaluate formulas, write or send."""
import re

def inspect_form(payload,spreadsheet_id,sheet_id,order_row):
    if payload.get('spreadsheetId')!=spreadsheet_id:raise ValueError('Unexpected spreadsheet')
    sheets=[s for s in payload.get('sheets',[]) if s.get('properties',{}).get('sheetId')==sheet_id]
    if len(sheets)!=1:raise ValueError('Native form sheet is missing or ambiguous')
    cells={}
    for block in sheets[0].get('data',[]):
        for ri,row in enumerate(block.get('rowData',[]),block.get('startRow',0)):
            for ci,cell in enumerate(row.get('values',[]),block.get('startColumn',0)):cells[(ri+1,ci+1)]=cell
    def cell(row,col):return cells.get((row,col),{})
    def display(row,col):return str(cell(row,col).get('formattedValue',''))
    actual=cell(5,3).get('effectiveValue',{}).get('numberValue')
    if actual!=order_row:raise ValueError('Form currently points to another ledger row')
    required_rows=(8,9,10,11,12,13,15,16)
    if any(not display(row,2) for row in required_rows):raise ValueError('Expected form labels missing; expand the grounded read')
    formula=cell(8,3).get('userEnteredValue',{}).get('formulaValue','')
    hazards=[]
    if re.search(r'액자화환!\$?C(?:[0-9"&]|$)',formula,re.I):
        hazards.append({'code':'event_clock_in_shipping_label','message':'배송 일시 수식이 행사시간 열을 참조합니다. 배송시각과 분리 검토 필요'})
    elif formula:
        hazards.append({'code':'shipping_formula_unverified','message':'배송 일시 수식의 배송시각 원본을 별도 검증해야 합니다.'})
    else:hazards.append({'code':'shipping_formula_missing','message':'배송 일시 수식 또는 데이터 연결 확인 필요'})
    if any('errorValue' in value.get('effectiveValue',{}) for value in cells.values()):
        hazards.append({'code':'sheet_formula_error','message':'주문서 수식 오류 확인 필요'})
    lines=['주문번호 '+str(order_row),display(6,2)]
    for row in required_rows:lines.append(display(row,2)+' '+(display(row,3) or '미확인'))
    return {'text':'\n'.join(lines),'ready_for_final_share':not hazards,'hazards':hazards,
            'source':'native formatted values / first form block only; not a visual clipboard copy',
            'formula_count':sum('formulaValue' in c.get('userEnteredValue',{}) for c in cells.values()),
            'duplicate_block_detected':bool(cell(5,9).get('userEnteredValue',{}).get('formulaValue'))}

def aggregate(result):
    return {key:result[key] for key in ('ready_for_final_share','hazards','source','formula_count','duplicate_block_detected')}
