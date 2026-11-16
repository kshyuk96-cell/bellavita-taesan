"""Local preflight and process proof; no model, cloud, or infrastructure claim."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from manager_bridge.host_preflight import (inspect_preflight,local_host_identity,prepare_restart_challenge,
    verify_restart_challenge,RegisteredWorkerObservations,CHALLENGE_FILE)


class HostPreflightTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.root.chmod(0o700)
        self.expected=local_host_identity()
        self.engine=SimpleNamespace(probe=lambda:{'isolation_verified':True})

    def test_requested_identity_never_attests_observed_host(self):
        value=inspect_preflight(expected_host_identity='0'*64,state_root=self.root,engine=self.engine)
        self.assertFalse(value['exact_expected_local_host_verified'])
        self.assertNotEqual('0'*64,value['observed_local_host_identity'])
        self.assertTrue(value['storage']['private_writable_fsync_verified'])
        self.assertFalse(value['infrastructure_persistence_verified'])
        self.assertFalse(value['remote_host_attestation_verified'])
        self.assertTrue(inspect_preflight(expected_host_identity=self.expected,state_root=self.root,engine=self.engine)['exact_expected_local_host_verified'])

    def test_private_path_challenge_schema_and_same_process_refused(self):
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):prepare_restart_challenge(expected_host_identity=self.expected,state_root=self.root)
        self.root.chmod(0o700)
        prepare_restart_challenge(expected_host_identity=self.expected,state_root=self.root)
        self.assertEqual(0o600,(self.root/CHALLENGE_FILE).stat().st_mode&0o777)
        with self.assertRaises(ValueError):verify_restart_challenge(expected_host_identity=self.expected,state_root=self.root)
        with self.assertRaises(ValueError):prepare_restart_challenge(expected_host_identity=self.expected,state_root=self.root)
        data=json.loads((self.root/CHALLENGE_FILE).read_text());data['nonce']='modified'
        (self.root/CHALLENGE_FILE).write_text(json.dumps(data))
        with self.assertRaises(ValueError):verify_restart_challenge(expected_host_identity=self.expected,state_root=self.root)

    def test_actual_cli_restart_waits_for_two_distinct_processes(self):
        script=Path(__file__).resolve().parents[2]/'scripts'/'check_governance_host.py'
        result=subprocess.run([sys.executable,'-B',str(script),'restart-proof','--state-root',str(self.root),'--expected-host-identity',self.expected],capture_output=True,text=True,timeout=30)
        self.assertEqual(0,result.returncode,result.stderr)
        value=json.loads(result.stdout)
        self.assertTrue(value['process_restart_persistence_verified'])
        self.assertEqual(0,value['writer_exit_code']);self.assertEqual(0,value['reader_exit_code'])
        self.assertFalse(value['infrastructure_persistence_verified']);self.assertFalse(value['reboot_verified'])

    def test_registered_process_liveness_and_observed_exit_only(self):
        registry=RegisteredWorkerObservations()
        with self.assertRaises(ValueError):registry.observe('unknown')
        with self.assertRaises(ValueError):registry.register('untrusted',SimpleNamespace(pid=os.getpid()))
        process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        def cleanup():
            if process.poll() is None:process.kill()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        registry.register('trusted',process)
        self.assertEqual('running',registry.observe('trusted')['state'])
        gone={'exact_process_gone':True,'pid_reused':False,'original_group_gone':False}
        with patch('manager_bridge.host_preflight.CodexHost._stop_observation',return_value=gone):
            value=registry.observe('trusted');self.assertEqual('unobserved',value['state']);self.assertFalse(value['stopped_verified'])
        with patch('manager_bridge.host_preflight.CodexHost._stop_observation',return_value={**gone,'pid_reused':True}):
            self.assertFalse(registry.observe('trusted')['stopped_verified'])
        with patch('manager_bridge.host_preflight.CodexHost._process_identity',side_effect=OSError('unavailable')):
            value=registry.observe('trusted');self.assertEqual('unknown',value['state']);self.assertFalse(value['stopped_verified'])
        process.terminate();process.wait(timeout=5)
        value=registry.observe('trusted');self.assertTrue(value['process_exit_observed']);self.assertTrue(value['stopped_verified'])
        self.assertFalse(value['safe_for_recovery']);self.assertFalse(value['descendants_verified']);self.assertFalse(value['signals_sent'])
