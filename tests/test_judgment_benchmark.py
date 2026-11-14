"""Synthetic judgment benchmark. Run this file via unittest discovery."""
import time
import unittest
from unittest.mock import patch

import agent
import delivery_counts
import staff_photo
from test_delivery_counts import DAY, FLAGS, VIEW, fixture
from test_staff_operations import fixture as photo_fixture


PHOTO_QUESTIONS = (
    ('', 'all'), ('사진', 'all'), ('[사진]', 'all'), ('전부 보여줘', 'all'),
    ('전체 상태', 'all'), ('배송 현황 알려줘', 'all'), ('이거 완료됐어?', 'all'),
    ('배송 상태 확인해 줘', 'all'), ('이 주문 배송 완료됐나요?', 'all'),
    ('이거 아직 안 끝났어?', 'all'), ('완료만', 'complete'),
    ('완료된 것만 보여줘', 'complete'), ('배송 완료건만 알려주세요', 'complete'),
    ('이 사진에서 끝난 거만 보여줘', 'complete'), ('미완료만', 'pending'),
    ('배송 미완료 건만 보여 줘', 'pending'), ('아직 안 끝난 것만', 'pending'),
    ('이 사진에서 아직 안 된 것만 보여주세요', 'pending'),
    ('완료 처리해 줘', None), ('주문 101번 완료 처리해 줘', None),
    ('주소가 어디야?', None), ('이거 택배야?', None),
    ('오늘은 몇 건?', None), ('취소해 줘', None), ('사진 삭제해 줘', None),
)

SCOPES = [(day, offset, center, label, metric)
          for offset, day in enumerate(('오늘', '내일', '모레'))
          for center in ('구로', '수원', None)
          for label, metric in (('미완료', 'pending'), ('완료', 'complete'), ('전체', 'total'))
          if (day, center, metric) not in (('모레', None, 'complete'), ('내일', '수원', 'total'))]

CONTEXT_CHANGES = [(offset, center, before, word, metric)
                   for offset, center in ((0, '구로'), (1, '수원'), (2, None), (1, '구로'), (0, None))
                   for before, word, metric in (('pending', '완료만', 'complete'),
                                                ('complete', '미완료만', 'pending'),
                                                ('pending', '전체만', 'total'),
                                                ('total', '남은 건만', 'pending'))]


class JudgmentBenchmark(unittest.TestCase):
    def test_photo_question_intent_25(self):
        self.assertEqual(len(PHOTO_QUESTIONS), 25)
        for prompt, expected in PHOTO_QUESTIONS:
            with self.subTest(prompt=prompt):
                self.assertEqual(staff_photo.question_mode(prompt), expected)

    def test_delivery_date_center_status_25(self):
        self.assertEqual(len(SCOPES), 25)
        for day, offset, center, label, metric in SCOPES:
            if label == '전체':
                prompt = f'{day} {center + "센터 " if center else ""}전체 배송 몇 건?'
            else:
                prompt = f'{day} {center + "센터 " if center else ""}배송 {label} 몇 건?'
            with self.subTest(prompt=prompt):
                self.assertEqual(delivery_counts.request(prompt), (offset, center, metric))

    def test_conversation_context_25(self):
        self.assertEqual(len(CONTEXT_CHANGES) + 5, 25)
        history = [{'created': time.time(), 'input': {'message': '가상 조회'},
                    'result': {'summary': '가상 조회 결과', 'read_query': None}}]
        for offset, center, old, word, expected in CONTEXT_CHANGES:
            with self.subTest(offset=offset, center=center, old=old, word=word), \
                 patch.object(agent, 'delivery_count') as read:
                history[0]['result']['read_query'] = ['delivery_count', offset, center, old]
                agent.handle(word, history, 'fixture')
                read.assert_called_once_with(offset, center, expected)
        history[0]['result']['read_query'] = ['delivery_count', 1, '구로', 'pending']
        for word, expected in (('그럼 수원만', (1, '수원', 'pending')), ('모레는?', (2, '구로', 'pending'))):
            with self.subTest(word=word), patch.object(agent, 'delivery_count') as read:
                agent.handle(word, history, 'fixture')
                read.assert_called_once_with(*expected)
        with self.subTest(word='ㅇㅇ'), patch.object(agent, 'delivery_count') as read:
            self.assertIn('다른 날짜', agent.handle('ㅇㅇ', history, 'fixture')['summary'])
            read.assert_not_called()
        for elapsed, since in ((901, 901), (0, 901)):
            with self.subTest(elapsed=elapsed, since=since):
                history[0]['created'] = time.time() - elapsed
                history[0]['result']['read_context_at'] = time.time() - since
                self.assertIsNone(agent.current_read_query(history))

    def test_data_grounding_25(self):
        checks = []
        for number, state in (('101', '완료'), ('102', '미완료'), ('103', '확인 필요'),
                              ('104', '미완료'), ('105', '주문번호 확인 필요')):
            for mode in ('all', 'complete', 'pending'):
                if mode == 'all':
                    expected = f'{number}번 · {state}'
                elif state in ('완료', '미완료'):
                    typed = '완료' if mode == 'complete' else '미완료'
                    expected = f'배송 {typed} {int(typed == state)}건'
                else:
                    expected = '확인하지 못한 주문 1건' if number == '103' else '정확한 주문번호만'
                checks.append((number, mode, expected))
        for number, mode, expected in checks:
            with self.subTest(number=number, mode=mode), patch.object(agent, 'read_staff_delivery', side_effect=photo_fixture):
                self.assertIn(expected, agent.lookup_photo_orders([number], mode)['summary'])
        extra = (('total', None, '2건'), ('complete', None, '1건'), ('pending', None, '1건'),
                 ('complete', '구로', '1건'), ('pending', '구로', '0건'),
                 ('complete', '수원', '0건'), ('pending', '수원', '1건'))
        self.assertEqual(len(checks) + len(extra) + 3, 25)
        for metric, center, expected in extra:
            with self.subTest(metric=metric, center=center):
                data = fixture()
                self.assertIn(expected, delivery_counts.count(DAY.date(), center, metric, lambda ranges: [data[key] for key in ranges]))
        for fault in ('duplicate', 'invalid'):
            with self.subTest(fault=fault):
                data = fixture()
                if fault == 'duplicate':
                    data[VIEW][1] = [5, (DAY.date() - delivery_counts.EPOCH).days]
                else:
                    data[FLAGS][1] = ['TRUE']
                result = delivery_counts.count(DAY.date(), None, 'pending', lambda ranges: [data[key] for key in ranges])
                self.assertIn('확인된 건은 0건', result)
                self.assertIn('상태 확인이 필요', result)
        with self.subTest(fault='read error'), patch.object(agent, 'read_staff_delivery', side_effect=TimeoutError):
            self.assertIn('조회하지 못했어요', agent.lookup_photo_orders(['101'], 'all')['summary'])
