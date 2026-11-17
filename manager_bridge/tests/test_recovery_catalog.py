"""Independent local recovery proofs with synthetic manager data only."""
from dataclasses import replace
import json,os,sqlite3,unittest
from manager_bridge.tests import test_encrypted_backup as fixtures
from manager_bridge.recovery_catalog import RecoveryCatalog,RecoveryTrust,RecoveryGrant
from manager_bridge.encrypted_backup import PrivateLocalBackupBackend
from manager_bridge.store import Conflict,PermissionDenied,Store
from manager_bridge.maintenance_registry import Registry
from manager_bridge.three_tier import ThreeTier
from manager_bridge.operating_budget import OperatingBudget,BudgetPolicy

class RecoveryCatalogTests(unittest.TestCase):
    setUp=fixtures.EncryptedBackupTests.setUp
    observe=fixtures.EncryptedBackupTests.observe
    verify=fixtures.EncryptedBackupTests.verify
    service=fixtures.EncryptedBackupTests.service
    def setup_catalog(self):
        service=self.service();self.trustkey=os.urandom(32);self.owner_current=True
        for item in ('catalog','recovery-staging'):(self.root/item).mkdir(mode=0o700)
        trust=RecoveryTrust('fixture-recovery',lambda:self.trustkey,lambda assertion,scope:'owner-subject' if assertion=='explicit-owner-proof' else None,lambda subject,scope:self.owner_current and subject=='owner-subject',clock=lambda:self.now)
        catalog=RecoveryCatalog(trust,PrivateLocalBackupBackend(self.root/'catalog'),service.backend,lambda keyid:self.key,self.root/'recovery-staging')
        service.backup(self.owner,'independent','backup-key');catalog.publish(self.owner,service,'independent')
        return service,catalog
    def grant(self,catalog,target=None):return catalog.trust.issue_grant('explicit-owner-proof','independent',target or self.root/'recovered.sqlite3')
    def test_original_database_absent_actual_recovery_quarantined_and_budget_denied(self):
        service,catalog=self.setup_catalog();budget=OperatingBudget(self.core);budget.install(self.owner,'project',BudgetPolicy(1,2,60))
        # Snapshot contains an installed policy for a restored gate check.
        service.backup(self.owner,'with-budget','backup-key');catalog.publish(self.owner,service,'with-budget')
        grant=catalog.trust.issue_grant('explicit-owner-proof','with-budget',self.root/'recovered.sqlite3')
        self.store.path.unlink()
        result=catalog.restore(grant);self.assertFalse(result['original_manager_database_required']);self.assertFalse(result['offsite_verified'])
        restored=Store(self.root/'recovered.sqlite3',clock=lambda:self.now);core=ThreeTier(restored,Registry(restored));gate=OperatingBudget(core)
        self.assertFalse(gate.check(self.owner,'project')['allowed'])
        with sqlite3.connect(self.root/'recovered.sqlite3') as c:self.assertEqual(c.execute('SELECT reconciliation_required FROM governance_restore_marker').fetchone()[0],1)
        self.assertEqual(list((self.root/'recovery-staging').iterdir()),[])
    def test_expired_future_unsigned_wrong_scope_and_destination_grants_denied(self):
        service,catalog=self.setup_catalog();grant=self.grant(catalog)
        bad=[replace(grant,signature='0'*64),replace(grant,scope='other'),replace(grant,destination=str(self.root/'other')),replace(grant,issued_at=self.now+1)]
        for item in bad:
            with self.assertRaises(PermissionDenied):catalog.restore(item)
        self.now+=301
        with self.assertRaises(PermissionDenied):catalog.restore(grant)
        with self.assertRaises(PermissionDenied):catalog.restore({'scope':'fixture-recovery'})
    def test_unsigned_catalog_or_tampered_object_rejected_without_restore(self):
        service,catalog=self.setup_catalog();grant=self.grant(catalog)
        path=self.root/'catalog/receipt-independent.aead';record=json.loads(path.read_bytes());record['receipt']['bytes']+=1;path.write_text(json.dumps(record))
        with self.assertRaises(Conflict):catalog.restore(grant)
        self.assertFalse((self.root/'recovered.sqlite3').exists())
    def test_replay_and_revoked_owner_grants_denied(self):
        service,catalog=self.setup_catalog();grant=self.grant(catalog);catalog.restore(grant)
        (self.root/'recovered.sqlite3').unlink()
        with self.assertRaises(Conflict):catalog.restore(grant)
        another=self.grant(catalog,self.root/'another');self.owner_current=False
        with self.assertRaises(PermissionDenied):catalog.restore(another)
    def test_keys_must_be_separate_and_secret_labels_not_cataloged(self):
        service,catalog=self.setup_catalog();self.trustkey=self.key
        with self.assertRaises(Conflict):catalog.publish(self.owner,service,'independent')
    def test_provider_callback_revocation_blocks_before_restore_write(self):
        service,catalog=self.setup_catalog();grant=self.grant(catalog);old=catalog.objects.get
        def revoke(bid,limit):
            value=old(bid,limit);self.owner_current=False;return value
        catalog.objects.get=revoke
        with self.assertRaises(PermissionDenied):catalog.restore(grant)
        self.assertFalse((self.root/'recovered.sqlite3').exists())
    def test_trust_key_callback_revocation_cannot_pass_verification(self):
        service,catalog=self.setup_catalog();grant=self.grant(catalog)
        def revoked_key():
            self.owner_current=False;return self.trustkey
        catalog.trust.provider=revoked_key
        with self.assertRaises(PermissionDenied):catalog.restore(grant)
        self.assertFalse((self.root/'recovered.sqlite3').exists())

    def test_grant_uses_one_issuance_instant_with_advancing_clock(self):
        service,catalog=self.setup_catalog()
        import itertools
        ticks=itertools.count(1000,0.01)
        catalog.trust.clock=lambda:next(ticks)
        grant=self.grant(catalog)
        self.assertEqual(300,grant.expires_at-grant.issued_at)
        self.assertEqual(1000,grant.issued_at)

    def test_invalid_issuance_clock_denied(self):
        service,catalog=self.setup_catalog()
        for value in (float('nan'),float('inf'),True,0):
            catalog.trust.clock=lambda:value
            with self.assertRaises(PermissionDenied):self.grant(catalog)
