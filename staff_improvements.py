"""Read-side helpers; source identity is configured by trusted server operators."""
import duplicate_review
import order_duplicates


def decision_fingerprints(read, first, second, *, immutable_header):
    if not immutable_header:
        raise ValueError('이 원장의 불변 주문 표식 연결이 미확인이어서 경고를 생략하지 않았어요.')
    if type(first) is not int or type(second) is not int or min(first, second) < 5 or first == second:
        raise ValueError('서로 다른 두 원장 행을 확인해 주세요.')
    ranges = ["'액자화환'!AC4:AC4"]
    for row in (first, second):
        ranges.extend((f"'액자화환'!B{row}:C{row}", f"'액자화환'!F{row}:F{row}",
                       f"'액자화환'!Q{row}:Q{row}", f"'액자화환'!AC{row}:AC{row}"))
    values = read(ranges)
    if len(values) != 9 or values[0] != [[immutable_header]]:
        raise ValueError('원본 주문 표식 열을 검증하지 못해 경고를 유지했어요.')
    snapshots = []
    keys = []
    for index, row in enumerate((first, second)):
        schedule, place, sender, marker = values[1+index*4:5+index*4]
        def cell(items, col=0):
            return items[0][col] if items and len(items[0]) > col else ''
        snapshot = dict(row_number=row, delivery_date=cell(schedule), event_time=cell(schedule, 1),
                        location=cell(place), sender_summary=cell(sender), order_id=cell(marker))
        snapshots.append(snapshot)
        key = order_duplicates.key(schedule[0] if schedule else [], place[0] if place else [],
                                   sender[0] if sender else [])
        if key is None:
            raise ValueError('주문 내용이 비었거나 읽히지 않아 판단을 저장하지 않았어요.')
        keys.append((key[0], key[1], key[3]))
    if keys[0] != keys[1] or snapshots[0]['order_id'] == snapshots[1]['order_id']:
        raise ValueError('동일 조건의 서로 다른 주문 표식을 확인하지 못해 경고를 유지했어요.')
    return tuple(duplicate_review.snapshot_fingerprint(snapshot) for snapshot in snapshots)
