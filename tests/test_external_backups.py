import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch,MagicMock
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import external_backups as external
import manage_data
import checkout
import shop
import shop_readiness

KEY='ab'*32


class ExternalBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);data=self.root/'data';data.mkdir()
        with sqlite3.connect(data/'state.sqlite3') as c:c.execute('CREATE TABLE shop_products(id TEXT PRIMARY KEY,details TEXT,revision INTEGER)');c.execute("INSERT INTO shop_products VALUES ('sample','private test data',1)")
        self.snapshot=self.root/'snapshot';manage_data.transfer(data,self.snapshot)
    def tearDown(self):self.tmp.cleanup()

    def test_encrypted_offsite_roundtrip_and_wrong_key_tamper_rejection(self):
        blob=external.pack(self.snapshot,KEY);self.assertNotIn(b'private test data',blob)
        report=external.restore(blob,self.root/'restored',KEY);self.assertEqual(report['counts']['shop_products'],1)
        with self.assertRaises(ValueError):external.restore(blob,self.root/'restored',KEY)
        with self.assertRaises(ValueError):external.restore(blob,self.root/'wrong','cd'*32)
        with self.assertRaises(ValueError):external.restore(blob[:-1]+bytes([blob[-1]^1]),self.root/'changed',KEY)
        with self.assertRaises(ValueError):external.pack(self.snapshot,'short')
        (self.snapshot/'extra.txt').write_text('changed')
        with self.assertRaises(ValueError):external.pack(self.snapshot,KEY)

    def test_encrypted_archive_cannot_escape_restore_directory(self):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as archive:
            item=tarfile.TarInfo('../escape');item.size=4;archive.addfile(item,io.BytesIO(b'evil'))
        nonce=b'0'*12;blob=external.MAGIC+nonce+AESGCM(bytes.fromhex(KEY)).encrypt(nonce,stream.getvalue(),external.MAGIC)
        with self.assertRaises(ValueError):external.restore(blob,self.root/'unsafe',KEY)
        self.assertFalse((self.root/'escape').exists());self.assertFalse((self.root/'unsafe').exists())

    def test_drive_transfer_re_download_and_decrypt_drill(self):
        cred=self.root/'service.json';cred.write_text('{}')
        with patch.dict(os.environ,{'SHOP_BACKUP_ENCRYPTION_KEY':KEY,'SHOP_EXTERNAL_BACKUP_ENABLED':'1','SHOP_BACKUP_DRIVE_FOLDER':'folder-test-123','SHOP_BACKUP_DRIVE_CREDENTIALS':str(cred)}),patch('google.oauth2.service_account.Credentials.from_service_account_file'),patch('google.auth.transport.requests.AuthorizedSession') as session:
            instance=session.return_value;instance.post.return_value.json.return_value={'id':'drive-test-file-123'}
            def returned(*args,**kwargs):
                request_body=instance.post.call_args.kwargs['data'];start=request_body.index(external.MAGIC);end=request_body.rfind(b'\r\n--');response=MagicMock();response.content=request_body[start:end];return response
            instance.get.side_effect=returned
            result=external.upload(self.snapshot);self.assertEqual(result['state'],'verified');self.assertEqual(result['restore_drill'],'passed')
            instance.post.assert_called_once();instance.get.assert_called_once()

    def test_readiness_distinguishes_mock_merchant_and_tax_missing_without_secrets(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;checkout.setup(c);shop.setup(c)
        p={'title':'검증 상품','public':True,'cash_tax_type':''};c.execute('INSERT INTO shop_products VALUES (?,?,1)',('a'*32,json.dumps(p)))
        with patch.dict(os.environ,{**{'INVITATION_PAYAPP_'+k:'isolated-verification-'+k for k in checkout.PAYAPP_FIELDS},'SOLAPI_API_KEY':'private-key-must-not-leak','SHOP_MESSAGES_ENABLED':'0','SHOP_ALIMTALK_ENABLED':'0','SHOP_CASH_RECEIPT_ENABLED':'0'}):
            r=shop_readiness.report(c);self.assertFalse(r['payapp']['merchant_keys_present']);self.assertTrue(r['payapp']['mock_adapter']);self.assertEqual(r['cash_receipt']['products_missing_tax_type'],['검증 상품']);self.assertNotIn('private-key-must-not-leak',json.dumps(r));self.assertFalse(r['alimtalk']['enabled'])
        external.record(c,{'file_id':'file-test-12345','sha256':'a'*64});external.record(c,{'file_id':'file-test-12345','sha256':'a'*64});self.assertEqual(c.execute('SELECT count(*) FROM shop_external_backups').fetchone()[0],1)
        with self.assertRaises(ValueError):external.record(c,{'file_id':'file-test-12345','sha256':'b'*64})
        c.close()
