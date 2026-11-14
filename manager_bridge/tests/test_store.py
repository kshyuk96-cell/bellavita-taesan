import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from manager_bridge.store import Store, Conflict, NotFound, PermissionDenied, canonical_json, digest, token_hash


PROJECT = 'bellavita'
OTHER = 'camp-site'
REPOSITORY = 'fixture/bellavita'
BASE = 'a' * 40
HEAD = 'b' * 40


class StoreTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'private-bridge.sqlite3'
        self.now = 1000.0
        self.store = Store(self.path, clock=lambda: self.now)
        self.owner = self.store.add_token('representative', 'owner', ['*'])
        self.store.register_project(self.owner, PROJECT, REPOSITORY)
        self.store.register_project(self.owner, OTHER, 'fixture/camp-site')
        self.manager = self.store.add_token('taesan', 'manager', [PROJECT])
        self.worker = self.store.add_token('developer', 'worker', [PROJECT])
        self.other_worker = self.store.add_token('other-business', 'worker', [OTHER])
        self.report = {'head_sha': HEAD, 'summary': '가상 CLI 결과, 운영 적용 없음',
                       'checks': [{'name': 'fixture tests', 'passed': True}]}

    def manifest(self, **changes):
        value = {'project': PROJECT, 'repository': REPOSITORY, 'base_sha': BASE,
                 'description': '가상 개발 범위', 'checks': ['가상 회귀 확인'], 'kind': 'development'}
        value.update(changes)
        return value

    def submit(self, key='request-1', **changes):
        return self.store.submit(self.manager, key, self.manifest(**changes))

    def approved(self, key='request-1', **changes):
        task = self.submit(key, **changes)
        return self.store.approve(self.owner, task['id'], task['version'], task['manifest_digest'])

    def claimed(self, key='request-1', **changes):
        self.approved(key, **changes)
        return self.store.claim(self.worker, PROJECT, lease_seconds=10)

    def test_project_scoped_idempotency_keeps_manifest_immutable(self):
        original = self.manifest()
        first = self.store.submit(self.manager, 'same-key', original)
        original['description'] = 'caller mutation'
        self.assertEqual(first['state'], 'pending_approval')
        self.assertEqual(self.store.submit(self.manager, 'same-key', self.manifest())['id'], first['id'])
        self.assertEqual(self.store.get_task(self.manager, first['id'])['manifest']['description'], '가상 개발 범위')
        with self.assertRaises(Conflict):
            self.store.submit(self.manager, 'same-key', self.manifest(description='다른 범위'))
        other = self.store.submit(self.owner, 'same-key', self.manifest(project=OTHER, repository='fixture/camp-site'))
        self.assertNotEqual(first['id'], other['id'])

    def test_only_owner_exact_manifest_version_and_digest_can_queue_work(self):
        task = self.submit()
        self.assertIsNone(self.store.claim(self.worker, PROJECT))
        for version, fingerprint in ((2, task['manifest_digest']), (True, task['manifest_digest']), (1, 'c' * 64)):
            with self.subTest(version=version), self.assertRaises(Conflict):
                self.store.approve(self.owner, task['id'], version, fingerprint)
        with self.assertRaises(PermissionDenied):
            self.store.approve(self.manager, task['id'], 1, task['manifest_digest'])
        self.assertEqual(self.store.approve(self.owner, task['id'], 1, task['manifest_digest'])['state'], 'queued')
        self.assertEqual(self.store.approve(self.owner, task['id'], 1, task['manifest_digest'])['state'], 'queued')

    def test_concurrent_claims_assign_a_task_once(self):
        task = self.approved()
        tokens = [self.store.add_token('worker-' + str(i), 'worker', [PROJECT]) for i in range(8)]
        barrier = threading.Barrier(8)
        def claim(token):
            barrier.wait(timeout=3)
            return self.store.claim(token, PROJECT)
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(claim, tokens))
        actual = [result for result in results if result]
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0]['id'], task['id'])
        self.assertEqual(actual[0]['state'], 'running')
        self.assertEqual(len(actual[0]['lease_token']), 64)

    def test_worker_project_and_task_capabilities_cannot_cross_scope(self):
        task = self.approved()
        test_only = self.store.add_token('tester', 'worker', [PROJECT], capabilities=['test'])
        self.assertIsNone(self.store.claim(test_only, PROJECT))
        with self.assertRaises(NotFound):
            self.store.claim(self.other_worker, PROJECT)
        with self.assertRaises(NotFound):
            self.store.get_task(self.other_worker, task['id'])
        with self.assertRaises(NotFound):
            self.store.list_tasks(self.manager, OTHER)
        test_task = self.approved('test-task', kind='test')
        self.assertEqual(self.store.claim(test_only, PROJECT)['id'], test_task['id'])

    def test_heartbeat_extends_only_current_worker_lease_and_records_bounded_progress(self):
        task = self.claimed()
        lease = task['lease_token']
        self.now += 5
        response = self.store.heartbeat(self.worker, task['id'], lease, lease_seconds=10, progress='가상 검증 중')
        self.assertFalse(response['cancel_requested'])
        self.assertEqual(response['lease_expires_at'], 1015)
        self.assertEqual(self.store.get_task(self.manager, task['id'])['progress'], '가상 검증 중')
        replacement = self.store.add_token('developer', 'worker', [PROJECT])
        with self.assertRaises(Conflict):
            self.store.heartbeat(replacement, task['id'], lease)
        with self.assertRaises(Conflict):
            self.store.heartbeat(self.worker, task['id'], 'f' * 64)

    def test_expired_lease_needs_verification_and_is_never_requeued(self):
        task = self.claimed()
        self.now += 11
        response = self.store.heartbeat(self.worker, task['id'], task['lease_token'])
        self.assertTrue(response['cancel_requested'])
        self.assertEqual(response['state'], 'needs_verification')
        self.assertIsNone(self.store.claim(self.worker, PROJECT))
        with self.assertRaises(Conflict):
            self.store.report(self.worker, task['id'], task['lease_token'], self.report)
        with self.assertRaises(Conflict):
            self.store.approve(self.owner, task['id'], 1, task['manifest_digest'])

    def test_worker_report_is_not_verified_and_manager_review_binds_report_and_manifest(self):
        task = self.claimed()
        result = self.store.report(self.worker, task['id'], task['lease_token'], self.report)
        self.assertEqual(result['state'], 'reported')
        self.assertIsNone(result['review'])
        self.assertNotIn('lease_token', result)
        with self.assertRaises(PermissionDenied):
            self.store.review(self.worker, task['id'], 1, task['manifest_digest'], 'fixture-run:1')
        with self.assertRaises(Conflict):
            self.store.review(self.manager, task['id'], 2, task['manifest_digest'], 'fixture-run:1')
        reviewed = self.store.review(self.manager, task['id'], 1, task['manifest_digest'], 'fixture-run:actual-manager-check')
        self.assertEqual(reviewed['state'], 'reviewed')
        self.assertEqual(reviewed['review']['report_digest'], digest(self.report))
        self.assertEqual(reviewed['review']['scope'], 'manager_record_only')

    def test_cancel_is_owner_manager_only_idempotent_and_rejects_late_finish(self):
        task = self.claimed()
        with self.assertRaises(PermissionDenied):
            self.store.cancel(self.worker, task['id'])
        result = self.store.cancel(self.manager, task['id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(self.store.cancel(self.manager, task['id'])['state'], 'cancelled')
        self.assertTrue(self.store.heartbeat(self.worker, task['id'], task['lease_token'])['cancel_requested'])
        with self.assertRaises(Conflict):
            self.store.report(self.worker, task['id'], task['lease_token'], self.report)

    def test_fresh_revocation_stops_worker_credential_and_requires_verification(self):
        task = self.claimed()
        self.assertTrue(self.store.revoke_token(self.owner, token_hash(self.worker)))
        self.assertFalse(self.store.revoke_token(self.owner, token_hash(self.worker)))
        with self.assertRaises(PermissionDenied):
            self.store.report(self.worker, task['id'], task['lease_token'], self.report)
        self.assertEqual(self.store.get_task(self.manager, task['id'])['state'], 'needs_verification')
        replacement = self.store.add_token('developer', 'worker', [PROJECT])
        with self.assertRaises(Conflict):
            self.store.report(replacement, task['id'], task['lease_token'], self.report)

    def test_expired_credentials_and_non_owner_wildcard_registration_are_rejected(self):
        for role in ('manager', 'worker'):
            with self.subTest(role=role), self.assertRaises(ValueError):
                self.store.add_token('invalid', role, ['*'])
        with self.assertRaises(ValueError):
            self.store.add_token('invalid', 'manager', ['not-registered'])
        expires = self.store.add_token('temporary', 'worker', [PROJECT], expires_at=1002)
        self.now = 1002
        with self.assertRaises(PermissionDenied):
            self.store.authenticate(expires)
        with self.assertRaises(PermissionDenied):
            self.store.register_project(self.manager, 'third', 'fixture/third')

    def test_registry_repository_cannot_be_redirected_by_manifest_or_second_registration(self):
        with self.assertRaises(Conflict):
            self.submit(repository='attacker/other')
        with self.assertRaises(Conflict):
            self.store.register_project(self.owner, PROJECT, 'fixture/changed')
        self.assertEqual(self.store.list_tasks(self.manager, PROJECT), [])

    def test_same_named_replacement_credential_cannot_complete_a_current_lease(self):
        task = self.claimed()
        replacement = self.store.add_token('developer', 'worker', [PROJECT])
        with self.assertRaises(Conflict):
            self.store.report(replacement, task['id'], task['lease_token'], self.report)
        self.assertEqual(self.store.get_task(self.manager, task['id'])['state'], 'running')

    def test_plaintext_bearer_and_lease_tokens_are_never_persisted_or_returned_in_lists(self):
        task = self.claimed()
        public = self.store.get_task(self.manager, task['id'])
        self.assertNotIn('lease_token', public)
        self.assertNotIn('token_hash', public)
        for value in (self.owner, self.manager, self.worker, task['lease_token']):
            self.assertNotIn(value, canonical_json(self.store.list_tasks(self.manager, PROJECT)))
        with self.assertRaises(ValueError):
            self.store.report(self.worker, task['id'], task['lease_token'],
                {**self.report, 'summary': 'accidental token ' + self.worker})
        with sqlite3.connect(self.path) as c:
            dump = '\n'.join(c.iterdump())
        for value in (self.owner, self.manager, self.worker, task['lease_token']):
            self.assertNotIn(value, dump)

    def test_invalid_manifests_and_false_verification_report_fields_do_not_mutate_queue(self):
        for changes in ({'base_sha': 'short'}, {'kind': 'deploy'}, {'checks': []},
                        {'checks': ['x' * 301]}, {'description': ''}, {'project': '../other'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.submit(**changes)
        task = self.claimed()
        for report in ({**self.report, 'verified': True},
                       {**self.report, 'checks': [{'name': 'fixture', 'passed': 'true'}]}):
            with self.assertRaises(ValueError):
                self.store.report(self.worker, task['id'], task['lease_token'], report)
        self.assertEqual(self.store.get_task(self.manager, task['id'])['state'], 'running')

    def test_restart_preserves_queue_leases_and_owner_approvals(self):
        task = self.claimed()
        restarted = Store(self.path, clock=lambda: self.now)
        self.assertEqual(restarted.get_task(self.manager, task['id'])['approval']['manifest_digest'], task['manifest_digest'])
        self.assertIsNone(restarted.claim(self.worker, PROJECT))
        result = restarted.report(self.worker, task['id'], task['lease_token'], self.report)
        self.assertEqual(result['state'], 'reported')

    def test_scoped_owner_cannot_revoke_another_project_connection(self):
        scoped = self.store.add_token('project-owner', 'owner', [PROJECT])
        with self.assertRaises(PermissionDenied):
            self.store.revoke_token(scoped, token_hash(self.other_worker))
        with self.assertRaises(PermissionDenied):
            self.store.revoke_token(scoped, token_hash(self.owner))


if __name__ == '__main__':
    unittest.main()
