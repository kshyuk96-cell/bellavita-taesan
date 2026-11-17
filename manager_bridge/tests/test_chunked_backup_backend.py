import json
import tempfile
from pathlib import Path
import unittest
from manager_bridge.chunked_backup_backend import ChunkedBackupBackend
from manager_bridge.encrypted_backup import BackupBackend,PrivateLocalBackupBackend
from manager_bridge.store import Conflict


class MemoryBackend(BackupBackend):
    def __init__(self):self.values={};self.puts=0;self.gets=[];self.fail_at=None
    def put_if_absent(self,key,payload):
        self.puts+=1
        if key in self.values:raise Conflict('exists')
        self.values[key]=payload
        if self.puts==self.fail_at:raise Conflict('uncertain stored upload')
        return True
    def get(self,key,limit):
        self.gets.append(key);value=self.values[key]
        if len(value)>limit:raise Conflict('too large')
        return value


class ChunkedBackupTests(unittest.TestCase):
    def setUp(self):
        self.transport=MemoryBackend();self.backend=ChunkedBackupBackend(self.transport,chunk_bytes=4,max_parts=16,max_payload_bytes=64)
    def test_roundtrip_and_final_publication(self):
        self.backend.put_if_absent('sample',b'abcdefghijk')
        self.assertEqual(b'abcdefghijk',self.backend.get('sample',64))
        self.assertEqual('sample',list(self.transport.values)[-1])
        self.assertTrue(all(len(v)<=4 for k,v in self.transport.values.items() if k.startswith('part-')))
    def test_duplicate_stops_before_more_parts(self):
        self.backend.put_if_absent('sample',b'abcdef');before=dict(self.transport.values)
        with self.assertRaises(Conflict):self.backend.put_if_absent('sample',b'replace')
        self.assertEqual(before,self.transport.values)
    def test_uncertain_part_not_published_or_replayed(self):
        self.transport.fail_at=2
        with self.assertRaises(Conflict):self.backend.put_if_absent('sample',b'abcdef')
        self.assertNotIn('sample',self.transport.values)
        before=dict(self.transport.values)
        with self.assertRaises(Conflict):self.backend.put_if_absent('sample',b'abcdef')
        self.assertEqual(before,self.transport.values)
    def test_missing_and_corrupt_parts_denied(self):
        self.backend.put_if_absent('sample',b'abcdef');part=json.loads(self.transport.values['sample'])['parts'][0]['id']
        self.transport.values[part]=b'xxxx'
        with self.assertRaises(Conflict):self.backend.get('sample',64)
        del self.transport.values[part]
        with self.assertRaises(KeyError):self.backend.get('sample',64)
    def test_order_and_final_hash_checked(self):
        self.backend.put_if_absent('sample',b'abcdefgh');m=json.loads(self.transport.values['sample']);m['parts'].reverse();self.transport.values['sample']=json.dumps(m).encode()
        with self.assertRaises(Conflict):self.backend.get('sample',64)
    def test_size_bound_before_part_reads(self):
        self.backend.put_if_absent('sample',b'abcdefgh');self.transport.gets=[]
        with self.assertRaises(Conflict):self.backend.get('sample',3)
        self.assertEqual(['sample'],self.transport.gets)
    def test_duplicate_keys_and_parts_denied(self):
        self.transport.values['sample']=b'{"version":1,"version":1}'
        with self.assertRaises(Conflict):self.backend.get('sample',64)
        del self.transport.values['sample'];self.backend.put_if_absent('sample',b'abcdefgh');m=json.loads(self.transport.values['sample']);m['parts'][1]=m['parts'][0];self.transport.values['sample']=json.dumps(m).encode()
        with self.assertRaises(Conflict):self.backend.get('sample',64)
    def test_reserved_names_and_bad_bounds_no_upload(self):
        for key,payload in (('part-any',b'a'),('claim-any',b'a'),('sample',b''),('sample',b'a'*65)):
            with self.assertRaises(ValueError):self.backend.put_if_absent(key,payload)
        self.assertEqual({},self.transport.values)
        with self.assertRaises(ValueError):ChunkedBackupBackend(self.transport,chunk_bytes=True)
    def test_manifest_swap_after_read_denied(self):
        self.backend.put_if_absent('sample',b'abcdefgh');original=self.transport.get;reads=0
        def get(key,limit):
            nonlocal reads
            value=original(key,limit)
            if key=='sample':
                reads+=1
                if reads==2:return value+b' '
            return value
        self.transport.get=get
        with self.assertRaises(Conflict):self.backend.get('sample',64)
    def test_actual_private_file_roundtrip_above_one_mib(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);root.chmod(0o700)
            backend=ChunkedBackupBackend(PrivateLocalBackupBackend(root))
            payload=os.urandom(1100000)
            backend.put_if_absent('large',payload)
            self.assertEqual(payload,backend.get('large',len(payload)))
            self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in root.iterdir()))


class ChunkedEncryptedIntegrationTests(unittest.TestCase):
    from manager_bridge.tests import test_encrypted_backup as fixtures
    setUp=fixtures.EncryptedBackupTests.setUp
    observe=fixtures.EncryptedBackupTests.observe
    verify=fixtures.EncryptedBackupTests.verify
    service=fixtures.EncryptedBackupTests.service

    def test_actual_encrypted_database_restores_quarantined(self):
        root=self.root/'chunks';root.mkdir(mode=0o700)
        backend=ChunkedBackupBackend(PrivateLocalBackupBackend(root),chunk_bytes=16384,max_parts=256,max_payload_bytes=4*1024*1024)
        service=self.service(backend=backend)
        service.backup(self.owner,'chunk-fixture','fixture-key')
        result=service.restore_rehearsal(self.owner,'chunk-fixture',self.root/'restored.sqlite3')
        self.assertTrue(result['reconciliation_required']);self.assertFalse(result['automatic_replay'])
        manifest=json.loads((root/'chunk-fixture.aead').read_bytes())
        self.assertGreater(len(manifest['parts']),1)
