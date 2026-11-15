import json
import tempfile
import threading
import unittest
from pathlib import Path

from manager_bridge.control import request, secret, validate_url, write_secret
from manager_bridge.server import make_server
from manager_bridge.store import Store
from manager_bridge.session_dispatch import SessionTarget
from manager_bridge.session_worker import SessionRunner
from manager_bridge.worker import WorkerClient
from manager_bridge.native_handoff import NativeHandoff


class HttpBridgeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / 'manager.sqlite3')
        self.owner = self.store.add_token('owner', 'owner', ['*'])
        self.store.register_project(self.owner, 'bellavita', 'fixture/flowers')
        self.store.register_project(self.owner, 'camp', 'fixture/camp')
        self.manager = self.store.add_token('taesan', 'manager', ['bellavita'])
        self.worker = self.store.add_token('developer', 'worker', ['bellavita'])
        self.other = self.store.add_token('other', 'worker', ['camp'])
        self.server = make_server(self.store, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.manifest = dict(project='bellavita', repository='fixture/flowers', base_sha='a'*40,
                             description='가상 연결 시험만 수행', checks=['가상 결과 대조'], kind='test')

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.folder.cleanup()

    def post(self, token, path, body):
        return request(self.base, token, 'POST', path, body)

    def submit(self):
        response = self.post(self.manager, '/v1/tasks', {'request_key': 'fixture-request-1', 'manifest': self.manifest})
        self.assertTrue(response.get('ok'), response)
        return response['task']

    def test_project_overview_requires_manager_and_omits_private_manifest(self):
        task = self.submit()
        response = request(self.base, self.manager, 'GET', '/v1/overview?project=bellavita')
        self.assertEqual(response['overview']['counts']['pending_approval'], 1)
        self.assertEqual(response['overview']['actions'][0]['task_id'], task['id'])
        self.assertNotIn(self.manifest['description'], json.dumps(response))
        self.assertEqual(request(self.base, self.worker, 'GET', '/v1/overview?project=bellavita')['status'], 403)
        self.assertEqual(request(self.base, self.manager, 'GET', '/v1/overview?project=camp')['status'], 404)

    def test_real_http_approval_claim_report_and_independent_review(self):
        task = self.submit()
        self.assertEqual(task['state'], 'pending_approval')
        self.assertIsNone(self.post(self.worker, '/v1/worker/claim', {'project': 'bellavita'})['task'])
        approval = {'version': task['version'], 'digest': task['manifest_digest']}
        self.assertEqual(self.post(self.manager, '/v1/tasks/'+task['id']+'/approve', approval)['status'], 403)
        self.assertTrue(self.post(self.owner, '/v1/tasks/'+task['id']+'/approve', approval)['ok'])
        lease = self.post(self.worker, '/v1/worker/claim', {'project': 'bellavita'})['task']
        self.assertEqual(lease['id'], task['id'])
        self.assertIn('lease_token', lease)
        shown = request(self.base, self.manager, 'GET', '/v1/tasks/'+task['id'])['task']
        self.assertNotIn('lease_token', shown)
        report = {'summary': '가상 결과 제출', 'head_sha': 'b'*40,
                  'checks': [{'name': 'fixture', 'passed': True}]}
        saved = self.post(self.worker, '/v1/worker/report',
            {'id': task['id'], 'lease_token': lease['lease_token'], 'report': report})
        self.assertEqual(saved['task']['state'], 'reported')
        self.assertNotEqual(saved['task']['state'], 'verified')
        review = {**approval, 'evidence_reference': 'fixture-review-log-1'}
        self.assertEqual(self.post(self.worker, '/v1/tasks/'+task['id']+'/review', review)['status'], 403)
        reviewed = self.post(self.manager, '/v1/tasks/'+task['id']+'/review', review)
        self.assertEqual(reviewed['task']['state'], 'reviewed')

    def test_cross_project_access_and_role_in_payload_cannot_grant_scope(self):
        task = self.submit()
        result = request(self.base, self.other, 'GET', '/v1/tasks/'+task['id'])
        self.assertIn(result['status'], (403, 404))
        result = self.post(self.other, '/v1/worker/claim', {'project': 'bellavita', 'role': 'owner'})
        self.assertIn(result['status'], (403, 404))

    def test_cancelled_work_rejects_late_report(self):
        task = self.submit()
        self.post(self.owner, '/v1/tasks/'+task['id']+'/approve',
                  {'version': task['version'], 'digest': task['manifest_digest']})
        lease = self.post(self.worker, '/v1/worker/claim', {'project': 'bellavita'})['task']
        self.assertTrue(self.post(self.manager, '/v1/tasks/'+task['id']+'/cancel', {})['ok'])
        late = self.post(self.worker, '/v1/worker/report', {'id': task['id'], 'lease_token': lease['lease_token'],
            'report': {'summary': 'late', 'checks': [{'name': 'fixture', 'passed': True}], 'head_sha': 'a'*40}})
        self.assertEqual(late['status'], 409)

    def test_unknown_token_has_no_task_access(self):
        result = request(self.base, 'invalid-token-that-must-not-authenticate', 'GET', '/v1/tasks?project=bellavita')
        self.assertEqual(result['status'], 403)

    def test_tokens_are_private_files_and_remote_plain_http_is_refused(self):
        target = Path(self.folder.name) / 'fixture.token'
        write_secret(target, self.owner)
        self.assertEqual(secret(target), self.owner)
        target.chmod(0o644)
        with self.assertRaises(ValueError):
            secret(target)
        with self.assertRaises(ValueError):
            validate_url('http://example.invalid')
        with self.assertRaises(ValueError):
            validate_url('https://user:secret@example.invalid')

    def test_registered_session_worker_uses_live_http_lease_without_claiming_execution(self):
        task = self.submit()
        self.post(self.owner, '/v1/tasks/'+task['id']+'/approve',
                  {'version': task['version'], 'digest': task['manifest_digest']})
        root = Path(self.folder.name)
        home, work = root / 'codex-home', root / 'checkout'
        home.mkdir(mode=0o700)
        work.mkdir()
        binary = root / 'fake-codex'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o700)
        token_file = root / 'worker.token'
        write_secret(token_file, self.worker)
        registry = {'developer': SessionTarget('bellavita', 'fixture/flowers',
            '12345678-1234-1234-1234-123456789abc', str(home), str(work), str(binary))}
        client = WorkerClient(self.base, token_file, 'bellavita')
        result = client.run_once(SessionRunner('developer', registry))
        self.assertEqual(result['status'], 'reported')
        saved = self.store.get_task(self.manager, task['id'])
        self.assertEqual(saved['state'], 'reported')
        self.assertEqual(saved['report']['head_sha'], self.manifest['base_sha'])
        checks = {check['name']: check['passed'] for check in saved['report']['checks']}
        self.assertTrue(checks['session_message_queued'])
        self.assertFalse(checks['session_execution_verified'])

    def test_native_receipts_reconcile_over_real_http_and_release_after_review(self):
        task = self.submit()
        approval = {'version': task['version'], 'digest': task['manifest_digest']}
        self.post(self.owner, '/v1/tasks/'+task['id']+'/approve', approval)
        root = Path(self.folder.name)
        token_file = root / 'native-worker.token'
        write_secret(token_file, self.worker)
        client = WorkerClient(self.base, token_file, 'bellavita')
        native = NativeHandoff(root/'native-state.sqlite3', client, '/root/fixture_worker')
        self.assertEqual(native.begin()['task_id'], task['id'])
        self.assertNotIn('lease_token', json.dumps(native.prepare_dispatch()))
        native.dispatched('fixture-platform:reported-reference')
        report = {'summary': 'fixture callback only', 'head_sha': self.manifest['base_sha'],
                  'checks': [{'name': 'fixture-only', 'passed': True}]}
        self.assertEqual(native.finish(report)['phase'], 'reported')
        reopened = NativeHandoff(root/'native-state.sqlite3', client, '/root/fixture_worker')
        self.assertEqual(reopened.reconcile()['phase'], 'reported')
        review = self.post(self.manager, '/v1/tasks/'+task['id']+'/review',
                           {**approval, 'evidence_reference': 'fixture-independent-review'})
        self.assertTrue(review['ok'])
        self.assertEqual(reopened.release_reviewed()['phase'], 'idle')
