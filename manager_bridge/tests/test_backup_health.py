"""Local encrypted backup monitoring and quarantine timing, no remote calls."""
import os
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.encrypted_backup import EncryptedBackup,PrivateLocalBackupBackend
from manager_bridge.governance_durability import GovernanceDurability
from manager_bridge.backup_health import BackupHealth,BackupHealthPolicy
from manager_bridge.store import Conflict,PermissionDenied,token_hash


class BackupHealthTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify

    def service(self):
        for p in ('objects','stage','bench'):(self.root/p).mkdir(mode=0o700)
        key=os.urandom(32)
        self.encrypted=EncryptedBackup(GovernanceDurability(self.core),PrivateLocalBackupBackend(self.root/'objects'),lambda keyid:key,self.root/'stage')
        health=BackupHealth(self.encrypted,rehearsal_root=self.root/'bench')
        health.install(self.owner,BackupHealthPolicy(10,1));return health

    def test_freshness_readback_and_retention_never_claim_data_loss_or_delete(self):
        health=self.service();self.encrypted.backup(self.owner,'one','key');self.now+=1;self.encrypted.backup(self.owner,'two','key')
        result=health.inspect(self.owner,verify_latest=True)
        self.assertEqual(result['state'],'observed_current');self.assertTrue(result['exact_readback_verified'])
        self.assertEqual(result['retention_candidates'],['one']);self.assertFalse(result['physical_deletion_performed'])
        self.assertIsNone(result['actual_customer_data_loss_seconds']);self.assertFalse(result['customer_rpo_verified'])
        self.now+=11;self.assertEqual(health.inspect(self.owner)['reason'],'backup_completion_stale')
        self.assertTrue((self.root/'objects/one.aead').exists())

    def test_owner_only_policy_cas_and_no_empty_backup_green(self):
        health=self.service()
        with self.assertRaises(PermissionDenied):health.inspect(self.manager)
        with self.assertRaises(Conflict):health.install(self.owner,BackupHealthPolicy())
        self.assertEqual(health.inspect(self.owner)['reason'],'no_stored_backup')

    def test_transport_failure_and_ciphertext_damage_require_attention(self):
        health=self.service();self.encrypted.backup(self.owner,'one','key')
        path=self.root/'objects/one.aead';path.write_bytes(b'damaged')
        result=health.inspect(self.owner,verify_latest=True)
        self.assertFalse(result['exact_readback_verified']);self.assertEqual(result['reason'],'backup_readback_unverified')
        actual=self.encrypted.backend.put_if_absent
        def failure(*args):raise RuntimeError('private-sentinel')
        self.encrypted.backend.put_if_absent=failure
        with self.assertRaises(Conflict):self.encrypted.backup(self.owner,'uncertain','key')
        self.assertEqual(health.inspect(self.owner)['reason'],'backup_uncertainty_requires_reconciliation')

    def test_rehearsal_measures_local_only_and_never_replays(self):
        health=self.service();self.encrypted.backup(self.owner,'one','key')
        result=health.rehearse(self.owner,'one','benchmark')
        self.assertTrue(result['quarantined']);self.assertGreaterEqual(result['elapsed_seconds'],0)
        self.assertFalse(result['production_rto_verified']);self.assertFalse(result['customer_rpo_verified'])
        self.assertEqual(list((self.root/'bench').iterdir()),[])
        with self.assertRaises(Conflict):health.rehearse(self.owner,'one','benchmark')

    def test_readback_callback_revocation_rechecked(self):
        health=self.service();self.encrypted.backup(self.owner,'one','key')
        actual=self.encrypted.backend.get
        def revoke(*args):
            value=actual(*args);self.store.revoke_token(self.owner,token_hash(self.owner));return value
        self.encrypted.backend.get=revoke
        with self.assertRaises(PermissionDenied):health.inspect(self.owner,verify_latest=True)

    def test_readback_metadata_race_rejected(self):
        health=self.service();self.encrypted.backup(self.owner,'one','key')
        actual=self.encrypted.backend.get
        def changed(*args):
            value=actual(*args)
            with self.store._transaction() as c:c.execute("UPDATE encrypted_backups SET bytes=bytes+1 WHERE backup_id='one'")
            return value
        self.encrypted.backend.get=changed
        with self.assertRaises(Conflict):health.inspect(self.owner,verify_latest=True)

    def test_failed_rehearsal_is_visible_and_not_replayed(self):
        health=self.service();self.encrypted.backup(self.owner,'one','key')
        (self.root/'objects/one.aead').write_bytes(b'damaged')
        with self.assertRaises(Conflict):health.rehearse(self.owner,'one','failed')
        self.assertEqual(health.inspect(self.owner)['reason'],'restore_rehearsal_unresolved')
        with self.assertRaises(Conflict):health.rehearse(self.owner,'one','failed')
