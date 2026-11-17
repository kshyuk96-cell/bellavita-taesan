import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from manager_bridge.private_state_inventory import inspect_private_state,public_summary,write_private_inventory


class PrivateStateInventoryTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.root.chmod(0o700)
        self.secret=self.root/'auth.json';self.secret.write_text('PRIVATE_SENTINEL');self.secret.chmod(0o600)
    def test_fixed_ten_categories_and_no_transfer_grant(self):
        v=inspect_private_state({'control_credentials':[self.secret]})
        self.assertEqual(10,v['category_count']);self.assertFalse(v['credentials_copied']);self.assertFalse(v['environment_replacement_authorized'])
    def test_metadata_read_does_not_open_secret(self):
        with patch.object(Path,'open',side_effect=AssertionError('contents opened')):
            v=inspect_private_state({'canonical_subscription':[self.secret]})
        self.assertEqual('metadata_available',next(c for c in v['categories'] if c['category']=='canonical_subscription')['state'])
    def test_missing_is_attention_not_substitution(self):
        p=self.root/'missing';v=inspect_private_state({'host_access_key':[p]})
        self.assertEqual('attention_required',next(c for c in v['categories'] if c['category']=='host_access_key')['state']);self.assertFalse(p.exists())
    def test_public_mode_secret_denied(self):
        self.secret.chmod(0o644);v=inspect_private_state({'control_credentials':[self.secret]})
        self.assertEqual('attention_required',next(c for c in v['categories'] if c['category']=='control_credentials')['state'])
    def test_symlink_and_hardlink_denied(self):
        link=self.root/'link';link.symlink_to(self.secret)
        hard=self.root/'hard';os.link(self.secret,hard)
        v=inspect_private_state({'control_credentials':[link,hard]})
        self.assertTrue(all(i['state']=='unavailable' for c in v['categories'] for i in c['items']))
    def test_relative_and_duplicate_entries_require_attention(self):
        v=inspect_private_state({'control_credentials':['relative',self.secret,self.secret]})
        items=next(c for c in v['categories'] if c['category']=='control_credentials')['items']
        self.assertEqual(['unavailable','metadata_available','unavailable'],[i['state'] for i in items])
    def test_summary_hides_paths_and_content(self):
        v=public_summary(inspect_private_state({'control_credentials':[self.secret]}));raw=json.dumps(v)
        self.assertNotIn(str(self.root),raw);self.assertNotIn('PRIVATE_SENTINEL',raw)
    def test_private_report_create_only(self):
        v=inspect_private_state({'control_credentials':[self.secret]});p=self.root/'inventory.json';write_private_inventory(v,p)
        self.assertEqual(0o600,p.stat().st_mode&0o777)
        with self.assertRaises(FileExistsError):write_private_inventory(v,p)
        self.assertNotIn('PRIVATE_SENTINEL',p.read_text())
    def test_no_directory_recursion_or_full_backup_claim(self):
        with patch.object(Path,'iterdir',side_effect=AssertionError('recursion')):
            v=inspect_private_state({'evidence_catalog':[self.root]})
        self.assertFalse(v['whole_workspace_inventory_verified']);self.assertFalse(v['complete_backup_verified'])
        self.assertIn('recovery_keys',v['unconfigured_categories'])
    def test_unknown_and_oversized_selections_denied(self):
        for value in ({'unknown':[self.secret]},{'control_credentials':[self.secret]*201}):
            with self.assertRaises(ValueError):inspect_private_state(value)
