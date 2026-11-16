"""Explicit offline adapter for the real Bellavita order-sheet normalization code.

No Ledger session is created. Sheet transport, UI, writes and payments are unmeasured.
"""
from .universal_qa import Adapter, Risk, Scenario

DATES = [('2024-02-29','2024-02-29'), ('2025-02-29',''), ('2026-11-16','2026-11-16'),
         ('2026-01-01','2026-01-01'), ('2026-12-31','2026-12-31'), ('',''),
         ('invalid',''), (None,''), (12,'')]
TIMES = [('00:00','00:00'), ('23:59','23:59'), ('12:30','12:30'), ('24:00',''),
         ('23:60',''), ('1:00',''), ('',''), (None,'')]
ADDRESSES = [('부산 해운대구','부산'), ('대구 중구','대구'), ('알수없는지역',''), ('','')]


def adapter():
    def execute(inputs):
        import order_ledger
        cells = [{} for _ in range(29)]
        for index, choices in ((4,['10호']), (6,['결혼']), (8,['부산','대구'])):
            cells[index] = {'dataValidation': {'condition': {'type':'ONE_OF_LIST',
                'values':[{'userEnteredValue':v} for v in choices]}}}
        details = dict(delivery_date=inputs['date'], delivery_time=inputs['time'],
                       address=inputs['address'], event_date='', event_time='',
                       size='10호', event_type='결혼', artwork='테스트작품', recipient='합성수신인')
        data = order_ledger.details_for_sheet(details, cells,
                    [['부산','부산센터','','TRUE'], ['대구','대구센터','','TRUE']])
        actual = {k:data[k] for k in ('delivery_date','delivery_time','channel')}
        return {'actual':actual, 'evidence':{'function':'order_ledger.details_for_sheet',
                    'transport':'none', 'data':'synthetic', 'scope':'offline_real_component'}}
    return Adapter('bellavita-order-ledger','offline-v1',execute)


def suite():
    rows = []
    for date, expected_date in DATES:
        for time, expected_time in TIMES:
            for address, expected_channel in ADDRESSES:
                rows.append(Scenario('ledger-'+str(len(rows)), 'bellavita-order-ledger',
                    'order-sheet-normalization', 'ledger-date-time-channel',
                    Risk('P1','잘못된 배송 일정 또는 채널','윤년·시각·지역 경계 입력','주문 수동 대조'),
                    dict(date=date,time=time,address=address),
                    dict(delivery_date=expected_date,delivery_time=expected_time,channel=expected_channel),
                    '고정 달력 예제·24시간 HH:MM·활성 지역 매핑 명세; SUT 호출 없는 기대값',
                    '날짜 유효성·시각 경계·지역 매핑의 연결 조합'))
    return rows
