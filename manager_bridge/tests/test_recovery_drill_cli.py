"""Actual process invocation; no live recovery or external paths."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


class RecoveryDrillCliTests(unittest.TestCase):
    def test_actual_synthetic_recovery(self):
        script=Path(__file__).resolve().parents[2]/'scripts/rehearse_governance_recovery.py'
        run=subprocess.run([sys.executable,str(script)],capture_output=True,text=True,timeout=30)
        self.assertEqual(0,run.returncode,run.stderr)
        value=json.loads(run.stdout)
        self.assertTrue(value['passed'])
        self.assertTrue(all(value['checks'].values()))
        self.assertFalse(value['offsite_verified'])
        self.assertFalse(value['always_on_verified'])
        self.assertFalse(value['customer_data_accessed'])

    def test_no_external_path_selector(self):
        script=Path(__file__).resolve().parents[2]/'scripts/rehearse_governance_recovery.py'
        run=subprocess.run([sys.executable,str(script),'--state-root','/not-a-customer-target'],capture_output=True,text=True,timeout=30)
        self.assertEqual(2,run.returncode)
        self.assertEqual('',run.stdout)
