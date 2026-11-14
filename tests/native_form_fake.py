"""Synthetic native cell payload; never fetches or changes a real Sheet."""
from shipping_formula_proposal import model,proposal

def native_payload(number,details,old_clock=False):
    rows=[{} for _ in range(16)]
    def put(row,col,value,**extra):
        values=rows[row-1].setdefault('values',[])
        while len(values)<col:values.append({})
        values[col-1]={'formattedValue':str(value),**extra}
    put(5,3,number,effectiveValue={'numberValue':number})
    put(6,2,'[테스트 / 실배송 금지] <'+details.get('event_type','')+'>')
    fields={8:('배송 일시:',model(details.get('delivery_date'),details.get('notes'))),9:('주문작품:',details.get('artwork')),10:('상품 구분:',details.get('size')),11:('배송 장소:',details.get('location') or ' / '.join(filter(None,(details.get('venue'),details.get('address'))))),12:('받는 분:',details.get('recipient')),13:('보내시는 분:',details.get('sender')),15:('문구 :',details.get('message')),16:('사진 보낼 곳 :',details.get('photo_phone'))}
    for row,(label,value) in fields.items():put(row,2,label);put(row,3,value or '미확인')
    rows[7]['values'][2]['userEnteredValue']={'formulaValue':'=TEXT(INDIRECT("액자화환!C"&$C$5),"hh:mm")' if old_clock else proposal()}
    return {'spreadsheetId':'synthetic','sheets':[{'properties':{'sheetId':5},'data':[{'rowData':rows}]}]}
