"""Crash-aware receipts for a parent runtime dispatching its native agents.

The parent invokes its actual platform tools outside this module. A dispatch
receipt is not proof of execution or verification. Only the registered agent
receives the public assignment; lease tokens stay in the private state file.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .control import request, secret
from .worker import validate_task


class HandoffBlocked(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class NativeHandoff:
    def __init__(self, path, client, agent_ref, *, read_task=None):
        if not isinstance(agent_ref, str) or not re.fullmatch(r'/[A-Za-z0-9_/-]{1,180}', agent_ref):
            raise ValueError('등록된 하위 에이전트가 필요해요.')
        self.path, self.client, self.agent_ref = Path(path).absolute(), client, agent_ref
        self.read_task = read_task or self._read_remote
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink() or self.path.parent.stat().st_mode & 0o077:
            raise ValueError('인계 기록은 소유자 전용 폴더에 보관해 주세요.')
        with self._locked() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS native_handoff (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),phase TEXT NOT NULL,
                agent TEXT,task TEXT,receipt TEXT,report TEXT)''')
            c.execute("INSERT OR IGNORE INTO native_handoff VALUES (1,'idle',NULL,NULL,NULL,NULL)")
            c.execute('''CREATE TABLE IF NOT EXISTS native_handoff_events (
                task_id TEXT,agent TEXT,phase TEXT NOT NULL,recorded REAL NOT NULL,
                receipt TEXT,report_digest TEXT)''')
            self._row(c)

    @contextmanager
    def _locked(self):
        fd = os.open(str(self.path)+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        connection = None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise HandoffBlocked('인계 잠금 파일을 확인하지 못했어요.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HandoffBlocked('다른 관리 작업이 인계 상태를 확인하고 있어요.') from None
            database = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(database)
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                    raise HandoffBlocked('비공개 인계 기록의 권한을 확인해 주세요.')
            finally:
                os.close(database)
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            yield connection
            connection.commit()
        finally:
            if connection:
                connection.close()
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _row(self, c):
        row = c.execute('SELECT * FROM native_handoff WHERE singleton=1').fetchone()
        if row['agent'] and row['agent'] != self.agent_ref:
            raise HandoffBlocked('기존 작업의 담당 에이전트가 달라 재배정하지 않았어요.')
        return row

    def _save(self, c, phase, *, task=None, receipt=None, report=None):
        c.execute('''UPDATE native_handoff SET phase=?,agent=?,task=COALESCE(?,task),
            receipt=COALESCE(?,receipt),report=COALESCE(?,report) WHERE singleton=1''',
            (phase, self.agent_ref, _json(task) if task is not None else None,
             receipt, _json(report) if report is not None else None))
        current = c.execute('SELECT task,receipt,report FROM native_handoff WHERE singleton=1').fetchone()
        c.execute('INSERT INTO native_handoff_events VALUES (?,?,?,?,?,?)',
                  (json.loads(current['task'])['id'] if current['task'] else None, self.agent_ref,
                   phase, time.time(), current['receipt'],
                   hashlib.sha256(current['report'].encode()).hexdigest() if current['report'] else None))
        c.commit()  # Write intent survives a lost response or parent interruption.

    def _public(self, c):
        row = self._row(c)
        result = {'phase': row['phase'], 'agent_ref': row['agent'], 'execution_verified': False}
        if row['task']:
            task = json.loads(row['task'])
            result.update(task_id=task['id'], version=task['version'], manifest_digest=task['manifest_digest'],
                          manifest=task['manifest'])
        return result

    def snapshot(self):
        with self._locked() as c:
            return self._public(c)

    def _pulse(self, c, row):
        task = json.loads(row['task'])
        try:
            validate_task(task, self.client.project)
            observed = self.client.heartbeat(task)
            if (not isinstance(observed, dict) or observed.get('id') != task['id']
                    or observed.get('state') != 'running' or observed.get('cancel_requested') is not False
                    or type(observed.get('lease_expires_at')) not in (int, float)
                    or not math.isfinite(observed['lease_expires_at']) or observed['lease_expires_at'] <= 0):
                raise ValueError('unverified heartbeat')
        except Exception:
            self._save(c, 'needs_verification')
            raise HandoffBlocked('임대권을 확인하지 못해 전달과 결과 제출을 멈췄어요.') from None
        self._save(c, row['phase'], task=task)
        return task

    def begin(self):
        with self._locked() as c:
            row = self._row(c)
            if row['phase'] in ('claimed', 'dispatched'):
                self._pulse(c, row)
                return self._public(c)
            if row['phase'] != 'idle':
                raise HandoffBlocked('이전 인계의 실행 여부를 먼저 대조해야 해요.')
            self._save(c, 'claim_pending')
            try:
                task = self.client.claim()
                if task is not None:
                    validate_task(task, self.client.project)
            except Exception:
                self._save(c, 'claim_uncertain')
                raise HandoffBlocked('작업 수신이 미확인이라 다시 가져오지 않았어요.') from None
            if task is None:
                c.execute("UPDATE native_handoff SET phase='idle',agent=NULL WHERE singleton=1")
                c.commit()
                return self._public(c)
            self._save(c, 'claimed', task=task)
            return self._public(c)

    def prepare_dispatch(self):
        with self._locked() as c:
            row = self._row(c)
            if row['phase'] != 'claimed':
                raise HandoffBlocked('이미 전달했거나 전달 여부를 확인해야 하는 작업이에요.')
            self._pulse(c, row)
            self._save(c, 'dispatch_pending')
            return self._public(c)

    def dispatched(self, receipt):
        if not isinstance(receipt, str) or not re.fullmatch(r'[A-Za-z0-9_:/.-]{1,200}', receipt):
            raise ValueError('실제 플랫폼 도구의 인계 참조가 필요해요.')
        with self._locked() as c:
            row = self._row(c)
            if row['phase'] != 'dispatch_pending':
                raise HandoffBlocked('전달 의도와 현재 상태가 일치하지 않아요.')
            task = self._pulse(c, row)
            if task['lease_token'] in receipt:
                raise ValueError('임대권은 플랫폼 참조로 기록할 수 없어요.')
            self._save(c, 'dispatched', receipt=receipt)
            return self._public(c)

    def finish(self, report):
        encoded = _json(report)
        if not isinstance(report, dict) or len(encoded.encode()) > 65536:
            raise ValueError('검증할 작업 보고 형식을 확인해 주세요.')
        with self._locked() as c:
            row = self._row(c)
            if row['phase'] != 'dispatched':
                raise HandoffBlocked('실제 인계 기록을 확인한 뒤 결과를 제출해야 해요.')
            task = self._pulse(c, row)
            self._save(c, 'report_pending', report=report)
            try:
                saved = self.client.report(task, report)
                if (saved.get('state') != 'reported' or saved.get('report') != report
                        or any(saved.get(key) != task.get(key) for key in
                               ('id', 'version', 'manifest_digest', 'manifest', 'worker'))):
                    raise ValueError('unverified report')
            except Exception:
                self._save(c, 'report_uncertain')
                raise HandoffBlocked('보고 응답이 미확인이라 다시 제출하지 않았어요.') from None
            self._save(c, 'reported')
            return self._public(c)

    def _read_remote(self, task_id):
        result = request(self.client.base_url, secret(self.client.token_file), 'GET', '/v1/tasks/'+task_id)
        if result.get('ok') is not True:
            raise HandoffBlocked('서버의 현재 작업을 읽지 못했어요.')
        return result['task']

    def reconcile(self):
        with self._locked() as c:
            row = self._row(c)
            if not row['task']:
                raise HandoffBlocked('대상 작업을 모르는 인계는 자동 복구하지 않아요.')
            original = json.loads(row['task'])
            try:
                observed = self.read_task(original['id'])
                bound = ('id', 'version', 'manifest_digest', 'manifest', 'worker')
                if any(observed.get(key) != original.get(key) for key in bound):
                    raise ValueError('binding changed')
            except Exception:
                raise HandoffBlocked('현재 서버 기록과 인계 대상을 대조하지 못했어요.') from None
            report = json.loads(row['report']) if row['report'] else None
            if (row['phase'] in ('report_pending', 'report_uncertain', 'reported', 'needs_verification')
                    and observed.get('state') in ('reported', 'reviewed') and report is not None
                    and observed.get('report') == report):
                self._save(c, 'reported')
            elif row['phase'] in ('claimed', 'dispatched') and observed.get('state') == 'running':
                self._pulse(c, row)
            else:
                self._save(c, 'needs_verification')
            return self._public(c)

    def release_reviewed(self):
        with self._locked() as c:
            row = self._row(c)
            if row['phase'] != 'reported' or not row['task'] or not row['report']:
                raise HandoffBlocked('관리자 검토가 끝난 보고만 종료할 수 있어요.')
            task = json.loads(row['task'])
            try:
                current = self.read_task(task['id'])
            except Exception:
                raise HandoffBlocked('현재 관리자 검토 기록을 읽지 못했어요.') from None
            if (current.get('state') != 'reviewed' or current.get('id') != task['id']
                    or current.get('version') != task['version'] or current.get('manifest_digest') != task['manifest_digest']
                    or current.get('manifest') != task['manifest'] or current.get('worker') != task['worker']
                    or current.get('report') != json.loads(row['report'])):
                raise HandoffBlocked('관리자 검토 결과와 제출한 보고가 일치하지 않아요.')
            c.execute('INSERT INTO native_handoff_events VALUES (?,?,?,?,?,?)',
                      (task['id'], self.agent_ref, 'released', time.time(), row['receipt'],
                       hashlib.sha256(row['report'].encode()).hexdigest()))
            c.execute("UPDATE native_handoff SET phase='idle',agent=NULL,task=NULL,receipt=NULL,report=NULL WHERE singleton=1")
            c.commit()
            return self._public(c)
