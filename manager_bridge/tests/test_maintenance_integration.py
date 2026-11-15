import importlib.util
from pathlib import Path
import unittest


class MaintenanceIntegration(unittest.TestCase):
    def test_registered_approved_pipeline_executes_fixture_and_preserves_source(self):
        file=Path(__file__).resolve().parents[2]/'scripts'/'maintenance_fixture.py'
        spec=importlib.util.spec_from_file_location('taesan_maintenance_fixture',file)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        result=module.run()
        self.assertEqual(result['job_state'],'done')
        self.assertEqual(result['actual_subprocess_checks'],{'pass':2,'fail':0,'skip':0})
        self.assertEqual(result['restore_state'],'passed')
        self.assertIs(result['source_backup_unchanged'],True)
        self.assertIs(result['ack_suppresses_duplicate'],True)
        self.assertIs(result['worker_result_independently_verified'],False)
        self.assertIsNone(result['live_ai_score'])
        self.assertIs(result['operational_change'],False)
        self.assertIs(result['schedule_created'],False)
