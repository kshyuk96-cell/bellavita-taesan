import copy
import hashlib
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_worker_quality_handoff as quality_fixtures
from manager_bridge.reference_bundle import ReferenceBundles
from manager_bridge.store import Conflict,PermissionDenied,token_hash


class ReferenceBundleTests(unittest.TestCase):
    def setUp(self):
        self.fx=quality_fixtures.WorkerQualityHandoffTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.root=self.fx.fx.root/'reference-preparation';self.root.mkdir(mode=0o700)
        self.bundles=ReferenceBundles(self.fx.core,self.fx.artifacts,self.root,handoff=self.fx.handoff)
        self.task=self.fx.task()

    def prepare(self):return self.bundles.prepare(self.fx.manager,self.task['id'],'developer')

    def assert_clean(self):self.assertEqual(list(self.root.iterdir()),[])

    def test_exact_local_bytes_are_prepared_private_readonly_but_not_delivered_or_viewed(self):
        with self.prepare() as bundle:
            self.assertTrue(bundle.prepared);self.assertFalse(bundle.delivered);self.assertFalse(bundle.viewed)
            self.assertFalse(bundle.execution_authorized);self.assertFalse(bundle.os_sandbox_enforced);self.assertTrue(bundle.owner_process_can_modify)
            self.assertEqual(stat.S_IMODE(bundle.directory.stat().st_mode),0o500)
            self.assertEqual(len(bundle.files),1)
            ref=bundle.files[0];path=bundle.directory/ref.filename
            self.assertRegex(ref.filename,r'^reference-00-[a-f0-9]{64}\.data$')
            self.assertFalse(path.is_symlink());self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o400)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),ref.sha256)
            self.assertEqual(path.stat().st_size,ref.size_bytes)
            self.assertFalse(bundle.handoff['reference_materialized']);self.assertTrue(bundle.handoff['reference_delivery_pending'])
            self.assertEqual(bundle.binding_digest,bundle.handoff['binding_digest'])
            location=bundle.directory
        self.assertFalse(location.exists());self.assert_clean()

    def test_body_read_failure_and_bad_hash_clean_partial_directory(self):
        with patch.object(self.fx.artifacts,'read_body',side_effect=OSError('synthetic body read failure')):
            with self.assertRaises(OSError):
                with self.prepare():self.fail('must not yield failed read')
        self.assert_clean()
        with patch.object(self.fx.artifacts,'read_body',return_value=b'wrong approved body'):
            with self.assertRaises(Conflict):
                with self.prepare():self.fail('must not yield different hash')
        self.assert_clean()

    def test_reviewer_preparation_requires_exact_frozen_candidate(self):
        task,reviewed=self.fx.fx.reviewed(self.task)
        with self.assertRaises(Conflict):
            with self.bundles.prepare(self.fx.manager,task['id'],'reviewer','f'*64):
                self.fail('wrong candidate must not receive references')
        self.assert_clean()
        with self.bundles.prepare(self.fx.manager,task['id'],'reviewer',reviewed['candidate_digest']) as bundle:
            self.assertEqual(bundle.handoff['candidate_digest'],reviewed['candidate_digest'])
            self.assertFalse(bundle.delivered)
            self.assertEqual(len(bundle.files),1)
        self.assert_clean()

    def test_returned_context_exception_always_cleans_local_files(self):
        with self.assertRaisesRegex(RuntimeError,'caller failure'):
            with self.prepare() as bundle:
                location=bundle.directory;raise RuntimeError('caller failure')
        self.assertFalse(location.exists());self.assert_clean()

    def test_mid_read_permission_revocation_or_retention_change_rejects_before_yield(self):
        original=self.fx.artifacts.read_body
        def expired(token,reference):
            data=original(token,reference)
            with self.fx.store._transaction() as c:c.execute('UPDATE artifact_content_records SET expires=999 WHERE reference=?',(reference,))
            return data
        with patch.object(self.fx.artifacts,'read_body',side_effect=expired):
            with self.assertRaises(Conflict):
                with self.prepare():self.fail('expired handoff must not yield')
        self.assert_clean()

    def test_mid_read_reauth_rejects_revoked_caller_and_cleans(self):
        original=self.fx.artifacts.read_body
        def revoked(token,reference):
            data=original(token,reference)
            self.fx.store.revoke_token(self.fx.owner,token_hash(self.fx.manager));return data
        with patch.object(self.fx.artifacts,'read_body',side_effect=revoked):
            with self.assertRaises(PermissionDenied):
                with self.prepare():self.fail('revoked caller must not yield')
        self.assert_clean()

    def test_duplicate_too_many_and_aggregate_byte_bounds_reject_before_body_read(self):
        packet=self.fx.handoff.generate(self.fx.manager,self.task['id'],'developer')
        for count in (2,11):
            bad=copy.deepcopy(packet);bad['references']=bad['references']*count
            with patch.object(self.fx.handoff,'generate',return_value=bad),patch.object(self.fx.artifacts,'read_body',side_effect=AssertionError('body read forbidden')):
                with self.assertRaises(Conflict):
                    with self.prepare():self.fail('invalid metadata must not yield')
            self.assert_clean()
        small=ReferenceBundles(self.fx.core,self.fx.artifacts,self.root,handoff=self.fx.handoff,max_bytes=1)
        with self.assertRaises(Conflict):
            with small.prepare(self.fx.manager,self.task['id'],'developer'):self.fail('byte bound must not yield')
        self.assert_clean()

    def test_handoff_change_after_body_read_cleans_before_exposing_bundle(self):
        original=self.fx.handoff.generate;calls=[]
        def changed(*args,**kwargs):
            packet=original(*args,**kwargs);calls.append(1)
            if len(calls)>1:packet['binding_digest']='f'*64
            return packet
        with patch.object(self.fx.handoff,'generate',side_effect=changed):
            with self.assertRaises(Conflict):
                with self.prepare():self.fail('changed binding must not yield')
        self.assert_clean()

    def test_private_root_identity_and_artifact_overlap_and_symlink_are_rejected(self):
        with self.assertRaises(ValueError):ReferenceBundles(self.fx.core,self.fx.artifacts,self.fx.artifacts.root)
        alias=self.fx.fx.root/'prep-link';alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):ReferenceBundles(self.fx.core,self.fx.artifacts,alias)
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            with self.prepare():self.fail('public root must not yield')
        self.root.chmod(0o700)
        self.assert_clean()

    def test_cleanup_failure_still_closes_both_directory_descriptors(self):
        closed=[];actual_close=os.close;actual_clear=__import__('manager_bridge.reference_bundle',fromlist=['_clear'])._clear
        def close(fd):closed.append(fd);return actual_close(fd)
        def failed_clear(fd):actual_clear(fd);raise RuntimeError('synthetic cleanup failure')
        with patch('manager_bridge.reference_bundle._clear',side_effect=failed_clear),patch('manager_bridge.reference_bundle.os.close',side_effect=close):
            with self.assertRaisesRegex(RuntimeError,'cleanup failure'):
                with self.prepare():pass
        self.assertGreaterEqual(len(set(closed)),3)  # extra identity fd plus bundle/root
        self.assert_clean()
