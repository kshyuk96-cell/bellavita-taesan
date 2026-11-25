import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import owner_alerts as legacy
import sendon_order_queue as queue


class StaffModes(unittest.TestCase):
    def check_mode(self, mode):
        env = dict(SENDON_MODE=mode, SENDON_ID='fixture', SENDON_API_KEY='fixture',
                   SENDON_LIVE_START_AT='2000-01-01T00:00:00+00:00')
        with tempfile.TemporaryDirectory() as folder:
            def connect():
                db=sqlite3.connect(Path(folder)/'fixture.db')
                db.row_factory=sqlite3.Row
                return db
            with connect() as db:
                legacy.setup(db)
                db.execute("INSERT INTO alert_account VALUES (1,'fixture')")
                legacy.enqueue(db,'staff:fixture','직원 신청')
            with patch.dict(os.environ,env), patch.dict(sys.modules,{'order_sms':SimpleNamespace(staff_phones=lambda _:['01000000001'])}):
                for _ in range(2):
                    queue.enqueue(connect,'a'*32,{'title':'가상 주문'})
            with patch.object(legacy,'token',return_value={'access_token':'fixture'}), patch.object(
                legacy,'post',return_value={'result_code':0}
            ) as kakao:
                for _ in range(3):
                    legacy.send_pending(connect,env=env)
                self.assertEqual(kakao.call_count,1 if mode=='live' else 2)
            with connect() as db, patch.object(queue,'build_opener',side_effect=AssertionError('Network forbidden')) as http:
                if mode=='live':
                    with patch.object(queue,'transmit',return_value=('accepted','fixture','200','fixture')) as fake:
                        self.assertTrue(queue.process(db,'a'*32,queue.STAFF,env=env))
                        fake.assert_called_once()
                    self.assertEqual(db.execute('SELECT channel FROM payapp_staff_dispatch').fetchone()[0],'sendon')
                else:
                    self.assertEqual(queue.process(db,'a'*32,queue.STAFF,env=env),mode=='dry_run')
                    self.assertEqual(queue.list_jobs(db)[0]['result_code'],'SIMULATED' if mode=='dry_run' else None)
                    self.assertEqual(db.execute('SELECT channel FROM payapp_staff_dispatch').fetchone()[0],'kakao')
                    if mode=='off':
                        self.assertTrue(queue.process(db,'a'*32,queue.STAFF,env={**env,'SENDON_MODE':'live'}))
                        self.assertEqual(queue.list_jobs(db)[0]['result_code'],'STAFF_ALREADY_CLAIMED')
                http.assert_not_called()

    def test_off(self):
        self.check_mode('off')

    def test_dry_run(self):
        self.check_mode('dry_run')

    def test_live(self):
        self.check_mode('live')
