import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_reference_bundle as bundle_fixtures
from manager_bridge.reference_delivery import ReferenceDelivery,ReferenceDeliveryContext
from manager_bridge.store import Conflict,PermissionDenied,token_hash


class ReferenceDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fx=bundle_fixtures.ReferenceBundleTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.token=self.fx.fx.manager
        self.delivery=ReferenceDelivery(self.fx.bundles,lambda:self.token)
        self.project='project';self.task=self.fx.task

    def packet(self,role='developer',candidate=None):return self.fx.fx.handoff.generate(self.token,self.task['id'],role,candidate)
    def prepare(self,role='developer',candidate=None,packet=None):return self.delivery.prepare(self.project,self.task['id'],role,candidate,packet or self.packet(role,candidate))
    def assert_clean(self):self.fx.assert_clean()

    def test_typed_developer_context_metadata_exact_paths_without_inline_bytes_or_claims(self):
        with self.prepare() as context:
            self.assertIs(type(context),ReferenceDeliveryContext);self.assertTrue(context.revalidate())
            metadata=context.metadata
            self.assertEqual(context.binding_digest,metadata['binding_digest']);self.assertEqual(context.mount_digest,metadata['mount_digest'])
            self.assertNotEqual(context.binding_digest,context.mount_digest)
            self.assertEqual(metadata['files'][0]['path'],str(context.directory/context.files[0].filename))
            self.assertNotIn('synthetic approved layout',json.dumps(metadata));self.assertNotIn(self.token,json.dumps(metadata))
            self.assertFalse(metadata['delivered']);self.assertFalse(metadata['viewed']);self.assertFalse(metadata['read_only_mount_verified']);self.assertFalse(metadata['execution_authorized'])
            metadata['files'].clear();self.assertEqual(len(context.metadata['files']),1)
            directory=context.directory
        self.assertFalse(directory.exists());self.assert_clean()
        with self.assertRaises(Conflict):context.revalidate()
        with self.assertRaises(Conflict):_ = context.metadata

    def test_reviewer_context_pins_actual_frozen_candidate_and_role(self):
        task,reviewed=self.fx.fx.fx.reviewed(self.task);self.task=task
        candidate=reviewed['candidate_digest']
        with self.prepare('reviewer',candidate) as context:
            self.assertEqual(context.metadata['candidate_digest'],candidate);self.assertEqual(context.metadata['role'],'reviewer')
            self.assertTrue(context.revalidate())
        self.assert_clean()

    def test_same_quality_preparations_have_different_physical_mount_identity(self):
        packet=self.packet()
        with self.prepare(packet=packet) as first:
            first_digest=first.mount_digest;quality_digest=first.binding_digest
        with self.prepare(packet=packet) as second:
            self.assertEqual(quality_digest,second.binding_digest);self.assertNotEqual(first_digest,second.mount_digest)
        self.assert_clean()

    def test_mutated_sha_identity_permissions_links_and_extra_file_fail_heartbeat_and_cleanup(self):
        for mutation in ('content','replacement','mode','symlink','extra'):
            with self.subTest(mutation=mutation):
                with self.assertRaises((Conflict,OSError)):
                    with self.prepare() as context:
                        path=context.directory/context.files[0].filename;context.directory.chmod(0o700)
                        if mutation=='content':path.chmod(0o600);path.write_bytes(b'changed');path.chmod(0o400)
                        elif mutation=='replacement':
                            data=path.read_bytes();path.unlink();path.write_bytes(data);path.chmod(0o400)
                        elif mutation=='mode':path.chmod(0o600)
                        elif mutation=='symlink':path.unlink();path.symlink_to(self.fx.fx.fx.root/'outside')
                        else:(context.directory/'unexpected').write_text('unexpected metadata')
                        context.directory.chmod(0o500);context.revalidate()
                self.assert_clean()

    def test_expiry_and_caller_revocation_block_current_context(self):
        with self.assertRaises(Conflict):
            with self.prepare() as context:
                with self.fx.fx.store._transaction() as c:c.execute("UPDATE artifact_content_records SET expires=999 WHERE project='project'")
                context.revalidate()
        self.assert_clean()

    def test_token_loader_identity_change_is_not_silently_reused(self):
        with self.assertRaises(Conflict):
            with self.prepare() as context:
                self.token=self.fx.fx.owner;context.revalidate()
        self.assert_clean()

    def test_wrong_project_role_packet_and_tampered_quality_rejected_before_yield(self):
        packet=self.packet();bad=copy.deepcopy(packet);bad['role']='reviewer'
        with self.assertRaises(Conflict):
            with self.prepare(packet=bad):self.fail('tampered packet cannot yield')
        with self.assertRaises(Conflict):
            with self.delivery.prepare('other',self.task['id'],'developer',None,packet):self.fail('wrong project cannot yield')
        self.assert_clean()

    def test_prepared_directory_alias_and_renamed_path_are_detected(self):
        with self.assertRaises(Conflict):
            with self.prepare() as context:
                previous=context.directory.with_name(context.directory.name+'-moved');context.directory.rename(previous)
                previous.rename(context.directory)  # Restore name before final cleanup.
                context.directory.chmod(0o700);context.revalidate()
        self.assert_clean()

    def test_authority_revoke_and_context_exception_always_cleanup(self):
        with self.assertRaises(PermissionDenied):
            with self.prepare() as context:
                self.fx.fx.store.revoke_token(self.fx.fx.owner,token_hash(self.token));context.revalidate()
        self.assert_clean()

    def test_wrong_candidate_packet_is_rejected_before_any_local_materialization(self):
        task,reviewed=self.fx.fx.fx.reviewed(self.task);self.task=task
        packet=self.packet('reviewer',reviewed['candidate_digest'])
        with self.assertRaises(Conflict):
            with self.delivery.prepare(self.project,self.task['id'],'reviewer','f'*64,packet):self.fail('wrong candidate must not yield')
        self.assert_clean()

    def test_root_or_file_permission_drift_cannot_change_mount_digest_in_place(self):
        with self.assertRaises(Conflict):
            with self.prepare() as context:
                pinned=context.mount_digest
                context.directory.chmod(0o700)
                self.assertEqual(context.mount_digest,pinned)
                context.revalidate()
        self.assert_clean()
