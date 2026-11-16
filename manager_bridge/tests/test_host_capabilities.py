import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from manager_bridge.host_capabilities import inspect_host


class HostCapabilitiesTests(unittest.TestCase):
    def test_expected_target_does_not_attest_remote_host_or_storage(self):
        with patch.dict(os.environ,{},clear=True):
            value=inspect_host(expected_service_id='srv-candidate',engine=SimpleNamespace(probe=lambda:{'isolation_verified':True}))
        self.assertTrue(value['namespace_isolation_verified'])
        self.assertFalse(value['exact_target_host_verified']);self.assertFalse(value['remote_execution_performed'])
        self.assertFalse(value['storage']['persistent_across_restart_verified']);self.assertFalse(value['deployment_authorized'])

    def test_failed_isolation_has_no_plain_process_fallback(self):
        def unavailable():raise OSError('private probe details')
        value=inspect_host(engine=SimpleNamespace(probe=unavailable))
        self.assertFalse(value['namespace_isolation_verified']);self.assertNotIn('private probe details',str(value))

    def test_private_fsync_probe_is_reversible_and_links_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);private=root/'private';private.mkdir(mode=0o700)
            value=inspect_host(state_root=private,engine=SimpleNamespace(probe=lambda:{}))
            self.assertTrue(value['storage']['private_writable_fsync_verified'])
            self.assertFalse(value['storage']['persistent_across_restart_verified']);self.assertEqual(list(private.iterdir()),[])
            alias=root/'alias';alias.symlink_to(private,target_is_directory=True)
            with self.assertRaises(ValueError):inspect_host(state_root=alias)
