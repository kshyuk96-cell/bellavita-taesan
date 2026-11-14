import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import agent
import development_queue
from read_development_queue import signature


class DevelopmentQueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(patch.object(agent, 'ROOT', Path(tmp.name)))
        with agent.db() as c:
            c.executemany('INSERT INTO staff VALUES (?,?,?)', [
                ('representative', 'admin', 0), ('staff', 'staff', 0)])

    def send(self, owner, message):
        reply = agent.dispatch({'userRequest': {'user': {'id': owner}, 'utterance': message}})
        return reply['template']

    def test_representative_natural_request_bypasses_approval(self):
        message = '배송일시 배송장소 보내는분요약(DB 반영값) 검토해서 같은 주문이 중복 입력되면 경고 알림 줄 수 있나'
        with patch.object(agent, 'choose') as choose:
            self.assertIn('바로 넣었어요', str(self.send('representative', message)))
            choose.assert_not_called()
        with agent.db() as c:
            work = development_queue.read(c)
            self.assertEqual([item['detail'] for item in work], [message])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM approvals').fetchone()[0], 0)

    def test_only_representative_approval_queues_employee_request(self):
        detail = '미배정건 조회에 날짜 추가'
        self.assertIn('대표님 승인 후', str(self.send('staff', '기능 개발: '+detail)))
        self.assertIn('대표 계정만', str(self.send('staff', '승인')))
        with agent.db() as c:
            self.assertEqual(development_queue.read(c), [])
        self.assertIn(detail, str(self.send('representative', '개발 요청 보여줘')))
        self.assertIn('승인했어요', str(self.send('representative', '승인')))
        self.assertIn('이미 처리', str(self.send('representative', '승인')))
        with agent.db() as c:
            self.assertEqual([item['detail'] for item in development_queue.read(c)], [detail])

    def test_multiple_requests_require_selecting_the_intended_one(self):
        for detail in ('첫 번째 요청', '두 번째 요청'):
            self.send('staff', '개발 요청: '+detail)
        response = self.send('representative', '승인')
        self.assertIn('여러 건', str(response))
        with agent.db() as c:
            self.assertEqual(development_queue.read(c), [])
        buttons = response['quickReplies']
        first = next(button for button in buttons if '첫 번째 요청' in button['label'])
        self.send('representative', first['messageText'])
        self.send('representative', '승인')
        with agent.db() as c:
            self.assertEqual([item['detail'] for item in development_queue.read(c)], ['첫 번째 요청'])

    def test_owner_can_follow_request_through_verification(self):
        self.assertIn('없어요', str(self.send('staff', '내 개발 요청 상태')))
        reply = self.send('staff', '기능 개발: 예시 요청')
        self.assertIn('내 개발 요청 상태', [button['label'] for button in reply['quickReplies']])
        self.assertIn('대표님 승인 대기', str(self.send('staff', '내 개발 요청 상태')))
        self.assertIn('없어요', str(self.send('representative', '내 개발 요청 상태')))
        self.send('representative', '승인')
        self.assertIn('작업 대기', str(self.send('staff', '내 개발 요청 상태')))
        with agent.db() as c:
            item = development_queue.read(c)[0]
            self.assertTrue(development_queue.complete(c, item['job'], 'https://github.com/kshyuk96-cell/bellavita-taesan/pull/1'))
        self.assertIn('검증을 마쳤어요', str(self.send('staff', '내 개발 요청 상태')))

    def test_natural_development_request_does_not_overwrite_invitation_draft(self):
        self.send('representative', '초대장')
        detail = '배송완료 처리 안 된 건이 몇 건인지 보는 기능이 필요해'
        with patch.object(agent, 'choose') as choose, patch.object(agent, 'run_invitation') as invitation:
            self.assertIn('바로 넣었어요', str(self.send('representative', detail + '. 개발해')))
            choose.assert_not_called()
            invitation.assert_not_called()
        with agent.db() as c:
            self.assertEqual([row['detail'] for row in development_queue.read(c)], [detail])
            self.assertIsNotNone(c.execute('SELECT 1 FROM invitation_input').fetchone())

    def test_worker_http_reads_and_completes_an_authorized_request(self):
        self.send('representative', '개발 요청: 예시 요청')
        key = Ed25519PrivateKey.generate()
        server = agent.ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler)
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.shutdown)
        endpoint = f'http://127.0.0.1:{server.server_port}/development-queue'
        with patch.object(development_queue, 'PUBLIC_KEY', key.public_key().public_bytes_raw().hex()):
            with self.assertRaises(HTTPError) as error:
                urlopen(endpoint, timeout=3)
            self.assertEqual(error.exception.code, 401)
            headers = {'Authorization': signature(key, 'GET')}
            with urlopen(Request(endpoint, headers=headers), timeout=3) as response:
                item = json.load(response)['requests'][0]
            with self.assertRaises(HTTPError) as replay:
                urlopen(Request(endpoint, headers=headers), timeout=3)
            self.assertEqual(replay.exception.code, 401)
            data = urlencode({'job': item['job'], 'result': 'https://github.com/kshyuk96-cell/bellavita-taesan/pull/1'}).encode()
            headers = {'Authorization': signature(key, 'POST', data)}
            with self.assertRaises(HTTPError) as changed:
                urlopen(Request(endpoint, data=data+b'2', headers=headers), timeout=3)
            self.assertEqual(changed.exception.code, 401)
            with urlopen(Request(endpoint, data=data, headers=headers), timeout=3) as response:
                self.assertTrue(json.load(response)['completed'])
            headers = {'Authorization': signature(key, 'GET')}
            with urlopen(Request(endpoint, headers=headers), timeout=3) as response:
                self.assertEqual(json.load(response)['requests'], [])
