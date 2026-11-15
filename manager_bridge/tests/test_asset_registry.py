import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from manager_bridge.asset_registry import AssetRegistry, AssetUnavailable, MAX_ASSET_BYTES
from manager_bridge.store import Conflict, NotFound, PermissionDenied, Store, token_hash


class AssetRegistryTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.base = Path(self.folder)
        self.now = 1000
        self.store = Store(self.base / 'state.sqlite3', clock=lambda: self.now)
        self.owner = self.store.add_token('owner', 'owner', ['*'])
        self.store.register_project(self.owner, 'bellavita', 'fixture/assets')
        self.store.register_project(self.owner, 'other', 'fixture/other')
        self.manager = self.store.add_token('manager', 'manager', ['bellavita'])
        self.worker = self.store.add_token('worker', 'worker', ['bellavita'])
        self.other = self.store.add_token('other', 'manager', ['other'])
        self.cache = self.base / 'asset-cache'
        self.registry = AssetRegistry(self.store, self.cache)
        self.content = b'fixture asset bytes, not a production image'
        self.sha = hashlib.sha256(self.content).hexdigest()

    def draft(self, **changes):
        args = dict(token=self.manager, project='bellavita', asset_id='sample', version='v1', content=self.content)
        args.update(changes)
        return self.registry.register_draft(**args)

    def approved(self):
        self.draft()
        return self.registry.approve(self.owner, 'bellavita', 'sample', 'v1', self.sha)

    def resolve(self, token=None, **changes):
        args = dict(token=token or self.worker, project='bellavita', asset_id='sample', version='v1', expected_sha256=self.sha)
        args.update(changes)
        return self.registry.resolve(**args)

    def test_draft_requires_owner_exact_hash_approval_before_worker_can_resolve(self):
        draft = self.draft()
        self.assertEqual(draft['status'], 'draft')
        with self.assertRaises(Conflict):
            self.resolve()
        with self.assertRaises(PermissionDenied):
            self.registry.approve(self.manager, 'bellavita', 'sample', 'v1', self.sha)
        with self.assertRaises(Conflict):
            self.registry.approve(self.owner, 'bellavita', 'sample', 'v1', 'a'*64)
        approved = self.registry.approve(self.owner, 'bellavita', 'sample', 'v1', self.sha)
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(self.resolve()['content'], self.content)
        self.assertEqual(approved['approved_actor'], 'owner')
        self.assertNotIn('path', approved)

    def test_same_input_is_idempotent_and_different_content_or_kind_cannot_replace_version(self):
        first = self.draft()
        self.assertEqual(self.draft(), first)
        with self.assertRaises(Conflict):
            self.draft(content=b'different fixture bytes')
        with self.assertRaises(Conflict):
            self.draft(kind='template')
        self.approved()
        self.assertEqual(self.draft()['status'], 'approved')
        self.assertEqual(self.resolve()['content'], self.content)

    def test_new_version_is_separately_approved_even_with_identical_content(self):
        self.approved()
        self.draft(version='v2')
        with self.assertRaises(Conflict):
            self.resolve(version='v2')
        self.registry.approve(self.owner, 'bellavita', 'sample', 'v2', self.sha)
        self.assertEqual(self.resolve(version='v2')['content'], self.content)

    def test_missing_version_and_wrong_hash_never_fall_back_to_another_asset(self):
        self.approved()
        with self.assertRaises(NotFound):
            self.resolve(version='v999')
        with self.assertRaises(Conflict):
            self.resolve(expected_sha256='a'*64)
        with self.assertRaises(NotFound):
            self.resolve(asset_id='unregistered')

    def test_project_scope_and_worker_write_permissions_are_checked(self):
        self.approved()
        with self.assertRaises(PermissionDenied):
            self.draft(token=self.worker, version='v2')
        with self.assertRaises(NotFound):
            self.resolve(token=self.other)
        with self.assertRaises(NotFound):
            self.draft(token=self.other, version='v2')
        with self.assertRaises(PermissionDenied):
            self.registry.approve(self.other, 'bellavita', 'sample', 'v1', self.sha)

    def test_revoked_and_expired_credentials_cannot_read_or_approve(self):
        self.approved()
        expiring = self.store.add_token('expiring', 'worker', ['bellavita'], expires_at=1001)
        self.now = 1002
        with self.assertRaises(PermissionDenied):
            self.resolve(token=expiring)
        self.store.revoke_token(self.owner, token_hash(self.worker))
        with self.assertRaises(PermissionDenied):
            self.resolve()

    def test_missing_cache_file_is_an_error_without_substitution(self):
        self.approved()
        (self.cache / self.sha).unlink()
        with self.assertRaises(AssetUnavailable):
            self.resolve()
        with self.assertRaises(AssetUnavailable):
            self.draft()  # An existing immutable record is not silently repaired.

    def test_tampered_content_blocks_resolve_and_approval(self):
        self.draft()
        (self.cache / self.sha).write_bytes(b'x'*len(self.content))
        with self.assertRaises(AssetUnavailable):
            self.registry.approve(self.owner, 'bellavita', 'sample', 'v1', self.sha)
        self.assertEqual(self.store.authenticate(self.manager)['role'], 'manager')
        # The failed approval did not promote the draft.
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT status FROM bridge_assets').fetchone()[0], 'draft')

    def test_cache_file_symlink_cannot_be_read_even_when_target_has_matching_bytes(self):
        self.approved()
        original = self.base / 'fixture-original'
        original.write_bytes(self.content)
        original.chmod(0o600)
        (self.cache / self.sha).unlink()
        (self.cache / self.sha).symlink_to(original)
        with self.assertRaises(AssetUnavailable):
            self.resolve()

    def test_cache_hardlink_and_broad_file_permissions_are_rejected(self):
        self.approved()
        cached = self.cache / self.sha
        cached.chmod(0o644)
        with self.assertRaises(AssetUnavailable):
            self.resolve()
        cached.chmod(0o600)
        os.link(cached, self.base / 'another-link')
        with self.assertRaises(AssetUnavailable):
            self.resolve()

    def test_private_modes_and_root_symlink_protection(self):
        self.draft()
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.cache / self.sha).stat().st_mode & 0o777, 0o600)
        link = self.base / 'cache-link'
        link.symlink_to(self.cache, target_is_directory=True)
        with self.assertRaises(AssetUnavailable):
            AssetRegistry(self.store, link)
        self.cache.chmod(0o755)
        with self.assertRaises(AssetUnavailable):
            self.draft(version='v2')

    def test_ancestor_replaced_with_symlink_after_registration_is_rejected(self):
        holder = self.base / 'holder'
        holder.mkdir(mode=0o700)
        registry = AssetRegistry(self.store, holder / 'cache')
        registry.register_draft(self.manager, 'bellavita', 'nested', 'v1', self.content)
        registry.approve(self.owner, 'bellavita', 'nested', 'v1', self.sha)
        original = self.base / 'original-holder'
        holder.rename(original)
        holder.symlink_to(original, target_is_directory=True)
        with self.assertRaises(AssetUnavailable):
            registry.resolve(self.worker, 'bellavita', 'nested', 'v1', self.sha)

    def test_bounded_bytes_and_identifier_validation_no_host_path_read_api(self):
        for content in (b'', b'x'*(MAX_ASSET_BYTES+1), 'file:///private/original.png', self.cache):
            with self.assertRaises(ValueError):
                self.draft(content=content)
        for name in ('../image', '/tmp/image', 'a/b', '..', ''):
            with self.assertRaises(ValueError):
                self.draft(asset_id=name)
        with self.assertRaises(ValueError):
            self.draft(kind='executable-program')
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_credential_bytes_are_rejected_and_not_persisted(self):
        with self.assertRaises(ValueError):
            self.draft(content=self.manager.encode())
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertNotIn(self.manager.encode(), (self.base / 'state.sqlite3').read_bytes())

    def test_restart_preserves_approval_and_exact_content(self):
        self.approved()
        restarted = AssetRegistry(Store(self.base / 'state.sqlite3', clock=lambda: self.now), self.cache)
        result = restarted.resolve(self.worker, 'bellavita', 'sample', 'v1', self.sha)
        self.assertEqual(result['status'], 'approved')
        self.assertEqual(result['content'], self.content)
        self.assertEqual(restarted.approve(self.owner, 'bellavita', 'sample', 'v1', self.sha)['approved_at'], 1000)


if __name__ == '__main__':
    unittest.main()
