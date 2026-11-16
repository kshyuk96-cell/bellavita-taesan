"""Actual synthetic subprocess flow; never patched mechanical isolation evidence."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from manager_bridge.governance_flow_rehearsal import rehearse_governance_flow, COMMAND_TIMEOUT


class GovernanceFlowRehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report=rehearse_governance_flow()

    def test_actual_developer_and_independent_semantic_commands_are_recorded(self):
        report=self.report
        self.assertGreaterEqual(len(report['developer_commands']),1)
        first=report['developer_commands'][0]
        self.assertEqual((first['exit_code'],first['written_value']),(0,1))
        self.assertTrue(0<=first['elapsed_seconds']<=COMMAND_TIMEOUT+1)
        first_review=report['semantic_commands'][0]
        self.assertEqual((first_review['recommendation'],first_review['observed_value'],first_review['exit_code']),('rework',1,1))
        self.assertTrue(0<=first_review['elapsed_seconds']<=COMMAND_TIMEOUT+1)
        self.assertEqual(len(first_review['stdout_digest']),64)

    def test_real_isolation_is_required_before_manager_acceptance(self):
        report=self.report
        if report['mechanical_probe']['isolation_verified']:
            self.assertEqual(report['state'],'passed')
            self.assertTrue(report['completed_full_flow']);self.assertTrue(report['parent_technical_accepted'])
            self.assertTrue(report['mechanical_checks'])
            self.assertTrue(all(check['exit_code']==0 for check in report['mechanical_checks']))
        else:
            self.assertEqual(report['state'],'blocked')
            self.assertEqual(report['blocker'],'mechanical_isolation_unavailable')
            self.assertFalse(report['completed_full_flow']);self.assertFalse(report['parent_technical_accepted'])
            self.assertFalse(report['manager_rework_observed']);self.assertFalse(report['preapproved_dependent_dispatched'])
            self.assertEqual(report['parent_state'],'needs_verification')

    def test_only_exact_owner_approved_dependent_task_can_follow(self):
        report=self.report
        self.assertTrue(report['unapproved_start_denied']);self.assertTrue(report['unapproved_tasks_remain_pending'])
        if report['completed_full_flow']:
            self.assertEqual(report['rework_count'],1);self.assertTrue(report['manager_rework_observed'])
            self.assertEqual([row['recommendation'] for row in report['semantic_commands']],['rework','pass'])
            self.assertEqual([row['written_value'] for row in report['developer_commands']],[1,2,3])
            self.assertTrue(report['preapproved_dependent_dispatched']);self.assertTrue(report['unapproved_next_denied'])
            self.assertTrue(report['unapproved_next_guard_exercised']);self.assertEqual(report['synthetic_owner_approvals'],2)
            dispatches=[row for row in report['actions'] if row['task_id']==report['child_task_id'] and row['phase']=='dispatch_development']
            self.assertEqual(len(dispatches),1);self.assertEqual(dispatches[0]['state'],'confirmed')
        else:
            self.assertFalse(report['unapproved_next_guard_exercised']);self.assertIsNone(report['child_task_id'])
            self.assertEqual(report['synthetic_owner_approvals'],1)

    def test_report_is_synthetic_and_trace_is_only_a_recorded_snapshot(self):
        report=self.report
        self.assertEqual(report['scope'],'disposable_synthetic_command_workers')
        self.assertFalse(report['actual_codex_execution_verified']);self.assertEqual(report['model_calls'],0)
        for field in ('customer_repositories_used','operational_change','external_tester_engine_used','deployment_performed','always_on_verified'):
            self.assertIs(report[field],False)
        self.assertEqual(report['trace']['scope'],'recorded_database_snapshot')
        self.assertIs(report['trace']['current_technical_acceptance_verified'],False)
        self.assertIs(report['trace']['execution_authorized'],False)
        # Credentials and temporary host paths are never part of the portable artifact.
        serialized=json.dumps(report)
        self.assertNotIn('manager.token',serialized);self.assertNotIn('/tmp/',serialized)

    def test_disposable_workspace_is_removed_from_private_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            report=rehearse_governance_flow(work_parent=root,max_ticks=4)
            self.assertTrue(report['workspaces_destroyed_on_return'])
            self.assertEqual(list(root.iterdir()),[])
            self.assertFalse(report['parent_technical_accepted'])

    def test_unsafe_parent_and_unbounded_ticks_are_rejected_before_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);public=root/'public';public.mkdir(mode=0o755);public.chmod(0o755)
            alias=root/'alias';alias.symlink_to(root,target_is_directory=True)
            for parent in (public,alias):
                with self.subTest(parent=parent),self.assertRaises(ValueError):rehearse_governance_flow(work_parent=parent)
            for ticks in (False,0,3,61,'30'):
                with self.subTest(ticks=ticks),self.assertRaises(ValueError):rehearse_governance_flow(max_ticks=ticks)
            self.assertEqual(sorted(p.name for p in root.iterdir()),['alias','public'])

    def test_cli_never_overwrites_an_existing_proof(self):
        from scripts.rehearse_governance_flow import main
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'result.json';output.write_text('existing approved proof')
            capture=io.StringIO()
            with redirect_stdout(capture):result=main(['--output',str(output)])
            self.assertEqual(result,1);self.assertEqual(output.read_text(),'existing approved proof')
            self.assertEqual(json.loads(capture.getvalue())['state'],'unverified')

    def test_cli_invalid_configuration_does_not_create_result_or_expose_paths(self):
        from scripts.rehearse_governance_flow import main
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'result.json';capture=io.StringIO()
            with redirect_stdout(capture):result=main(['--output',str(output),'--max-ticks','0'])
            self.assertEqual(result,1);self.assertFalse(output.exists())
            body=json.loads(capture.getvalue());self.assertEqual(body['state'],'unverified')
            self.assertFalse(body['completed_full_flow']);self.assertNotIn(directory,capture.getvalue())
