import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import agent
import delivery_reports


class ReportReissueTests(unittest.TestCase):
    def test_employee_can_reissue_only_their_own_report(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(agent, 'ROOT', Path(temporary)), patch.dict(
            'os.environ', {'TAESAN_PUBLIC_URL': 'https://example.com', 'TAESAN_WEBHOOK_SECRET': 'qa-secret'}
        ):
            def dispatch(owner, text):
                return agent.dispatch({'userRequest': {'user': {'id': owner}, 'utterance': text}})

            job = 'a' * 12
            result = {'summary': '모의 배정안', 'route_buttons': [],
                      'route_report': {'2026-11-15': {'routes': [], 'missed': [], 'held': 0}}}
            with agent.db() as connection:
                connection.executemany('INSERT INTO staff VALUES (?,?,?)',
                                       [(owner, 'staff', time.time()) for owner in ('qa-one', 'qa-two')])
                connection.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                                   (job, 'qa-one', '', time.time(), 'done', '{}', json.dumps(result)))

            def excel_path(reply):
                link = reply['template']['outputs'][-1]['textCard']['buttons'][0]['webLinkUrl']
                return urlsplit(link).path

            old_path = excel_path(dispatch('qa-one', '최근 배송보고서'))
            with patch('delivery_reports.time.time', return_value=time.time() + 3601):
                self.assertFalse(delivery_reports.authorized(old_path, 'qa-secret'))
                for message in ('최근 배송보고서 링크 다시 보내줘', '지난 배송루트 보고서 엑셀 링크 재발급해 주세요',
                                '최근 배송보고서 다시 열어줘'):
                    with self.subTest(message=message):
                        new_path = excel_path(dispatch('qa-one', message))
                        self.assertIn(job, new_path)
                        self.assertTrue(delivery_reports.authorized(new_path, 'qa-secret'))
                self.assertIn('아직 만든 배송보고서가 없어요', str(dispatch('qa-two', '최근 배송보고서 링크 다시 보내줘')))
