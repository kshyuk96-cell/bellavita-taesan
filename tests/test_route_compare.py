import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import agent
import delivery_routes


DAY = '2026-11-22'
SERIAL = (date.fromisoformat(DAY) - delivery_routes.EPOCH).days


def fixture(rows):
    return (
        [['주문번호', '배송일', '행사시간', '배송주소'],
         *[[number, SERIAL, clock, address] for number, clock, address, _, _, _ in rows]],
        [['배송채널'], *[[center] for _, _, _, center, _, _ in rows]],
        [['배송 완료'], *[[checked] for _, _, _, _, checked, _ in rows]],
        [['배송원'], *[[owner] for _, _, _, _, _, owner in rows]],
    )


BEFORE = fixture([(11, .6, '가상 장소 A', '구로', False, ''),
                  (12, .6, '가상 장소 A', '구로', False, ''),
                  (14, .6, '가상 장소 C', '구로', False, '')])
AFTER = fixture([(14, .6, '가상 장소 C', '구로', False, ''),
                 (13, .6, '가상 장소 D', '구로', False, ''),
                 (11, .7, '가상 장소 B', '구로', False, ''),
                 (15, .6, '가상 장소 E', '구로', False, ''),
                 (15, .6, '가상 장소 E', '구로', False, ''),
                 (16, .6, '가상 장소 F', '수원', False, ''),
                 (17, .6, '가상 장소 G', '구로', True, ''),
                 (18, .6, '가상 장소 H', '구로', False, '가상 배송원'),
                 (19, .6, '가상 장소 I', '구로', '', '')])


def saved():
    selected = delivery_routes.board_rows(*BEFORE, [DAY])
    baseline = delivery_routes.board_snapshot(selected, *BEFORE[1:])
    return {'route_report': {DAY: {'routes': [], 'missed': [], 'held': 0}}, 'route_board': baseline}


class RouteCompareTests(unittest.TestCase):
    def test_diff_excludes_reordering_and_does_not_guess_new_order(self):
        baseline = saved()
        self.assertNotIn('가상 장소', json.dumps(baseline, ensure_ascii=False))
        summary = delivery_routes.compare_saved(baseline, lambda _: AFTER)
        self.assertIn('현재 미배정 3건', summary)
        self.assertIn('새로 보이는 미배정 번호 1건 · 13번', summary)
        self.assertIn('행사시간·장소 변경 1건 · 11번', summary)
        self.assertIn('목록에서 빠진 번호 1건 · 12번', summary)
        self.assertIn('상태 확인 필요 2건 · 15번, 19번', summary)
        self.assertNotIn('14번', summary)
        self.assertNotIn('가상 장소', summary)
        self.assertIn('행 이동', summary)

    def test_staff_uses_own_saved_baseline_and_refreshes_without_replanning(self):
        def send(owner, message):
            body = {'userRequest': {'user': {'id': owner}, 'utterance': message}}
            return str(agent.dispatch(body))

        with tempfile.TemporaryDirectory() as tmp, patch.object(agent, 'ROOT', Path(tmp)), \
             patch.object(agent, 'read_delivery_ranges', side_effect=[AFTER, BEFORE]) as read, \
             patch.object(delivery_routes, 'calculate') as plan:
            with agent.db() as c:
                for employee in ('a', 'b', 'old'):
                    c.execute('INSERT INTO staff VALUES (?,?,0)', (employee, 'staff'))
                for employee, record in [('a', saved()), ('old', {'route_report': {DAY: {}}})]:
                    c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                              (employee * 8, employee, 'fixture', 0, 'done', '{}', json.dumps(record)))
            for person, expected in [('b', '아직 만든'), ('old', '비교할 기록이 없어요')]:
                self.assertIn(expected, send(person, '배송루트 변경 확인'))
            read.assert_not_called()
            self.assertIn('13번', send('a', '아까 배송루트에서 뭐가 바뀌었어?'))
            self.assertNotIn('13번', send('a', '배송루트 변경 확인'))
            self.assertEqual(read.call_count, 2)
            self.assertEqual(read.call_args.args[0], delivery_routes.BOARD_RANGES)
            plan.assert_not_called()
