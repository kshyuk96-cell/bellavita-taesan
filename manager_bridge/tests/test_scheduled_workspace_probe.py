import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from manager_bridge.scheduled_workspace_probe import prepare, respond, verify


class ScheduledWorkspaceProbeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root.chmod(0o700)
        self.pin = prepare(self.root)['challenge_sha256']

    def test_roundtrip_is_only_continuity(self):
        respond(self.root, expected_hash=self.pin)
        result = verify(self.root, expected_hash=self.pin)
        self.assertTrue(result['workspace_read_write_verified'])
        self.assertFalse(any(v for k,v in result.items() if k.endswith('_verified') and k != 'workspace_read_write_verified'))
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(0o600, (self.root/'receipt.json').stat().st_mode & 0o777)

    def test_duplicates_preserve_original(self):
        respond(self.root, expected_hash=self.pin)
        before = (self.root/'receipt.json').read_bytes()
        with self.assertRaises(FileExistsError): respond(self.root, expected_hash=self.pin)
        with self.assertRaises(FileExistsError): prepare(self.root)
        self.assertEqual(before, (self.root/'receipt.json').read_bytes())

    def test_missing_never_substituted(self):
        (self.root/'challenge.json').unlink()
        with self.assertRaises(FileNotFoundError): respond(self.root, expected_hash=self.pin)
        self.assertFalse((self.root/'receipt.json').exists())

    def test_replaced_challenge(self):
        p=self.root/'challenge.json';p.write_text('{}')
        with self.assertRaises(ValueError):respond(self.root, expected_hash=self.pin)

    def test_expired_and_backward_clock(self):
        for now in (1, 10**12):
            with patch('manager_bridge.scheduled_workspace_probe.time.time', return_value=now):
                with self.assertRaises(ValueError): respond(self.root, expected_hash=self.pin)

    def test_nonfinite_clock(self):
        for now in (float('nan'),float('inf'),True):
            with patch('manager_bridge.scheduled_workspace_probe.time.time', return_value=now):
                with self.assertRaises(ValueError): respond(self.root, expected_hash=self.pin)

    def test_symlink_and_hardlink(self):
        p=self.root/'challenge.json';other=self.root/'other';p.rename(other);p.symlink_to(other)
        with self.assertRaises(OSError):respond(self.root,expected_hash=self.pin)
        p.unlink();os.link(other,p)
        with self.assertRaises(ValueError):respond(self.root,expected_hash=self.pin)

    def test_public_file_or_root(self):
        (self.root/'challenge.json').chmod(0o644)
        with self.assertRaises(ValueError):respond(self.root,expected_hash=self.pin)
        (self.root/'challenge.json').chmod(0o600);self.root.chmod(0o755)
        with self.assertRaises(ValueError):respond(self.root,expected_hash=self.pin)

    def test_tampered_receipt(self):
        respond(self.root,expected_hash=self.pin)
        p=self.root/'receipt.json';v=json.loads(p.read_text());v['pid']+=1;p.write_text(json.dumps(v))
        with self.assertRaises(ValueError):verify(self.root,expected_hash=self.pin)

    def test_oversized_file(self):
        (self.root/'challenge.json').write_bytes(b'x'*4097)
        with self.assertRaises(ValueError):respond(self.root,expected_hash=self.pin)

    def test_lifetime_bound(self):
        for n in (0,3601,True):
            with self.assertRaises(ValueError):prepare(self.root,lifetime_seconds=n)
