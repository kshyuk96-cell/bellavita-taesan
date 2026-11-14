"""Write only confirmed order fields and preserve the sheet's existing validation and formulas."""
import re
import math
from datetime import date, datetime
from urllib.parse import quote

import order_intake
from delivery_regions import delivery_alert

EPOCH = date(1899, 12, 30)
HEADER = ['주문일시', '배송일', '행사시간', '작품', '액자 사이즈(호수)', '설치장소', '행사 구분', '주문 채널', '배송 채널', '비고', '받는분']
FIELDS = {0: 'created', 1: 'delivery_date', 2: 'event_time', 3: 'artwork', 4: 'size', 5: 'location', 6: 'event_type',
          8: 'channel', 9: 'notes', 10: 'recipient', 11: 'photo_phone', 15: 'sender', 17: 'message', 19: 'source', 28: 'uuid'}

class VerificationError(ValueError):
    def __init__(self,field):
        self.field=field
        super().__init__('원장 저장값이 달라 등록 완료를 보류했어요. 항목: '+field)



def populated(row, index):
    return row[index] if index < len(row) else None


def dropdown(cell):
    condition = cell.get('dataValidation', {}).get('condition', {})
    if condition.get('type') != 'ONE_OF_LIST':
        return set()
    return {value['userEnteredValue'] for value in condition['values'] if value.get('userEnteredValue')}


def channel(address, choices, mapping):
    place = order_intake.region(address)
    if not place:
        return ''
    candidates = {row[0] for row in mapping if len(row) >= 4 and str(row[3]).upper() == 'TRUE'
                  and row[0] in choices and (str(row[1]).startswith(place + '센터') or str(row[0]).startswith(place))}
    return next(iter(candidates)) if len(candidates) == 1 else ''


def details_for_sheet(details, cells, mapping):
    data = dict(details)
    data['location'] = ' / '.join(dict.fromkeys(filter(None, (data.get('venue'), data.get('address')))))
    data['channel'] = channel(data.get('address', ''), dropdown(cells[8]), mapping)
    if data.get('event_type') not in dropdown(cells[6]):
        data['event_type'] = ''
    if data.get('size') not in dropdown(cells[4]):
        data['size'] = ''
    for key in ('delivery_date', 'event_date'):
        try:
            data[key] = date.fromisoformat(data.get(key, '')).isoformat()
        except (ValueError, TypeError):
            data[key] = ''
    for key in ('delivery_time', 'event_time'):
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', data.get(key) or ''):
            data[key] = ''
    missing = [label for key, label in (('delivery_date', '배송일'), ('delivery_time', '배송시각'),
               ('artwork', '작품'), ('size', '규격'), ('channel', '배송채널'), ('location', '설치장소'),
               ('recipient', '받는 분'), ('event_type', '행사 구분')) if not data.get(key)]
    event = ' '.join(filter(None, (data['event_date'], data['event_time'])))
    delivery_time = data['delivery_time']
    data['notes'] = ' / '.join(filter(None, (data.get('note'), f'행사 {event}' if event else '',
                                           f'배송시각 {delivery_time}' if delivery_time else '',
                                           '·'.join(missing) + ' 확인 필요' if missing else '')))
    alert = delivery_alert(data['location'])
    data['delivery_region'] = alert['region']
    data['regional_alert'] = alert['message']
    if alert['message']:
        data['notes'] = ' / '.join(filter(None, (data['notes'], alert['message'])))
    return data


def stored_value(key,value):
    if value and key=='delivery_date':return (date.fromisoformat(value)-EPOCH).days
    if value and key=='event_time':
        hour,minute=map(int,value.split(':'));return (hour*60+minute)/1440
    return value if value is not None else ''


def same_value(actual,expected):
    if actual is None:actual=''
    if type(expected) in (int,float):
        return type(actual) in (int,float) and math.isclose(actual,expected,rel_tol=0,abs_tol=1e-9)
    return str(actual)==str(expected)


class Ledger:
    def __init__(self, session, spreadsheet_id):
        self.session = session
        self.url = 'https://sheets.googleapis.com/v4/spreadsheets/' + spreadsheet_id

    def values(self, rng):
        response = self.session.get(self.url + '/values/' + quote(rng, safe=''),
                                    params={'valueRenderOption': 'UNFORMATTED_VALUE'}, timeout=15)
        response.raise_for_status()
        return response.json().get('values', [])

    def find(self, marker):
        ids = self.values("'액자화환'!AC5:AC")
        matches = [i for i, row in enumerate(ids, 5) if row and row[0] == marker]
        if len(matches) > 1:
            raise ValueError('같은 주문 번호가 원장에 여러 번 있어 확인이 필요해요.')
        return matches[0] if matches else None

    def row_cells(self, row):
        response = self.session.get(self.url, params={'ranges': f"'액자화환'!A{row}:AC{row}",
            'fields': 'sheets(properties(sheetId),data(rowData(values(userEnteredValue,dataValidation))))'}, timeout=15)
        response.raise_for_status()
        sheet = response.json()['sheets'][0]
        if sheet['properties']['sheetId'] != 0:
            raise ValueError('액자화환 원장의 설정을 확인해 주세요.')
        return (sheet['data'][0].get('rowData', [{}])[0].get('values', []) + [{}] * 29)[:29]

    def save(self, marker, details, created, *, update=False):
        if self.values("'액자화환'!A4:K4") != [HEADER]:
            raise ValueError('액자화환 원장 제목이 바뀌어 주문을 보류했어요.')
        row = self.find(marker)
        if update and not row:
            raise ValueError('연결된 원장 주문을 찾지 못했어요.')
        existing = row is not None
        if not existing:
            row = 5 + len(self.values("'액자화환'!A5:A"))
        cells = self.row_cells(row)
        if not existing:
            for i, cell in enumerate(cells):
                value = cell.get('userEnteredValue', {})
                if value and (i in FIELDS or not ('formulaValue' in value or value == {'boolValue': False})):
                    raise ValueError('다음 주문 행에 기존 내용이 있어 등록을 보류했어요.')
            if not all(dropdown(cells[index]) for index in (4, 6, 8)):
                raise ValueError('다음 주문 행의 선택 목록을 확인해 주세요.')
        elif cells[28].get('userEnteredValue', {}).get('stringValue') != marker:
            raise ValueError('연결된 원장 주문의 위치를 다시 확인해 주세요.')
        mapping = self.values("'배송채널DB'!A2:D200")
        data = details_for_sheet(details, cells, mapping)
        data.update(created=(datetime.fromisoformat(created).date() - EPOCH).days, uuid=marker)
        fields = FIELDS.items() if not existing or update else ()
        requests = []
        for column, key in fields:
            if update and column in (0, 2, 28):  # Staff edits do not include the event time.
                continue
            value = data.get(key)
            if key == 'event_time' and not value:
                continue
            if cells[column].get('userEnteredValue', {}).get('formulaValue'):
                raise ValueError('원장 수식이 있는 칸을 확인해 주세요.')
            if value and key == 'delivery_date':
                value = (date.fromisoformat(value) - EPOCH).days
            if value and key == 'event_time':
                hour, minute = map(int, value.split(':'))
                value = (hour * 60 + minute) / 1440
            entered = ({'numberValue': value} if type(value) in (int, float) else {'stringValue': str(value)}) if value is not None and value != '' else {}
            if not entered and not update:
                continue
            requests.append({'updateCells': {'start': {'sheetId': 0, 'rowIndex': row - 1, 'columnIndex': column},
                'rows': [{'values': [{'userEnteredValue': entered} if entered else {}]}], 'fields': 'userEnteredValue'}})
        if requests:
            response = self.session.post(self.url + ':batchUpdate', json={'requests': requests}, timeout=20)
            response.raise_for_status()
        verified = self.find(marker)
        saved = self.values(f"'액자화환'!A{verified}:AC{verified}") if verified else []
        if not saved:raise ValueError('원장 등록 결과를 확인하지 못했어요.')
        for column,key in FIELDS.items():
            if update and column in (0,2,28):continue
            expected=stored_value(key,data.get(key))
            if not update and expected=='':continue
            if not same_value(populated(saved[0],column),expected):
                raise VerificationError(key)
        return verified, data
