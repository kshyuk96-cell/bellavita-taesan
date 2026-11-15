import tempfile
from pathlib import Path
import unittest

from manager_bridge.asset_registry import AssetRegistry, AssetUnavailable
from manager_bridge.asset_usage import AssetUsage
from manager_bridge.store import Store, Conflict, NotFound, PermissionDenied, token_hash


class AssetUsageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = 1000
        self.store = Store(self.root / 'state.db', clock=lambda: self.now)
        self.owner = self.store.add_token('owner', 'owner', ['*'])
        self.store.register_project(self.owner, 'common', 'fixture/common')
        self.store.register_project(self.owner, 'other', 'fixture/other')
        self.manager = self.store.add_token('manager', 'manager', ['common'])
        self.worker = self.store.add_token('worker', 'worker', ['common'])
        self.other = self.store.add_token('other', 'manager', ['other'])
        self.registry = AssetRegistry(self.store, self.root / 'assets')
        self.usage = AssetUsage(self.store, self.registry)
        self.ref = self.asset('logo', b'private fixture bytes must not enter usage records')
        self.task = self.store.submit(self.manager, 'fixture', {'project': 'common', 'repository': 'fixture/common',
            'description': 'fixture only', 'base_sha': 'a'*40, 'kind': 'test', 'checks': ['fixture']})
        self.store.approve(self.owner, self.task['id'], 1, self.task['manifest_digest'])
        self.claim = self.store.claim(self.worker, 'common', lease_seconds=10)

    def asset(self, identity, content, project='common', approve=True):
        row = self.registry.register_draft(self.owner, project, identity, 'v1', content)
        if approve:
            self.registry.approve(self.owner, project, identity, 'v1', row['sha256'])
        return {'asset_id': identity, 'version': 'v1', 'sha256': row['sha256']}

    def record(self, **changes):
        args = dict(token=self.worker, task_id=self.task['id'], version=1,
                    manifest_digest=self.task['manifest_digest'], refs=[self.ref], lease_token=self.claim['lease_token'])
        args.update(changes)
        return self.usage.record_worker_usage(**args)

    def observe(self, **changes):
        args = dict(token=self.manager, task_id=self.task['id'], version=1,
                    manifest_digest=self.task['manifest_digest'], refs=[self.ref], evidence_reference='fixture:independent-asset-observation')
        args.update(changes)
        return self.usage.record_manager_observation(**args)

    def test_current_worker_receipt_is_task_bound_and_idempotent(self):
        first = self.record()
        self.assertEqual(first['provenance'], 'worker_claim')
        self.assertEqual(first['manifest_digest'], self.task['manifest_digest'])
        self.assertEqual(first['assets'], [self.ref])
        self.assertFalse(first['rendering_execution_verified'])
        self.assertFalse(first['duplicate'])
        self.assertTrue(self.record()['duplicate'])
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM bridge_asset_usage').fetchone()[0], 1)
            payload = c.execute('SELECT payload FROM bridge_asset_usage').fetchone()[0]
            self.assertNotIn('private fixture bytes', payload)
            self.assertNotIn(self.worker, payload)
            self.assertNotIn(self.claim['lease_token'], payload)

    def test_other_worker_or_manager_cannot_claim_current_worker_usage(self):
        replacement = self.store.add_token('worker', 'worker', ['common'])
        with self.assertRaises(Conflict):
            self.record(token=replacement)
        with self.assertRaises(Conflict):
            self.record(lease_token='b'*64)
        with self.assertRaises(PermissionDenied):
            self.record(token=self.manager)

    def test_expired_cancelled_and_revoked_worker_cannot_record(self):
        self.now += 11
        with self.assertRaises(Conflict):
            self.record()
        self.now = 1000
        self.store.cancel(self.manager, self.task['id'])
        with self.assertRaises(Conflict):
            self.record()
        self.store.revoke_token(self.owner, token_hash(self.worker))
        with self.assertRaises(PermissionDenied):
            self.record()

    def test_wrong_task_binding_or_changed_event_cannot_overwrite_history(self):
        with self.assertRaises(Conflict):
            self.record(version=2)
        with self.assertRaises(Conflict):
            self.record(manifest_digest='f'*64)
        self.record()
        second = self.asset('second', b'new fixture content')
        with self.assertRaises(Conflict):
            self.record(refs=[second])
        self.assertFalse(self.record(refs=[second], usage_key='render-stage-two')['duplicate'])

    def test_cross_project_draft_missing_and_tampered_assets_are_rejected(self):
        other = self.asset('other-only', b'other project fixture', project='other')
        with self.assertRaises(NotFound):
            self.record(refs=[other])
        draft = self.asset('draft', b'draft fixture', approve=False)
        with self.assertRaises(Conflict):
            self.record(refs=[draft])
        with self.assertRaises(NotFound):
            self.record(refs=[self.ref | {'version': 'v99'}])
        (self.registry.root / self.ref['sha256']).write_bytes(b'tampered')
        with self.assertRaises(AssetUnavailable):
            self.record()

    def test_manager_observation_is_separate_from_worker_claim(self):
        self.record()
        row = self.observe()
        self.assertEqual(row['provenance'], 'manager_observation')
        self.assertFalse(row['rendering_execution_verified'])
        self.assertTrue(self.observe()['duplicate'])
        with self.assertRaises(Conflict):
            self.observe(evidence_reference='fixture:different-observation')
        with self.assertRaises(PermissionDenied):
            self.observe(token=self.worker)

    def test_manager_can_trace_asset_usage_without_exposing_raw_data(self):
        self.record()
        self.observe()
        result = self.usage.list_by_asset(self.manager, 'common', 'logo', 'v1', self.ref['sha256'])
        self.assertEqual(len(result['records']), 2)
        self.assertFalse(result['truncated'])
        self.assertTrue(all(row['task_id'] == self.task['id'] for row in result['records']))
        limited = self.usage.list_by_asset(self.manager, 'common', 'logo', 'v1', self.ref['sha256'], limit=1)
        self.assertTrue(limited['truncated'])
        with self.assertRaises(PermissionDenied):
            self.usage.list_by_asset(self.worker, 'common', 'logo', 'v1', self.ref['sha256'])
        with self.assertRaises(NotFound):
            self.usage.list_by_asset(self.other, 'common', 'logo', 'v1', self.ref['sha256'])

    def test_invalid_refs_and_evidence_are_not_stored(self):
        for refs in ([], [self.ref, self.ref], [self.ref | {'content': b'raw'}]):
            with self.assertRaises(ValueError):
                self.record(refs=refs)
        with self.assertRaises(ValueError):
            self.observe(evidence_reference='비공개 문구 원문을 저장하지 않음')
        with self.assertRaises(ValueError):
            self.record(usage_key='../path')

    def test_restart_preserves_immutable_usage_and_no_fictitious_pending_observation(self):
        self.record()
        restarted = AssetUsage(Store(self.root / 'state.db', clock=lambda: self.now), self.registry)
        self.assertEqual(len(restarted.list_by_asset(self.manager, 'common', 'logo', 'v1', self.ref['sha256'])['records']), 1)
        pending = self.store.submit(self.manager, 'pending', {'project': 'common', 'repository': 'fixture/common',
            'description': 'fixture', 'base_sha': 'a'*40, 'kind': 'test', 'checks': ['fixture']})
        with self.assertRaises(Conflict):
            self.observe(task_id=pending['id'], manifest_digest=pending['manifest_digest'])


if __name__ == '__main__':
    unittest.main()
