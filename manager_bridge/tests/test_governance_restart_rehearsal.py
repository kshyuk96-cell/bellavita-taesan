"""Actual separate manager processes and bounded synthetic command workers."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from manager_bridge.governance_restart_rehearsal import rehearse_governance_restart, PROCESS_TIMEOUT, _child


class GovernanceRestartRehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            cls.report=rehearse_governance_restart(work_parent=directory)
            cls.parent_cleaned=not list(Path(directory).iterdir())

    def test_distinct_real_manager_processes_reload_each_shared_case(self):
        for scenario in self.report['scenarios'].values():
            first,second=scenario['first_process'],scenario['second_process']
            self.assertEqual(first['task_id'],second['task_id'])
            self.assertEqual(first['database_identity_digest'],second['database_identity_digest'])
            self.assertEqual(len(first['database_identity_digest']),64)
            self.assertEqual(len(first['source_head']),40);self.assertEqual(len(second['source_head']),40)
            self.assertTrue(scenario['same_task_and_database_verified'])
            self.assertNotEqual(first['manager_pid'],second['manager_pid'])
            self.assertNotEqual(first['instance_id'],second['instance_id'])
            for process in (first,second):
                self.assertEqual(process['process_exit'],0);self.assertTrue(process['leader_released'])
                self.assertTrue(0<=process['elapsed_seconds']<=PROCESS_TIMEOUT+1)

    def test_clean_restart_uses_persisted_executed_receipt_without_first_command_replay(self):
        scenario=self.report['scenarios']['clean'];first,second=scenario['first_process'],scenario['second_process']
        self.assertEqual(first['host_phase'],'executed');self.assertEqual(first['hierarchy_state'],'developing')
        self.assertEqual(first['source_value'],1);self.assertEqual(first['developer_commands'],1)
        self.assertFalse(scenario['original_command_duplicated'])
        self.assertEqual(scenario['original_write_receipts'],1)
        self.assertEqual(scenario['duplicate_observation_scope'],'fixed_fixture_value1_receipts')
        phases=[r['phase'] for r in second['actions']]
        self.assertTrue(phases);self.assertEqual(phases[0],'collect_development')
        self.assertNotIn('dispatch_development',phases)
        if scenario['completed']:
            self.assertEqual(scenario['state'],'passed');self.assertEqual(second['developer_commands'],2)
            self.assertIn('dispatch_rework',phases)
            self.assertLess(phases.index('collect_development'),phases.index('dispatch_rework'))
            self.assertEqual(second['rework_count'],1);self.assertTrue(second['technical_accepted'])
            self.assertEqual([row['written_value'] for row in scenario['developer_command_journal']],[1,2])
            self.assertEqual([row['recommendation'] for row in scenario['semantic_command_journal']],['rework','pass'])
            self.assertTrue(second['mechanical_checks']);self.assertTrue(all(c['exit_code']==0 for c in second['mechanical_checks']))
        else:
            self.assertEqual(scenario['state'],'blocked');self.assertFalse(second['technical_accepted'])
            self.assertEqual(second['developer_commands'],1);self.assertEqual(second['hierarchy_state'],'needs_verification')

    def test_unknown_completion_retains_write_and_quarantine_without_any_normal_reexecution(self):
        scenario=self.report['scenarios']['uncertain'];first,second=scenario['first_process'],scenario['second_process']
        self.assertTrue(scenario['completed']);self.assertTrue(scenario['quarantine_preserved'])
        self.assertFalse(scenario['normal_restart_reexecuted'])
        self.assertEqual(first['host_phase'],second['host_phase']);self.assertEqual(second['host_phase'],'uncertain')
        self.assertEqual(first['developer_commands'],1);self.assertEqual(second['developer_commands'],1)
        self.assertEqual(first['source_value'],1);self.assertEqual(second['source_value'],1)
        self.assertEqual(first['source_head'],second['source_head'])
        self.assertFalse(first['technical_accepted']);self.assertFalse(second['technical_accepted'])
        self.assertEqual(len(scenario['developer_command_journal']),1)
        self.assertEqual(scenario['developer_command_journal'][0]['exit_code'],0)
        self.assertEqual(scenario['semantic_command_journal'],[])
        self.assertEqual(len(second['actions']),4)
        self.assertTrue(all(a['state']=='project_quarantined' for a in second['actions']))

    def test_portable_proof_has_no_token_paths_or_native_reboot_claim(self):
        report=self.report
        self.assertEqual(report['scope'],'disposable_synthetic_process_restart')
        self.assertEqual(report['model_calls'],0)
        for field in ('true_os_reboot_verified','actual_codex_execution_verified','customer_repositories_used',
                      'external_tester_engine_used','operational_change','always_on_verified'):
            self.assertIs(report[field],False)
        self.assertTrue(report['workspaces_destroyed_on_return']);self.assertTrue(self.parent_cleaned)
        serialized=json.dumps(report)
        self.assertNotIn('manager.token',serialized);self.assertNotIn('lease_token',serialized);self.assertNotIn('/tmp/',serialized)

    def test_public_or_symlink_work_parent_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);public=root/'public';public.mkdir();public.chmod(0o755)
            alias=root/'alias';alias.symlink_to(root,target_is_directory=True)
            for path in (public,alias):
                with self.subTest(path=path),self.assertRaises(ValueError):rehearse_governance_restart(work_parent=path)
            self.assertEqual(sorted(p.name for p in root.iterdir()),['alias','public'])

    def test_child_mode_rejects_unrecognized_stage_before_host_access(self):
        with self.assertRaises(ValueError):_child('/nonexistent','arbitrary')

    def test_cli_never_overwrites_previous_proof(self):
        from scripts.rehearse_governance_restart import main
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'proof.json';output.write_text('original proof')
            capture=io.StringIO()
            with redirect_stdout(capture):code=main(['--output',str(output)])
            self.assertEqual(code,1);self.assertEqual(output.read_text(),'original proof')
            self.assertEqual(json.loads(capture.getvalue())['state'],'unverified')
            self.assertNotIn(directory,capture.getvalue())
