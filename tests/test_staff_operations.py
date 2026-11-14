import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from openpyxl import load_workbook
from PIL import Image

import agent
import delivery_venues
import development_queue
import staff_delivery
import staff_photo


NOW = datetime(2026, 11, 14, 12, tzinfo=agent.SEOUL)
SERIAL = (NOW.date() - staff_delivery.EPOCH).days
URL = 'https://secure.kakaocdn.net/dna/test.jpg'


def payload(message, owner='employee'):
    return {'userRequest': {'user': {'id': owner}, 'utterance': message}}


def text(result):
    return result['template']['outputs'][0]['simpleText']['text']


def fixture(ranges):
    data = {
        "'배송관리'!A3:D": [list(staff_delivery.COLUMNS[:4]), [101, SERIAL, .5, '=TEST()'],
                       [102, SERIAL, .55, '가상홀'], [103, SERIAL, .55, '가상홀'],
                       [104, SERIAL + 1, .55, '내일홀'], [105, SERIAL, .55, '다른홀'],
                       [105, SERIAL, .55, '다른홀']],
        "'배송관리'!G3:G": [['배송채널'], ['구로센터'], ['구로센터'], ['구로센터'], ['구로센터'], ['수원센터'], ['수원센터']],
        "'배송관리'!K3:K": [['배송 완료'], [True], [False], ['TRUE'], [False], [False], [False]],
        "'배송관리'!O3:O": [['배송원'], ['가상 직원'], [], ['교육용 담당'], [], [], []],
    }
    data["'배송관리'!A3:A"] = [row[:1] for row in data["'배송관리'!A3:D"]]
    return [data[key] for key in ranges]


class StaffOperationsTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(agent, 'ROOT', Path(folder)))
        self.enterContext(patch.object(agent, 'read_staff_delivery', side_effect=fixture))
        clock = self.enterContext(patch.object(agent, 'datetime', wraps=datetime))
        clock.now.return_value = NOW
        self.enterContext(patch.dict(os.environ, {'TAESAN_PUBLIC_URL': 'https://qa.example',
                                                   'TAESAN_WEBHOOK_SECRET': 'fixture-only'}))
        with agent.db() as conn:
            conn.execute("INSERT INTO staff VALUES ('employee','staff',0)")

    def test_report_through_kakao_and_signed_file_endpoints(self):
        answer = agent.dispatch(payload('오늘 배송 현황 엑셀'))
        self.assertIn('4건 · 완료 1건 · 미완료 1건 · 확인 필요 2건', text(answer))
        with agent.db() as conn:
            self.assertNotIn('가상홀', conn.execute('SELECT result FROM jobs').fetchone()[0])
        self.assertIn('3건 · 완료 1건 · 미완료 1건 · 확인 필요 1건',
                      text(agent.dispatch(payload('그럼 구로만'))))
        self.assertIn('1건 · 완료 0건 · 미완료 1건', text(agent.dispatch(payload('내일은?'))))
        outputs = answer['template']['outputs']
        image = outputs[1]['simpleImage']['imageUrl']
        excel = outputs[2]['textCard']['buttons'][0]['webLinkUrl']
        with ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                with urlopen(image.replace('https://qa.example', base)) as response:
                    self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
                    self.assertEqual(Image.open(response).format, 'PNG')
                excel = excel.replace('https://qa.example', base)
                with urlopen(excel) as response:
                    table = load_workbook(BytesIO(response.read())).active
                self.assertEqual(table['D2'].value, '=TEST()')
                self.assertEqual(table['D2'].data_type, 's')
                self.assertEqual([cell[0].value for cell in table['G2:G5']], ['완료', '미완료', '확인 필요', '확인 필요'])
                bad_signature = excel[:-1] + ('0' if excel[-1] != '0' else '1')
                with self.assertRaises(HTTPError) as error:
                    urlopen(bad_signature)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
                with agent.db() as conn:
                    conn.execute("DELETE FROM staff WHERE owner='employee'")
                with self.assertRaises(HTTPError) as error:
                    urlopen(excel)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            finally:
                server.shutdown()
                worker.join()

    def test_short_follow_up_respects_invitation_and_read_context(self):
        with patch.object(agent, 'run_invitation', return_value={'summary': '가상 초대장 자료'}) as invite, \
             patch.object(agent, 'choose') as choose:
            agent.dispatch(payload('초대장'))
            self.assertEqual(text(agent.dispatch(payload('내일은?'))), '가상 초대장 자료')
            invite.assert_called_once()
            self.assertIn('배송관리', text(agent.dispatch(payload('배송 미완료 목록'))))
            self.assertIn('다른 날짜', text(agent.dispatch(payload('ㅇㅇ'))))
            self.assertIn('구로', text(agent.dispatch(payload('그럼 구로만'))))
            with agent.db() as conn:
                conn.execute("UPDATE jobs SET created=?", (time.time() - 1000,))
            self.assertEqual(text(agent.dispatch(payload('내일은?'))), '가상 초대장 자료')
            self.assertEqual(invite.call_count, 2)
            choose.assert_not_called()

    def test_consented_photo_reads_sheet_and_does_not_persist_urls(self):
        body = payload('사진')
        body['action'] = {'detailParams': {'secureimage': {'value': json.dumps({
            'privacyAgreement': 'Y', 'secureUrls': f'List({URL.replace("https", "http")},{URL})'
        })}}}
        with patch.dict(sys.modules, {'openai': SimpleNamespace(OpenAI=Mock())}), \
             patch.object(staff_photo, 'extract_numbers', return_value=['101', '102', '103', '105', '999']) as extract:
            answer = text(agent.dispatch(body))
            self.assertIn('101번 · 완료', answer)
            self.assertIn('102번 · 미완료', answer)
            self.assertIn('103번 · 확인 필요', answer)
            self.assertIn('105번 · 주문번호 확인 필요', answer)
            self.assertIn('999번 · 배송관리에서 찾을 수 없음', answer)
            self.assertEqual(extract.call_args.args[0], [URL, URL])
            with agent.db() as conn:
                self.assertNotIn('kakaocdn', str(tuple(conn.execute('SELECT input,result FROM jobs').fetchone())))
            body['action'] = {'params': {'secureimage': {'privacyAgreement': 'N', 'secureUrls': [URL]}}}
            self.assertIn('동의', text(agent.dispatch(body)))
            body['action']['params']['secureimage'].update(privacyAgreement='Y', secureUrls=['https://example.invalid/dna/test'])
            self.assertIn('동의', text(agent.dispatch(body)))
            self.assertEqual(extract.call_count, 1)

    def test_mock_vision_only_returns_numeric_order_ids(self):
        stream = BytesIO()
        Image.new('RGB', (6, 6)).save(stream, format='PNG')
        model = Mock()
        model.responses.create.return_value.output_text = '{"orders":["123","123","099",42,"이름","456"]}'
        outputs = staff_photo.extract_numbers([URL], model, 'mock-model', fetch=lambda *_args, **_kwargs: BytesIO(stream.getvalue()))
        self.assertEqual(outputs, ['123', '456'])
        self.assertIs(model.responses.create.call_args.kwargs['store'], False)

    def test_same_photo_respects_question_and_cannot_request_writes(self):
        def photo(question):
            body = payload(question)
            body['action'] = {'params': {'secureimage': {'privacyAgreement': 'Y', 'secureUrls': [URL]}}}
            return text(agent.dispatch(body))

        with patch.dict(sys.modules, {'openai': SimpleNamespace(OpenAI=Mock())}), \
             patch.object(staff_photo, 'extract_numbers', return_value=['101', '102', '103', '999']) as extract, \
             patch.object(agent, 'choose') as choose:
            complete = photo('완료된 것만 보여줘')
            self.assertIn('배송 완료 1건 · 101번', complete)
            self.assertIn('확인하지 못한 주문 2건', complete)
            self.assertNotIn('102번', complete)
            pending = photo('미완료만 보여줘')
            self.assertIn('배송 미완료 1건 · 102번', pending)
            self.assertNotIn('101번', pending)
            all_statuses = photo('전부 보여줘')
            self.assertIn('101번 · 완료', all_statuses)
            self.assertIn('102번 · 미완료', all_statuses)
            self.assertIn('배송 완료·미완료 상태를 조회', photo('주문 101번 완료 처리해 줘'))
            self.assertEqual(extract.call_count, 3)
            choose.assert_not_called()
            with agent.db() as conn:
                statuses = conn.execute('SELECT input FROM jobs').fetchall()
            self.assertTrue(all(json.loads(row[0])['message'] == '사진으로 배송 확인' for row in statuses))

    def test_photo_differentiates_unreadable_image_and_sheet_outage(self):
        with patch.dict(sys.modules, {'openai': SimpleNamespace(OpenAI=Mock())}), \
             patch.object(staff_photo, 'extract_numbers', side_effect=[[], ['101']]), \
             patch.object(agent, 'read_staff_delivery', side_effect=TimeoutError) as read:
            self.assertIn('주문번호가 잘 보이도록', agent.photo_order_status([URL], '전체 상태')['summary'])
            read.assert_not_called()
            self.assertIn('배송관리를 조회하지 못했어요', agent.photo_order_status([URL], '전체 상태')['summary'])

    def test_slow_read_hands_off_to_kakao_callback_without_transmitting(self):
        release = threading.Event()

        def delayed_read(ranges):
            if not release.wait(8):
                raise RuntimeError('callback was not issued')
            return fixture(ranges)

        body = payload('배송 미완료 목록')
        body['userRequest']['callbackUrl'] = 'https://bot-api.kakao.com/fixture'
        callback = Mock()
        with patch.object(agent, 'read_staff_delivery', side_effect=delayed_read):
            try:
                response = agent.dispatch(body, defer_callback=callback)
                self.assertIs(response.get('useCallback'), True)
                callback.assert_called_once()
                url, job, future, owner = callback.call_args.args
                self.assertEqual(url, body['userRequest']['callbackUrl'])
                self.assertEqual(owner, 'employee')
                release.set()
                self.assertIn('미완료 1건', text(agent.present(job, future.result(timeout=5), owner)))
            finally:
                release.set()

    def test_alert_deduplication_and_release_status(self):
        with agent.db() as conn:
            for _ in range(2):
                staff_delivery.enqueue_imminent(conn, staff_delivery.rows_for(NOW.date(), None, fixture), NOW)
            self.assertEqual([row[0] for row in conn.execute('SELECT id FROM alert_outbox')],
                             ['delivery-imminent:2026-11-14:102'])
            job = '123456789abc'
            conn.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                         (job, 'employee', 'fixture', 0, 'done', '{}', '{"development":true}'))
            self.assertIn('승인 대기', development_queue.latest_status(conn, 'employee'))
            development_queue.enqueue(conn, job, '가상 기능')
            self.assertIn('작업 대기', development_queue.latest_status(conn, 'employee'))
            self.assertFalse(development_queue.record_release(conn, job, 'a' * 40))
            self.assertTrue(development_queue.complete(conn, job, 'https://github.com/kshyuk96-cell/bellavita-taesan/pull/1'))
            self.assertIn('검증을 마쳤어요', development_queue.latest_status(conn, 'employee'))
            self.assertTrue(development_queue.record_release(conn, job, 'a' * 40))
            self.assertIn('운영에 반영됐어요', development_queue.latest_status(conn, 'employee'))

    def test_reviewed_venue_alias_does_not_merge_adjacent_halls(self):
        alias_file = Path(agent.ROOT) / 'alias.csv'
        alias_file.write_text('alias,venue\n가상홀 별칭,가상호텔 가홀\n', encoding='utf-8')
        data = [[['배송일'], [SERIAL], [SERIAL], [SERIAL]],
                [['설치장소'], ['가상홀 별칭'], ['가상호텔 가홀'], ['가상호텔 나홀']]]
        with patch.dict(os.environ, {'TAESAN_VENUE_ALIASES': str(alias_file)}):
            result = delivery_venues.rank(NOW.date(), None, lambda _: data)
            self.assertIn('가상호텔 가홀', result)
            self.assertIn('2건', result)
            self.assertNotIn('3건', result)


if __name__ == '__main__':
    unittest.main()
