"""Private inert-byte retention, scoped recovery, and credential rejection."""
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.artifact_content import ArtifactContent
from manager_bridge.operations_reporting import ArtifactObservation,OperationsReporting
from manager_bridge.control_digest import ControlDigest
from manager_bridge.governance_portfolio import Portfolio
from manager_bridge.store import Conflict,PermissionDenied,NotFound,token_hash


class ArtifactContentTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify

    def content(self,data=b'Actual fixture result\x00\xff bytes',**kwargs):
        root=self.root/('artifacts-'+str(getattr(self,'roots',0)));self.roots=getattr(self,'roots',0)+1;root.mkdir(mode=0o700)
        provider=lambda project,key:ArtifactObservation(project,'qa_report',key,data,self.now)
        return ArtifactContent(self.core,root,sources={('project','qa_report'):provider},**kwargs)

    def test_actual_private_write_read_exact_bytes_and_metadata_receipt(self):
        data=b'Actual fixture result\x00\xff bytes';content=self.content(data)
        receipt=content.retain(self.manager,'project','qa_report','one')
        self.assertEqual(content.read_body(self.manager,receipt['reference']),data)
        self.assertEqual(receipt['sha256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(receipt['size_bytes'],len(data));self.assertFalse(receipt['acceptance_authorized'])
        self.assertFalse(receipt['http_body_download_enabled'])
        path=content.root/receipt['reference'];self.assertEqual(path.read_bytes(),data)
        self.assertEqual(path.stat().st_mode&0o777,0o600);self.assertEqual(path.stat().st_nlink,1)
        reporting=OperationsReporting(self.core,ControlDigest(Portfolio(self.core)),artifact_sources={('project','qa_report'):content.metadata_source(lambda:self.manager,'project','qa_report')})
        metadata=reporting.register_artifact(self.manager,'project','qa_report','one')
        self.assertEqual(metadata['sha256'],receipt['sha256']);self.assertEqual(metadata['retrieval'],'metadata_only')
        with self.store._transaction() as c:stored=[dict(row) for row in c.execute('SELECT * FROM artifact_content_records')]
        self.assertNotIn('Actual fixture result',json.dumps(stored));self.assertNotIn(str(content.root),json.dumps(receipt))

    def test_credential_key_rejected_before_observer_and_bytes_secrets_rejected(self):
        content=self.content();called=[]
        content.sources[('project','qa_report')]=lambda *args:called.append(args)
        with self.assertRaises(ValueError):content.retain(self.manager,'project','qa_report',self.dev)
        self.assertEqual(called,[])
        punctuation=self.store.add_token('punctuation-worker','worker',['project'],token='!'*40+'$%')
        for data in (b'\x00prefix'+self.dev.encode()+b'suffix\xff',b'\x00'+self.owner.encode()+b'\xff',b'prefix'+punctuation.encode()+b'suffix',
                     b'sk-proj-UNREGISTEREDSECRET12345',b'Authorization: Bearer secret-token-fixture',
                     b'{"refresh_token":"opaque-secret-123456"}',b'-----BEGIN OPENSSH PRIVATE KEY-----',
                     b'https://user:secretpassword@example.invalid'):
            source=self.content(data)
            with self.assertRaises(ValueError):source.retain(self.manager,'project','qa_report','secret')
            self.assertEqual(list(source.root.iterdir()),[])
        safe=self.content(b'file_sha256='+b'a'*64)
        safe.retain(self.manager,'project','qa_report','hash-is-not-secret')

    def test_scope_expiry_and_config_change_block_body_access(self):
        content=self.content();receipt=content.retain(self.manager,'project','qa_report','one',retention_seconds=60)
        limited=self.store.add_token('limited','manager',['other'])
        with self.assertRaises((NotFound,PermissionDenied)):content.read_body(limited,receipt['reference'])
        with self.assertRaises(PermissionDenied):content.read_body(self.dev,receipt['reference'])
        self.now+=61
        with self.assertRaises(Conflict):content.read_body(self.manager,receipt['reference'])
        self.assertEqual(content.metadata(self.manager,receipt['reference'])['state'],'expired')
        self.assertTrue((content.root/receipt['reference']).exists())
        self.registry.register(self.owner,'project',self.config,expected_revision=1)
        with self.assertRaises(Conflict):content.metadata(self.manager,receipt['reference'])

    def test_symlink_hardlink_permissions_and_tampering_rejected(self):
        content=self.content();receipt=content.retain(self.manager,'project','qa_report','one');path=content.root/receipt['reference']
        original=path.read_bytes();path.write_bytes(b'X'*len(original))
        with self.assertRaises(Conflict):content.read_body(self.manager,receipt['reference'])
        path.write_bytes(original);hardlink=self.root/'linked';os.link(path,hardlink)
        with self.assertRaises(ValueError):content.read_body(self.manager,receipt['reference'])
        hardlink.unlink();path.chmod(0o644)
        with self.assertRaises(ValueError):content.read_body(self.manager,receipt['reference'])
        path.chmod(0o600);path.unlink();path.symlink_to(self.store.path)
        with self.assertRaises((OSError,ValueError)):content.read_body(self.manager,receipt['reference'])
        with self.assertRaises(ValueError):ArtifactContent(self.core,path)
        with self.assertRaises(ValueError):content.read_body(self.manager,'../state.sqlite3')

    def test_observer_and_file_read_reauthenticate_before_publish_or_return(self):
        content=self.content()
        def observer(project,key):
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return ArtifactObservation(project,'qa_report',key,b'fixture',self.now)
        content.sources[('project','qa_report')]=observer
        with self.assertRaises(PermissionDenied):content.retain(self.manager,'project','qa_report','revoked')
        self.assertEqual(list(content.root.iterdir()),[])
        # Use the still-valid owner to create a separate result, then revoke caller during read.
        content=self.content();receipt=content.retain(self.owner,'project','qa_report','one')
        reader=self.store.add_token('body-reader','manager',['project'])
        original=content._file;revoked=[]
        def check(info):
            original(info)
            if not revoked:
                revoked.append(True)
                from manager_bridge.store import token_hash
                self.store.revoke_token(self.owner,token_hash(reader))
        with patch.object(content,'_file',side_effect=check):
            with self.assertRaises(PermissionDenied):content.read_body(reader,receipt['reference'])

    def test_body_is_inert_and_capacity_counts_unknown_files(self):
        marker=self.root/'never-created'
        data=('__import__("pathlib").Path('+repr(str(marker))+').write_text("ran")').encode()
        content=self.content(data);receipt=content.retain(self.manager,'project','qa_report','inert')
        self.assertEqual(content.read_body(self.manager,receipt['reference']),data);self.assertFalse(marker.exists())
        full=self.content(b'1',max_total_bytes=8*1024*1024)
        unknown=full.root/'unregistered';unknown.write_bytes(b'x'*(8*1024*1024));unknown.chmod(0o600)
        with self.assertRaises(Conflict):full.retain(self.manager,'project','qa_report','blocked')
        self.assertTrue(unknown.exists())

    def test_immutable_reference_and_missing_source(self):
        content=self.content();receipt=content.retain(self.manager,'project','qa_report','one')
        self.assertEqual(content.retain(self.manager,'project','qa_report','one'),receipt)
        content.sources[('project','qa_report')]=lambda project,key:ArtifactObservation(project,'qa_report',key,b'changed',self.now)
        with self.assertRaises(Conflict):content.retain(self.manager,'project','qa_report','one')
        with self.assertRaises(NotFound):content.retain(self.manager,'project','diagnostic','missing')


if __name__=='__main__':unittest.main()
