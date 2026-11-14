import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from manager_bridge.native_handoff import NativeHandoff, HandoffBlocked
from manager_bridge.store import Store, token_hash


class Client:
    project = 'fixture'

    def __init__(self, store, token):
        self.store, self.token = store, token
        self.claims = self.reports = 0
        self.lose_claim = self.lose_report = False
        self.last_task = None

    def claim(self):
        self.claims += 1
        self.last_task = self.store.claim(self.token, self.project, lease_seconds=30)
        if self.lose_claim:
            raise ConnectionError('private transport error')
        return self.last_task

    def heartbeat(self, task):
        result = self.store.heartbeat(self.token, task['id'], task['lease_token'], lease_seconds=30)
        task['lease_expires_at'] = result['lease_expires_at']
        return result

    def report(self, task, report):
        self.reports += 1
        result = self.store.report(self.token, task['id'], task['lease_token'], report)
        if self.lose_report:
            raise ConnectionError('report response lost after commit')
        return result


class NativeHandoffTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.path = root / 'native.sqlite3'
        self.store = Store(root / 'queue.sqlite3', clock=lambda: 1000)
        self.owner = self.store.add_token('owner', 'owner', ['*'])
        self.store.register_project(self.owner, 'fixture', 'fixture/repository')
        self.manager = self.store.add_token('manager', 'manager', ['fixture'])
        self.worker = self.store.add_token('worker', 'worker', ['fixture'])
        self.client = Client(self.store, self.worker)
        manifest = dict(project='fixture', repository='fixture/repository', base_sha='a'*40,
                        description='fixture read-only check', checks=['fixture checks'], kind='test')
        task = self.store.submit(self.manager, 'native-1', manifest)
        self.task = self.store.approve(self.owner, task['id'], task['version'], task['manifest_digest'])
        self.report = {'head_sha': 'a'*40, 'summary': 'actual fixture result only',
                       'checks': [{'name': 'fixture', 'passed': True}]}
        self.native = self.reopen()

    def tearDown(self):
        self.folder.cleanup()

    def reopen(self, **options):
        read = options.pop('read_task', lambda task_id: self.store.get_task(self.worker, task_id))
        return NativeHandoff(self.path, self.client, '/root/registered_worker', read_task=read, **options)

    def dispatch(self):
        self.native.begin()
        self.native.prepare_dispatch()
        self.native.dispatched('platform:fixture-receipt-1')

    def test_flow_hides_secrets_and_waits_for_independent_review(self):
        assignment = self.native.begin()
        self.assertEqual(assignment['manifest_digest'], self.task['manifest_digest'])
        text = json.dumps(assignment)
        self.assertNotIn(self.worker, text)
        self.assertNotIn(self.client.last_task['lease_token'], text)
        self.assertFalse(assignment['execution_verified'])
        self.native.prepare_dispatch()
        self.native.dispatched('platform:fixture-receipt-1')
        self.assertEqual(self.native.finish(self.report)['phase'], 'reported')
        with self.assertRaises(HandoffBlocked):
            self.native.release_reviewed()
        self.store.review(self.manager, self.task['id'], self.task['version'],
                          self.task['manifest_digest'], 'fixture-independent-log')
        self.assertEqual(self.native.release_reviewed()['phase'], 'idle')
        with sqlite3.connect(self.path) as c:
            events = c.execute('SELECT phase,receipt,report_digest FROM native_handoff_events').fetchall()
        self.assertTrue(any(row[0]=='dispatched' and row[1] for row in events))
        self.assertTrue(any(row[0]=='reported' and row[2] for row in events))

    def test_restart_resumes_same_agent_without_reclaim_or_redispatch(self):
        self.dispatch()
        reopened = self.reopen()
        state = reopened.begin()
        self.assertEqual(state['phase'], 'dispatched')
        self.assertEqual(self.client.claims, 1)
        with self.assertRaises(HandoffBlocked):
            reopened.prepare_dispatch()

    def test_crash_between_dispatch_intent_and_receipt_does_not_resend(self):
        self.native.begin()
        self.native.prepare_dispatch()
        reopened = self.reopen()
        with self.assertRaises(HandoffBlocked):
            reopened.begin()
        self.assertEqual(reopened.reconcile()['phase'], 'needs_verification')
        self.assertEqual(self.client.claims, 1)

    def test_report_lost_after_remote_commit_is_reconciled_without_repost(self):
        self.dispatch()
        self.client.lose_report = True
        with self.assertRaises(HandoffBlocked):
            self.native.finish(self.report)
        self.assertEqual(self.native.snapshot()['phase'], 'report_uncertain')
        self.assertEqual(self.reopen().reconcile()['phase'], 'reported')
        self.assertEqual(self.client.reports, 1)

    def test_claim_response_lost_blocks_second_claim(self):
        self.client.lose_claim = True
        with self.assertRaises(HandoffBlocked):
            self.native.begin()
        self.assertEqual(self.native.snapshot()['phase'], 'claim_uncertain')
        with self.assertRaises(HandoffBlocked):
            self.reopen().begin()
        self.assertEqual(self.client.claims, 1)

    def test_late_remote_commit_can_be_reconciled_after_initial_running_read(self):
        self.dispatch()
        original = self.client.report
        def in_flight(task, report):
            self.client.reports += 1
            raise ConnectionError('request still in flight')
        self.client.report = in_flight
        with self.assertRaises(HandoffBlocked):
            self.native.finish(self.report)
        self.assertEqual(self.native.reconcile()['phase'], 'needs_verification')
        lease = self.client.last_task
        self.store.report(self.worker, lease['id'], lease['lease_token'], self.report)
        self.assertEqual(self.native.reconcile()['phase'], 'reported')
        self.assertEqual(self.client.reports, 1)
        self.client.report = original

    def test_mismatched_report_ack_is_reconciled_not_treated_as_saved(self):
        self.dispatch()
        original = self.client.report
        def malformed(task, report):
            saved = original(task, report)
            saved['manifest_digest'] = 'b'*64
            return saved
        self.client.report = malformed
        with self.assertRaises(HandoffBlocked):
            self.native.finish(self.report)
        self.assertEqual(self.native.reconcile()['phase'], 'reported')
        self.assertEqual(self.client.reports, 1)

    def test_cancel_or_revocation_stops_before_dispatch(self):
        self.native.begin()
        self.store.cancel(self.manager, self.task['id'])
        with self.assertRaises(HandoffBlocked):
            self.native.prepare_dispatch()
        self.assertEqual(self.native.snapshot()['phase'], 'needs_verification')
        self.assertEqual(self.client.reports, 0)

    def test_revoked_token_cannot_resume_dispatched_work(self):
        self.dispatch()
        self.store.revoke_token(self.owner, token_hash(self.worker))
        with self.assertRaises(HandoffBlocked):
            self.reopen().begin()

    def test_changed_agent_or_remote_manifest_is_not_accepted(self):
        self.dispatch()
        with self.assertRaises(HandoffBlocked):
            NativeHandoff(self.path, self.client, '/root/different_agent')
        def changed(task_id):
            result = copy.deepcopy(self.store.get_task(self.worker, task_id))
            result['manifest']['base_sha'] = 'b'*40
            return result
        with self.assertRaises(HandoffBlocked):
            self.reopen(read_task=changed).reconcile()

    def test_private_permissions_and_report_before_dispatch(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.native.begin()
        with self.assertRaises(HandoffBlocked):
            self.native.finish(self.report)
        self.assertEqual(self.client.reports, 0)
        self.path.chmod(0o644)
        with self.assertRaises(HandoffBlocked):
            self.native.snapshot()
