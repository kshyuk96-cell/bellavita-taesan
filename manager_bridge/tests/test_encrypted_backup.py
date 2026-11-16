"""Private local AEAD fixtures, never remote services or persistent keys."""
import hashlib,json,os,sqlite3,unittest
from unittest.mock import patch
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.encrypted_backup import EncryptedBackup,PrivateLocalBackupBackend,BackupBackend
from manager_bridge.governance_durability import GovernanceDurability
from manager_bridge.store import Conflict,PermissionDenied,token_hash

class EncryptedBackupTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    def service(self,provider=None,backend=None):
        for label in ('staging','objects'):(self.root/label).mkdir(mode=0o700,exist_ok=True)
        self.key=os.urandom(32)
        return EncryptedBackup(GovernanceDurability(self.core),backend or PrivateLocalBackupBackend(self.root/'objects'),provider or (lambda keyid:self.key),self.root/'staging')
    def test_local_ciphertext_roundtrip_restores_quarantined_new_database(self):
        service=self.service();result=service.backup(self.owner,'fixture-backup','fixture-key')
        raw=(self.root/'objects/fixture-backup.aead').read_bytes()
        self.assertNotIn(b'SQLite format',raw);self.assertNotIn(self.owner.encode(),raw);self.assertNotIn(self.key,raw)
        self.assertFalse(result['offsite_verified'])
        restored=service.restore_rehearsal(self.owner,'fixture-backup',self.root/'restored.sqlite3')
        self.assertTrue(restored['reconciliation_required']);self.assertFalse(restored['automatic_replay'])
        with sqlite3.connect(self.root/'restored.sqlite3') as c:self.assertEqual(c.execute('SELECT reconciliation_required FROM governance_restore_marker').fetchone()[0],1)
        self.assertEqual(list((self.root/'staging').iterdir()),[])
        self.assertEqual((self.root/'objects/fixture-backup.aead').stat().st_mode&0o777,0o600)
    def test_owner_scope_and_no_overwrite(self):
        service=self.service()
        with self.assertRaises(PermissionDenied):service.backup(self.manager,'denied','key')
        limited=self.store.add_token('limited','owner',['project'])
        with self.assertRaises(PermissionDenied):service.backup(limited,'denied','key')
        service.backup(self.owner,'once','key')
        with self.assertRaises(Conflict):service.backup(self.owner,'once','key')
        target=self.root/'existing';target.write_text('preserve')
        with self.assertRaises(ValueError):service.restore_rehearsal(self.owner,'once',target)
        self.assertEqual(target.read_text(),'preserve')
    def test_ciphertext_digest_and_aead_both_reject_tampering(self):
        service=self.service();service.backup(self.owner,'tamper','key');path=self.root/'objects/tamper.aead'
        raw=bytearray(path.read_bytes());raw[-1]^=1;path.write_bytes(raw)
        with self.assertRaises(Conflict):service.restore_rehearsal(self.owner,'tamper',self.root/'restored')
        with self.store._transaction() as c:c.execute('UPDATE encrypted_backups SET ciphertext_digest=? WHERE backup_id=?',(hashlib.sha256(raw).hexdigest(),'tamper'))
        with self.assertRaisesRegex(Conflict,'authentication failed'):service.restore_rehearsal(self.owner,'tamper',self.root/'restored')
        self.assertFalse((self.root/'restored').exists())
    def test_wrong_or_unavailable_key_never_leaks_provider_exception(self):
        service=self.service();service.backup(self.owner,'keycheck','key');self.key=os.urandom(32)
        with self.assertRaisesRegex(Conflict,'authentication failed'):service.restore_rehearsal(self.owner,'keycheck',self.root/'restored')
        def missing(keyid):raise RuntimeError('private-key-sentinel')
        service.secret_provider=missing
        with self.assertRaises(Conflict) as error:service.backup(self.owner,'missing','key')
        self.assertNotIn('sentinel',str(error.exception));self.assertEqual(list((self.root/'staging').iterdir()),[])
    def test_transport_uncertainty_not_retried_and_revocation_after_upload(self):
        service=self.service();actual=service.backend
        class Backend(BackupBackend):
            def put_if_absent(inner,bid,payload):
                actual.put_if_absent(bid,payload)
                self.store.revoke_token(self.owner,token_hash(self.owner))
                return True
            def get(inner,bid,max_bytes):return actual.get(bid,max_bytes)
        service.backend=Backend()
        with self.assertRaises(PermissionDenied):service.backup(self.owner,'uncertain','key')
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT state FROM encrypted_backups').fetchone()[0],'uncertain')
        self.assertTrue((self.root/'objects/uncertain.aead').exists())
    def test_trusted_provider_bounds_and_fresh_nonces(self):
        service=self.service();service.backup(self.owner,'one','key');service.backup(self.owner,'two','key')
        one=(self.root/'objects/one.aead').read_bytes();two=(self.root/'objects/two.aead').read_bytes();self.assertNotEqual(one,two)
        service.secret_provider=lambda keyid:b'short'
        with self.assertRaises(Conflict):service.backup(self.owner,'bad-key','key')
    def test_secret_derived_labels_blocked_before_journal_or_transport(self):
        import base64
        key=b'A'*32
        service=self.service(provider=lambda keyid:key)
        variants=[key.decode(),key.hex(),key.hex().upper(),base64.b64encode(key).decode(),base64.b64encode(key).decode().rstrip('='),base64.urlsafe_b64encode(key).decode().rstrip('=')]
        for label in variants:
            # Some encoded forms may also be rejected by the identifier grammar.
            with self.assertRaises((Conflict,ValueError)) as error:service.backup(self.owner,'safe-backup',label)
            self.assertNotIn(label,str(error.exception))
            with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM encrypted_backups').fetchone()[0],0)
            self.assertEqual(list((self.root/'objects').iterdir()),[])
        with self.assertRaises(Conflict):service.backup(self.owner,key.decode(),'safe-key-id')
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM encrypted_backups').fetchone()[0],0)
    def test_label_guard_checks_base64url_and_not_ciphertext(self):
        import base64
        from manager_bridge.encrypted_backup import _guard_cleartext_labels
        key=bytes(range(224,256))
        for encoder in (base64.b64encode,base64.urlsafe_b64encode):
            for value in (encoder(key),encoder(key).rstrip(b'=')):
                with self.assertRaises(Conflict):_guard_cleartext_labels(b'{"key_id":"'+value+b'"}',key)
        _guard_cleartext_labels(b'{"key_id":"ordinary-nonsecret-id"}',key)
