"""Inspect read-only native form cells; do not evaluate formulas, write or send."""
import re
import hashlib,json
from shipping_formula_proposal import proposal

def inspect_form(payload,spreadsheet_id,sheet_id,order_row,expected_details=None):
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
    labels={8:{'배송일시:'},9:{'주문작품:','작품:'},10:{'상품구분:','규격:'},11:{'배송장소:','장소:'},12:{'받는분:'},13:{'보내시는분:','보내는분:'},15:{'문구:'},16:{'사진보낼곳:','사진:'}}
    if any(''.join(display(row,2).split()).replace('：',':') not in labels[row] for row in required_rows):raise ValueError('Native form labels changed; review the schema')
    formula=cell(8,3).get('userEnteredValue',{}).get('formulaValue','')
    hazards=[]
    if re.search(r'액자화환!\$?C(?:[0-9"&]|$)',formula,re.I):
        hazards.append({'code':'event_clock_in_shipping_label','message':'배송 일시 수식이 행사시간 열을 참조합니다. 배송시각과 분리 검토 필요'})
    elif formula==proposal():
        if expected_details is None:hazards.append({'code':'verified_order_missing','message':'원장에 확인된 현재 주문과 대조해야 합니다.'})
    elif formula:
        hazards.append({'code':'shipping_formula_unverified','message':'배송 일시 수식의 배송시각 원본을 별도 검증해야 합니다.'})
    else:hazards.append({'code':'shipping_formula_missing','message':'배송 일시 수식 또는 데이터 연결 확인 필요'})
    if any('errorValue' in value.get('effectiveValue',{}) for value in cells.values()):
        hazards.append({'code':'sheet_formula_error','message':'주문서 수식 오류 확인 필요'})
    if expected_details is not None:
        from shipping_formula_proposal import model
        details=expected_details
        from order_assistance import present
        if any(not present(details.get(key)) for key in ('sender','recipient','event_type','artwork','size')) or not any(present(details.get(key)) for key in ('venue','address')):
            hazards.append({'code':'core_fields_missing','message':'최종 주문서의 필수 항목을 확인해야 합니다.'})
        expected={8:model(details.get('delivery_date'),details.get('notes','')),9:details.get('artwork'),10:details.get('size'),11:details.get('location') or ' / '.join(filter(None,(details.get('venue'),details.get('address')))),12:details.get('recipient'),13:details.get('sender'),15:details.get('message'),16:details.get('photo_phone')}
        for row,value in expected.items():
            value=str(value or '미확인').replace('\r\n','\n')
            if display(row,3).replace('\r\n','\n')!=value:hazards.append({'code':'field_mismatch','message':'주문서 표시값과 확인된 원장 정보가 다릅니다.'});break
        if not details.get('delivery_date') or not details.get('delivery_time') or expected[8]!=details['delivery_date'][5:].replace('-','.')+' '+details['delivery_time']:
            hazards.append({'code':'explicit_shipping_missing_or_conflicting','message':'명시된 배송일·배송시각과 주문서 값을 확인해야 합니다.'})
        if details.get('event_type') and '<'+details['event_type']+'>' not in display(6,2):
            hazards.append({'code':'event_type_mismatch','message':'주문서 행사 구분이 다릅니다.'})
    lines=['주문번호 '+str(order_row),display(6,2)]
    for row in required_rows:lines.append(display(row,2)+' '+(display(row,3) or '미확인'))
    return {'text':'\n'.join(lines),'ready_for_final_share':not hazards,'hazards':hazards,
            'order_row':order_row,'details_digest':hashlib.sha256(json.dumps(expected_details,sort_keys=True,ensure_ascii=False).encode()).hexdigest() if expected_details is not None else None,
            'source':'native formatted values / first form block only; not a visual clipboard copy',
            'formula_count':sum('formulaValue' in c.get('userEnteredValue',{}) for c in cells.values()),
            'duplicate_block_detected':bool(cell(5,9).get('userEnteredValue',{}).get('formulaValue'))}

def aggregate(result):
    return {key:result[key] for key in ('ready_for_final_share','hazards','source','formula_count','duplicate_block_detected')}
