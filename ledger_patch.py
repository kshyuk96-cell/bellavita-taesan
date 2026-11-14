"""Explicit admin-only, exact-cell ledger patches with private durable receipts.

The server chooses its configured ledger and resolves the actor's role. This is
not automatic order intake and does not enable background ledger writes. Plain
strings stay literal; formulas require an explicit {'formula': '=...'} value.

Google Sheets has no compare-and-swap for cell values. Re-reading immediately
before batchUpdate detects prior changes but cannot exclude a human edit between
that read and the write. Never describe this helper as atomic with external edits.
Timeouts/uncertain outcomes are recorded and NEVER retried automatically. Every
writer for this ledger on a host must use the SAME private audit directory AND
audit database; a different database cannot see its unresolved receipts. Locks
coordinate this host only, not multiple machines. There is no automatic uncertain
receipt reconciliation: an authorized operator must inspect the actual cells and
audit before further writes can be enabled.
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import ledger_access


_CELL = re.compile(r'([A-Z]{1,3})([1-9][0-9]{0,6})')
_HASH = re.compile(r'[a-f0-9]{64}')
_REQUEST = re.compile(r'[A-Za-z0-9_-]{8,100}')
_PATCH_LOCK = threading.RLock()


class PatchError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _column(value):
    number = 0
    for char in value:
        number = number * 26 + ord(char) - 64
    return number


def _letters(number):
    letters = ''
    while number:
        number, char = divmod(number - 1, 26)
        letters = chr(char + 65) + letters
    return letters


def _entered(value):
    if value is None:
        return None
    if type(value) is bool:
        return {'boolValue': value}
    if type(value) in (int, float) and math.isfinite(value):
        return {'numberValue': value}
    if isinstance(value, str) and len(value) <= 5000:
        return {'stringValue': value}
    if (isinstance(value, dict) and set(value) == {'formula'} and isinstance(value['formula'], str)
            and value['formula'].startswith('=') and 1 < len(value['formula']) <= 5000):
        return {'formulaValue': value['formula']}
    raise PatchError('셀 값은 문자·숫자·체크값·빈 값 또는 명시한 수식으로 입력해 주세요.')


def _source_value(value):
    if value in (None, {}):
        return None
    if not isinstance(value, dict) or len(value) != 1:
        raise PatchError('현재 셀 원본 형식을 확인하지 못했어요.')
    key, item = next(iter(value.items()))
    if key == 'numberValue' and type(item) in (int, float) and math.isfinite(item):
        return value
    if key == 'boolValue' and type(item) is bool:
        return value
    if key in ('stringValue', 'formulaValue') and isinstance(item, str):
        return value
    raise PatchError('현재 셀 원본 형식을 확인하지 못했어요.')


class LedgerPatchService:
    def __init__(self, env=None, *, role_for, audit_path=None, session=None):
        self.env = os.environ if env is None else env
        if not callable(role_for):
            raise PatchError('서버의 직원 권한 확인 연결이 필요해요.')
        self.role_for = role_for
        self.audit_path = Path(audit_path or (Path(self.env.get('TAESAN_DATA', 'taesan-data')) / 'ledger-edits.sqlite3')).resolve()
        self._session = session
        self._owns_session = session is None
        self._closed = False

    def close(self, *, close_injected=False):
        if not self._closed and self._session is not None and (self._owns_session or close_injected):
            ledger_access._close(self._session)
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _ledger_lock(self, ledger_id):
        """Nonblocking same-host advisory lock, not a distributed lock/CAS."""
        descriptor = None
        acquired = False
        try:
            import fcntl
            self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
            if hasattr(os, 'O_CLOEXEC'):
                flags |= os.O_CLOEXEC
            lock_path = self.audit_path.parent / ('.ledger-' + _hash(ledger_id) + '.lock')
            descriptor = os.open(lock_path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PatchError('원장 수정 잠금 파일을 확인하지 못했어요.')
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        except PatchError:
            raise
        except (ImportError, OSError, AttributeError):
            raise PatchError('원장 수정 잠금 연결을 확인하지 못해 실행하지 않았어요.') from None
        finally:
            if descriptor is not None:
                try:
                    if acquired:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _authorize(self, actor):
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 512:
            raise PermissionError('대표 계정을 확인해 주세요.')
        try:
            role = self.role_for(actor)
        except Exception:
            role = None
        if role != 'admin':
            raise PermissionError('대표 계정의 명시한 원장 수정만 처리할 수 있어요.')

    def _target(self):
        try:
            target = ledger_access.configured_ledger(self.env)
        except ValueError:
            target = None
        if target is None:
            raise PatchError('태산이 서버에 정확한 원장 파일을 연결해 주세요.')
        return target

    def _transport(self):
        if self._closed:
            raise PatchError('원장 연결이 닫혀 있어 새 연결로 확인해야 해요.')
        if self._session is None:
            credentials = ledger_access.load_server_credentials(self.env, scopes=(ledger_access.SHEETS_SCOPE,))
            self._session = ledger_access.authorized_session(credentials)
        return self._session

    def _selection(self, requested_range, values):
        title = self.env.get('TAESAN_LEDGER_TAB', '액자화환')
        if not isinstance(title, str) or not title or len(title) > 80 or any(ord(c) < 32 for c in title):
            raise PatchError('서버에 등록한 원장 탭을 확인해 주세요.')
        if not isinstance(requested_range, str) or len(requested_range) > 200:
            raise PatchError('수정할 셀 범위를 명시해 주세요.')
        configured_tabs = self.env.get('TAESAN_LEDGER_ALLOWED_TABS', title)
        if not isinstance(configured_tabs, str):
            raise PatchError('서버의 수정 허용 탭 목록을 확인해 주세요.')
        allowed_tabs = {tab.strip() for tab in configured_tabs.split(',') if tab.strip()}
        if (not allowed_tabs or len(allowed_tabs) > 100 or any(len(tab) > 80 or any(ord(c) < 32 for c in tab)
                                                             for tab in allowed_tabs)):
            raise PatchError('서버의 수정 허용 탭 목록을 확인해 주세요.')
        cell_range = requested_range
        if '!' in requested_range:
            sheet, cell_range = requested_range.rsplit('!', 1)
            if sheet.startswith("'") and sheet.endswith("'"):
                sheet = sheet[1:-1].replace("''", "'")
            if sheet not in allowed_tabs:
                raise PatchError('서버에 등록한 원장 탭만 수정할 수 있어요.')
            title = sheet
        elif title not in allowed_tabs:
            raise PatchError('기본 원장 탭이 수정 허용 목록에 없어요.')
        ends = cell_range.split(':')
        if len(ends) not in (1, 2) or any(not _CELL.fullmatch(end) for end in ends):
            raise PatchError('전체 열·행 대신 시작과 끝 셀을 명시해 주세요.')
        first, last = _CELL.fullmatch(ends[0]), _CELL.fullmatch(ends[-1])
        r1, r2 = int(first[2]), int(last[2])
        c1, c2 = _column(first[1]), _column(last[1])
        height, width = r2 - r1 + 1, c2 - c1 + 1
        if not 1 <= height <= 20 or not 1 <= width <= 20 or height * width > 100:
            raise PatchError('한 번에 100셀·20행·20열 이내의 범위를 지정해 주세요.')
        if (not isinstance(values, list) or len(values) != height
                or any(not isinstance(row, list) or len(row) != width for row in values)):
            raise PatchError('요청 범위와 입력 값의 행·열 수가 같아야 해요.')
        cells = [[_entered(value) for value in row] for row in values]
        quoted = "'" + title.replace("'", "''") + "'"
        canonical = f'{quoted}!{_letters(c1)}{r1}:{_letters(c2)}{r2}'
        return dict(title=title, quoted=quoted, a1=canonical, r1=r1 - 1, r2=r2,
                    c1=c1 - 1, c2=c2, height=height, width=width, cells=cells)

    def _read_json(self, url, params):
        response = None
        try:
            response = self._transport().get(url, params=params, timeout=10, allow_redirects=False)
            if response.status_code == 403:
                raise PermissionError('이 서버 계정은 지정한 원장을 읽거나 편집할 권한이 없어요.')
            if response.status_code != 200:
                raise PatchError('원장 읽기 응답을 확인하지 못했어요.')
            payload = response.json()
            if not isinstance(payload, dict):
                raise PatchError('원장 읽기 형식을 확인하지 못했어요.')
            return payload
        except (PatchError, PermissionError):
            raise
        except Exception:
            raise PatchError('원장 연결을 확인하지 못했어요. 셀 변경은 확인되지 않았어요.') from None
        finally:
            ledger_access._close(response)

    def _snapshot(self, ledger_id, selected):
        url = 'https://sheets.googleapis.com/v4/spreadsheets/' + ledger_id
        metadata = self._read_json(url, {'fields': 'spreadsheetId,sheets(properties(sheetId,title,gridProperties))'})
        if metadata.get('spreadsheetId') != ledger_id:
            raise PatchError('설정된 원장과 응답 파일이 달라 중단했어요.')
        sheets = [sheet for sheet in metadata.get('sheets', []) if isinstance(sheet, dict)
                  and sheet.get('properties', {}).get('title') == selected['title']]
        if len(sheets) != 1:
            raise PatchError('원장 탭을 하나로 확인하지 못했어요.')
        properties = sheets[0]['properties']
        grid = properties.get('gridProperties', {})
        rows, columns, sheet_id = grid.get('rowCount'), grid.get('columnCount'), properties.get('sheetId')
        if (type(rows) is not int or type(columns) is not int or type(sheet_id) is not int
                or not selected['r2'] <= rows or not selected['c2'] <= columns or columns > 1000):
            raise PatchError('현재 원장 범위를 벗어나거나 열 구성을 확인하지 못했어요.')
        row_range = f'{selected["quoted"]}!A{selected["r1"]+1}:{_letters(columns)}{selected["r2"]}'
        data = self._read_json(url, {'ranges': row_range, 'includeGridData': 'true',
            'fields': 'spreadsheetId,sheets(properties(sheetId,title),data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue))))'})
        if data.get('spreadsheetId') != ledger_id:
            raise PatchError('원장 재조회 파일이 달라 중단했어요.')
        sheets = data.get('sheets')
        if (not isinstance(sheets, list) or len(sheets) != 1
                or sheets[0].get('properties', {}).get('sheetId') != sheet_id
                or sheets[0].get('properties', {}).get('title') != selected['title']):
            raise PatchError('원장 재조회 탭이 달라 중단했어요.')
        if not isinstance(sheets[0].get('data'), list) or not sheets[0]['data']:
            raise PatchError('현재 셀 원본을 읽었다는 근거가 없어 중단했어요.')
        matrix = [[None for _ in range(columns)] for _ in range(selected['height'])]
        errors = set()
        for block in sheets[0].get('data', []):
            row_start, col_start = block.get('startRow', 0), block.get('startColumn', 0)
            if type(row_start) is not int or type(col_start) is not int:
                raise PatchError('원장 셀 위치를 확인하지 못했어요.')
            for row_index, row in enumerate(block.get('rowData', []), row_start):
                for col_index, cell in enumerate(row.get('values', []), col_start):
                    if not selected['r1'] <= row_index < selected['r2'] or not 0 <= col_index < columns:
                        raise PatchError('응답 셀 범위가 요청 범위와 달라요.')
                    if not isinstance(cell, dict):
                        raise PatchError('현재 셀 형식을 확인하지 못했어요.')
                    matrix[row_index - selected['r1']][col_index] = _source_value(cell.get('userEnteredValue'))
                    if isinstance(cell.get('effectiveValue'), dict) and 'errorValue' in cell['effectiveValue']:
                        errors.add((row_index, col_index))
        target = [row[selected['c1']:selected['c2']] for row in matrix]
        return {'sheet_id': sheet_id, 'rows': matrix, 'cells': target, 'errors': errors,
                'before_digest': _hash([ledger_id, selected['a1'], target]),
                'row_fingerprint': _hash([ledger_id, sheet_id, selected['r1'], matrix])}

    @contextmanager
    def _audit(self):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        c = sqlite3.connect(self.audit_path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            self.audit_path.chmod(0o600)
            c.execute('''CREATE TABLE IF NOT EXISTS ledger_patches (
                request_id TEXT PRIMARY KEY,actor TEXT NOT NULL,ledger_id TEXT NOT NULL,cell_range TEXT NOT NULL,
                payload_digest TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
                requested_cells TEXT NOT NULL,before_cells TEXT,after_cells TEXT,result TEXT NOT NULL)''')
            with c:
                yield c
        finally:
            c.close()

    def _finish(self, request_id, status, summary, *, before=None, after=None):
        result = {'status': status, 'summary': summary, 'request_id': request_id}
        with self._audit() as c:
            c.execute('''UPDATE ledger_patches SET state=?,updated=?,before_cells=COALESCE(?,before_cells),
                after_cells=COALESCE(?,after_cells),result=? WHERE request_id=?''',
                (status, time.time(), _json(before) if before is not None else None,
                 _json(after) if after is not None else None, _json(result), request_id))
        return result

    def prepare(self, actor, requested_range, values):
        self._authorize(actor)
        selected, ledger_id = self._selection(requested_range, values), self._target()
        try:
            snapshot = self._snapshot(ledger_id, selected)
        except (PatchError, PermissionError):
            raise
        except Exception:
            raise PatchError('변경 전 원장 형식을 확인하지 못했어요.') from None
        self._authorize(actor)
        return {'status': 'prepared', 'ledger_id': ledger_id, 'range': selected['a1'],
                'before_digest': snapshot['before_digest'], 'row_fingerprint': snapshot['row_fingerprint'],
                'payload_digest': _hash([ledger_id, selected['a1'], selected['cells']])}

    def apply(self, actor, request_id, requested_range, values, *, expected_digest, expected_fingerprint):
        self._authorize(actor)
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            raise PatchError('중복 실행을 대조할 수정 요청 ID가 필요해요.')
        ledger_id = self._target()
        # The private audit directory must be shared by workers on this host.
        # Human edits and other hosts are still outside this advisory lock.
        with self._ledger_lock(ledger_id) as acquired:
            if not acquired:
                return {'status': 'blocked', 'code': 'ledger_busy', 'request_id': request_id,
                        'summary': '다른 원장 수정이 진행 중이라 이번 요청을 실행하지 않았어요.'}
            with _PATCH_LOCK:
                return self._apply(actor, request_id, requested_range, values,
                    expected_digest=expected_digest, expected_fingerprint=expected_fingerprint,
                    locked_ledger_id=ledger_id)

    def _apply(self, actor, request_id, requested_range, values, *, expected_digest, expected_fingerprint,
               locked_ledger_id):
        self._authorize(actor)
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            raise PatchError('중복 실행을 대조할 수정 요청 ID가 필요해요.')
        if any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in (expected_digest, expected_fingerprint)):
            raise PatchError('변경 전 셀과 주문 행의 확인값이 필요해요.')
        selected, ledger_id = self._selection(requested_range, values), self._target()
        if ledger_id != locked_ledger_id:
            raise PatchError('원장 연결 대상이 바뀌어 수정하지 않았어요.')
        payload_digest = _hash([actor, ledger_id, selected['a1'], selected['cells'], expected_digest, expected_fingerprint])
        with self._audit() as c:
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT actor,payload_digest,result FROM ledger_patches WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing['actor'] != actor or existing['payload_digest'] != payload_digest:
                    raise PatchError('같은 수정 요청 ID의 계정이나 내용이 달라 다시 실행하지 않았어요.')
                self._authorize(actor)
                prior = json.loads(existing['result'])
                if prior['status'] in ('pending', 'submitting'):
                    prior = {**prior, 'status': 'uncertain', 'summary': '이 수정 요청의 실제 반영 여부가 미확인입니다. 자동으로 다시 실행하지 않아요.'}
                return {**prior, 'reused': True}
            unresolved = c.execute("SELECT 1 FROM ledger_patches WHERE ledger_id=? AND state IN ('checking','submitting','uncertain') LIMIT 1",
                                   (ledger_id,)).fetchone()
            if unresolved:
                return {'status': 'blocked', 'code': 'prior_outcome_uncertain', 'request_id': request_id,
                        'summary': '이전 원장 수정의 실제 반영 여부가 미확인이라 새 수정을 실행하지 않았어요. 먼저 현재 원장과 수정 기록을 대조해야 해요.'}
            initial = {'status': 'pending', 'summary': '이 수정 요청의 처리 결과를 확인 중이에요.', 'request_id': request_id}
            now = time.time()
            c.execute('INSERT INTO ledger_patches VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?)',
                (request_id, actor, ledger_id, selected['a1'], payload_digest, 'checking', now, now,
                 _json(selected['cells']), _json(initial)))
        try:
            before = self._snapshot(ledger_id, selected)
            if before['before_digest'] != expected_digest or before['row_fingerprint'] != expected_fingerprint:
                return self._finish(request_id, 'blocked', '원장 내용이나 해당 주문 행이 바뀌어 수정하지 않았어요. 최신 내용으로 다시 확인해 주세요.', before=before['cells'])
            self._authorize(actor)
        except PermissionError:
            return self._finish(request_id, 'denied', '현재 계정 권한을 확인하지 못해 원장을 수정하지 않았어요.')
        except Exception:
            return self._finish(request_id, 'blocked', '변경 전 원장을 확인하지 못해 수정하지 않았어요.')
        self._finish(request_id, 'submitting', '원장 수정 응답과 재조회 결과를 확인하고 있어요.', before=before['cells'])
        body = {'requests': [{'updateCells': {'range': {'sheetId': before['sheet_id'],
            'startRowIndex': selected['r1'], 'endRowIndex': selected['r2'],
            'startColumnIndex': selected['c1'], 'endColumnIndex': selected['c2']},
            'rows': [{'values': [{'userEnteredValue': value} if value is not None else {} for value in row]}
                     for row in selected['cells']], 'fields': 'userEnteredValue'}}]}
        response = None
        try:
            self._authorize(actor)
        except PermissionError:
            return self._finish(request_id, 'denied', '실행 직전 대표 권한이 변경되어 원장을 수정하지 않았어요.')
        try:
            response = self._transport().post('https://sheets.googleapis.com/v4/spreadsheets/' + ledger_id + ':batchUpdate',
                json=body, timeout=15, allow_redirects=False)
            status = response.status_code
        except Exception:
            return self._finish(request_id, 'uncertain', '원장 수정 응답이 끊겨 반영 여부가 미확인입니다. 같은 요청을 자동 재실행하지 않아요.')
        finally:
            ledger_access._close(response)
        if status in (401, 403):
            return self._finish(request_id, 'denied', '원장 수정 요청이 권한 문제로 거절됐어요. 서버 계정과 보호 범위를 확인해 주세요.')
        if status != 200:
            return self._finish(request_id, 'uncertain', '원장 수정 성공을 확인하지 못했어요. 현재 셀을 대조하기 전 다시 실행하지 않아요.')
        try:
            self._authorize(actor)
            after = self._snapshot(ledger_id, selected)
            self._authorize(actor)
        except Exception:
            return self._finish(request_id, 'uncertain', '수정 요청 뒤 권한 또는 재조회를 확인하지 못해 실제 반영은 미확인입니다.')
        expected_rows = [list(row) for row in before['rows']]
        for index, row in enumerate(selected['cells']):
            expected_rows[index][selected['c1']:selected['c2']] = row
        target_errors = any(selected['r1'] <= row < selected['r2'] and selected['c1'] <= col < selected['c2']
                            for row, col in after['errors'])
        if after['sheet_id'] != before['sheet_id'] or after['rows'] != expected_rows or target_errors:
            return self._finish(request_id, 'uncertain', '수정 후 셀 값·수식 결과 또는 같은 행의 다른 내용이 예상과 달라 확인이 필요해요.', after=after['cells'])
        return self._finish(request_id, 'verified', '지정한 원장 셀을 수정하고 실제 재조회가 일치하는 것까지 확인했어요.', after=after['cells'])
