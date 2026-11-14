import copy
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import ledger_access
import ledger_patch


TARGET = 'fixture_ledger_1234567890'


class Response:
    def __init__(self, payload=None, status=200):
        self.payload, self.status_code = payload or {}, status
    def json(self):
        return copy.deepcopy(self.payload)
    def close(self):
        pass


class SheetTransport:
    def __init__(self, title='액자화환'):
        self.title = title
        self.cells = {(4, 0): {'stringValue': 'immutable-fixture-order'},
                      (4, 1): {'stringValue': 'old'}, (4, 2): {'boolValue': False}}
        self.gets, self.posts = [], []
        self.errors = set()
        self.post_status = 200
        self.raise_after_post = False
        self.after_write = None
        self.entered, self.release = None, None

    def get(self, url, **kwargs):
        self.gets.append((url, copy.deepcopy(kwargs)))
        self.assert_url(url)
        params = kwargs['params']
        if 'gridProperties' in params['fields']:
            return Response({'spreadsheetId': TARGET, 'sheets': [{'properties': {
                'sheetId': 0, 'title': self.title, 'gridProperties': {'rowCount': 20, 'columnCount': 4}}}]})
        match = re.search(r'!A([0-9]+):[A-Z]+([0-9]+)$', params['ranges'])
        start, end = int(match[1]) - 1, int(match[2])
        rows = []
        for row in range(start, end):
            values = []
            for col in range(4):
                value = {'userEnteredValue': copy.deepcopy(self.cells[(row, col)])} if (row, col) in self.cells else {}
                if (row, col) in self.errors:
                    value['effectiveValue'] = {'errorValue': {'type': 'REF', 'message': 'private-fixture'}}
                values.append(value)
            rows.append({'values': values})
        return Response({'spreadsheetId': TARGET, 'sheets': [{'properties': {'sheetId': 0, 'title': self.title},
            'data': [{'startRow': start, 'startColumn': 0, 'rowData': rows}]}]})

    @staticmethod
    def assert_url(url):
        if url != 'https://sheets.googleapis.com/v4/spreadsheets/' + TARGET:
            raise AssertionError('unexpected fixture endpoint')

    def post(self, url, **kwargs):
        if url != 'https://sheets.googleapis.com/v4/spreadsheets/' + TARGET + ':batchUpdate':
            raise AssertionError('unexpected fixture endpoint')
        self.posts.append((url, copy.deepcopy(kwargs)))
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError('fixture barrier')
        if self.post_status == 200:
            change = kwargs['json']['requests'][0]['updateCells']
            target = change['range']
            for row_offset, row in enumerate(change['rows']):
                for col_offset, cell in enumerate(row['values']):
                    index = (target['startRowIndex'] + row_offset, target['startColumnIndex'] + col_offset)
                    if 'userEnteredValue' in cell:
                        self.cells[index] = copy.deepcopy(cell['userEnteredValue'])
                    else:
                        self.cells.pop(index, None)
            if self.after_write:
                self.after_write()
        if self.raise_after_post:
            raise TimeoutError('private-token fixture timeout after server commit')
        return Response(status=self.post_status)


class LedgerPatchTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.audit = Path(self.folder) / 'private-audit.sqlite3'
        self.roles = {'owner': 'admin', 'other': 'admin', 'staff': 'staff'}
        self.sheet = SheetTransport()
        self.env = {'TAESAN_ORDER_SHEET_ID': TARGET}
        self.service = self.make_service()

    def make_service(self, **kwargs):
        return ledger_patch.LedgerPatchService(self.env, role_for=lambda actor: self.roles.get(actor),
            audit_path=self.audit, session=self.sheet, **kwargs)

    def prepared(self, values=None, a1='B5'):
        values = [['new']] if values is None else values
        return self.service.prepare('owner', a1, values)

    def apply(self, prepared, values=None, a1='B5', request='request_fixture_001', actor='owner'):
        return self.service.apply(actor, request, a1, [['new']] if values is None else values,
            expected_digest=prepared['before_digest'], expected_fingerprint=prepared['row_fingerprint'])

    def test_exact_admin_patch_uses_only_target_mask_and_actual_reread_before_success(self):
        prepared = self.prepared()
        result = self.apply(prepared)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(self.sheet.cells[(4, 1)], {'stringValue': 'new'})
        self.assertEqual(self.sheet.cells[(4, 0)], {'stringValue': 'immutable-fixture-order'})
        request = self.sheet.posts[0][1]
        self.assertFalse(request['allow_redirects'])
        change = request['json']['requests'][0]['updateCells']
        self.assertEqual(change['fields'], 'userEnteredValue')
        self.assertEqual(change['range'], {'sheetId': 0, 'startRowIndex': 4, 'endRowIndex': 5,
                                          'startColumnIndex': 1, 'endColumnIndex': 2})
        self.assertEqual(len(self.sheet.gets), 6)  # prepare + before + actual after

    def test_request_id_retry_never_reposts_and_different_payload_or_owner_is_rejected(self):
        prepared = self.prepared()
        self.assertEqual(self.apply(prepared)['status'], 'verified')
        self.assertTrue(self.apply(prepared)['reused'])
        self.assertEqual(len(self.sheet.posts), 1)
        for changes in ({'values': [['different']]}, {'actor': 'other'}):
            with self.subTest(changes=changes), self.assertRaises(ledger_patch.PatchError):
                self.apply(prepared, **changes)
        self.assertEqual(len(self.sheet.posts), 1)

    def test_row_reuse_or_edit_outside_target_blocks_before_any_write(self):
        prepared = self.prepared()
        self.sheet.cells[(4, 0)] = {'stringValue': 'reused-row-new-order'}
        self.assertEqual(self.apply(prepared)['status'], 'blocked')
        self.assertEqual(self.sheet.posts, [])
        self.assertEqual(self.sheet.cells[(4, 1)], {'stringValue': 'old'})

    def test_non_admin_or_changed_role_after_prepare_never_posts(self):
        with self.assertRaises(PermissionError):
            self.service.prepare('staff', 'B5', [['new']])
        self.assertEqual(self.sheet.gets, [])
        prepared = self.prepared()
        self.roles['owner'] = 'staff'
        with self.assertRaises(PermissionError):
            self.apply(prepared)
        self.assertEqual(self.sheet.posts, [])

    def test_role_revoked_during_audit_is_rechecked_immediately_before_post(self):
        prepared = self.prepared()
        original = self.service._finish
        def finish(request, status, summary, **kwargs):
            result = original(request, status, summary, **kwargs)
            if status == 'submitting':
                self.roles['owner'] = 'staff'
            return result
        with patch.object(self.service, '_finish', side_effect=finish):
            self.assertEqual(self.apply(prepared)['status'], 'denied')
        self.assertEqual(self.sheet.posts, [])

    def test_role_revoked_after_write_is_uncertain_without_exposing_success(self):
        prepared = self.prepared()
        self.sheet.after_write = lambda: self.roles.update(owner='staff')
        self.assertEqual(self.apply(prepared)['status'], 'uncertain')
        self.assertEqual(len(self.sheet.posts), 1)

    def test_timeout_after_remote_commit_is_uncertain_not_retried_and_blocks_new_request(self):
        prepared = self.prepared()
        self.sheet.raise_after_post = True
        result = self.apply(prepared)
        self.assertEqual(result['status'], 'uncertain')
        self.assertNotIn('private-token', json.dumps(result))
        self.assertEqual(self.apply(prepared)['status'], 'uncertain')
        self.assertEqual(len(self.sheet.posts), 1)
        fresh = self.prepared([['another']])
        blocked = self.apply(fresh, [['another']], request='request_fixture_002')
        self.assertEqual(blocked['code'], 'prior_outcome_uncertain')
        self.assertEqual(len(self.sheet.posts), 1)

    def test_actual_permission_rejection_is_denied_not_success(self):
        prepared = self.prepared()
        self.sheet.post_status = 403
        self.assertEqual(self.apply(prepared)['status'], 'denied')
        self.assertEqual(self.sheet.cells[(4, 1)], {'stringValue': 'old'})

    def test_plain_formula_text_stays_literal_and_explicit_formula_clear_boolean_are_typed(self):
        values = [['=not-an-executable-formula', {'formula': '=1+1'}, True]]
        prepared = self.prepared(values, 'B5:D5')
        self.assertEqual(self.apply(prepared, values, 'B5:D5')['status'], 'verified')
        self.assertEqual(self.sheet.cells[(4, 1)], {'stringValue': '=not-an-executable-formula'})
        self.assertEqual(self.sheet.cells[(4, 2)], {'formulaValue': '=1+1'})
        self.assertEqual(self.sheet.cells[(4, 3)], {'boolValue': True})
        clear = self.prepared([[None]])
        self.assertEqual(self.apply(clear, [[None]], request='request_fixture_002')['status'], 'verified')
        self.assertNotIn((4, 1), self.sheet.cells)

    def test_new_formula_error_or_unrelated_same_row_edit_prevents_verified_status(self):
        prepared = self.prepared()
        self.sheet.after_write = lambda: self.sheet.errors.add((4, 1))
        self.assertEqual(self.apply(prepared)['status'], 'uncertain')
        self.assertEqual(len(self.sheet.posts), 1)

    def test_bounded_explicit_range_and_exact_shape_are_required_before_reads(self):
        for a1, values in (('B:B', [['x']]), ('A1:Z20', [['x']]), ('B5:A5', [['x']]),
                           ("'다른탭'!B5", [['x']]), ('B5:C5', [['x']]), ('B0', [['x']]),
                           ('B5', [[float('nan')]]), ('B5', [[{'formula': 'no-equals'}]])):
            with self.subTest(a1=a1), self.assertRaises(ledger_patch.PatchError):
                self.service.prepare('owner', a1, values)
        self.assertEqual(self.sheet.gets, [])

    def test_configured_exact_tab_allowlist_supports_another_existing_tab_without_arbitrary_file(self):
        self.env['TAESAN_LEDGER_ALLOWED_TABS'] = '액자화환,재고'
        self.sheet.title = '재고'
        prepared = self.service.prepare('owner', "'재고'!B5", [['new']])
        self.assertEqual(self.apply(prepared, a1="'재고'!B5")['status'], 'verified')
        self.assertEqual(prepared['ledger_id'], TARGET)

    def test_private_durable_audit_keeps_before_actual_after_and_request_without_logging(self):
        self.apply(self.prepared())
        self.assertEqual(stat.S_IMODE(self.audit.stat().st_mode), 0o600)
        with sqlite3.connect(self.audit) as c:
            row = c.execute('SELECT actor,state,before_cells,after_cells FROM ledger_patches').fetchone()
        self.assertEqual(row[0:2], ('owner', 'verified'))
        self.assertEqual(json.loads(row[2]), [[{'stringValue': 'old'}]])
        self.assertEqual(json.loads(row[3]), [[{'stringValue': 'new'}]])

    def test_owned_transport_closes_but_injected_transport_is_caller_owned(self):
        with ledger_patch.LedgerPatchService(self.env, role_for=lambda _: 'admin', audit_path=self.audit,
                                            session=self.sheet):
            pass
        self.sheet.close = Mock()
        self.service.close()
        self.sheet.close.assert_not_called()
        with patch.object(ledger_access, 'load_server_credentials', return_value=object()), \
             patch.object(ledger_access, 'authorized_session', return_value=self.sheet):
            owned = ledger_patch.LedgerPatchService(self.env, role_for=lambda _: 'admin', audit_path=self.audit)
            owned.prepare('owner', 'B5', [['new']])
            owned.close()
            owned.close()
        self.sheet.close.assert_called_once()

    def test_different_request_ids_cannot_post_concurrently_from_two_service_instances(self):
        prepared = self.prepared()
        second = self.make_service()
        self.sheet.entered, self.sheet.release = threading.Event(), threading.Event()
        self.addCleanup(self.sheet.release.set)
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(self.apply, prepared)
            try:
                self.assertTrue(self.sheet.entered.wait(3))
                blocked = second.apply('owner', 'request_fixture_002', 'B5', [['new']],
                    expected_digest=prepared['before_digest'], expected_fingerprint=prepared['row_fingerprint'])
                self.assertEqual(blocked['code'], 'ledger_busy')
                self.assertEqual(len(self.sheet.posts), 1)
            finally:
                self.sheet.release.set()
            self.assertEqual(future.result(timeout=3)['status'], 'verified')
        stale = second.apply('owner', 'request_fixture_002', 'B5', [['new']],
            expected_digest=prepared['before_digest'], expected_fingerprint=prepared['row_fingerprint'])
        self.assertEqual(stale['status'], 'blocked')
        self.assertEqual(len(self.sheet.posts), 1)

    def test_advisory_lock_is_nonblocking_across_processes_and_rejects_symlinks(self):
        child = '''import sys
from pathlib import Path
import ledger_patch
s=ledger_patch.LedgerPatchService({'TAESAN_SHEET_ID':sys.argv[1]},role_for=lambda _: 'admin',audit_path=Path(sys.argv[2]))
with s._ledger_lock(sys.argv[1]) as locked: print('acquired' if locked else 'blocked')
'''
        with self.service._ledger_lock(TARGET) as locked:
            self.assertTrue(locked)
            result = subprocess.run([sys.executable, '-c', child, TARGET, str(self.audit)],
                capture_output=True, text=True, check=True, timeout=5,
                env={'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(Path(ledger_patch.__file__).parent)})
            self.assertEqual(result.stdout.strip(), 'blocked')
        lock_path = self.audit.parent / ('.ledger-' + ledger_patch._hash(TARGET) + '.lock')
        lock_path.unlink()
        safe = self.audit.parent / 'unrelated-fixture'
        safe.write_text('unchanged', encoding='utf-8')
        lock_path.symlink_to(safe)
        with self.assertRaises(ledger_patch.PatchError):
            self.apply(self.prepared())
        self.assertEqual(safe.read_text(encoding='utf-8'), 'unchanged')


if __name__ == '__main__':
    unittest.main()
