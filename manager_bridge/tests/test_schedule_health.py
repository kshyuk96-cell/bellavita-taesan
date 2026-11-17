import json
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_governance_supervision as fixtures
from manager_bridge.governance_supervision import GovernanceSupervision,SupervisionSettings
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.store import Conflict


class ScheduleHealthTests(unittest.TestCase):
    setUp=fixtures.SupervisionTests.setUp
    observe=fixtures.SupervisionTests.observe
    verify=fixtures.SupervisionTests.verify
    host=fixtures.SupervisionTests.host
    supervisor=fixtures.SupervisionTests.supervisor

    def job(self,service,name='auth'):
        return next(j for j in service.report(self.manager,['project'])['jobs'] if j['job']==name)

    def test_nonfinite_and_nonpositive_claim_clock_denied(self):
        service=self.supervisor()
        for clock in (float('nan'),float('inf'),0,True):
            with patch.object(self.store,'_now',return_value=clock):
                with self.assertRaises(Conflict):service._claim(self.manager,'auth',60)
        self.assertEqual([],service.report(self.manager,['project'])['jobs'])

    def test_rollback_blocks_reclaim_and_reports(self):
        service=self.supervisor();service.poll(self.manager);self.now-=1
        self.assertIsNone(service._claim(self.manager,'auth',60))
        self.assertEqual('clock_rollback',self.job(service)['schedule']['reason'])
        self.assertEqual('verify_clock',self.job(service)['schedule']['next_action'])

    def test_missing_schedule_not_mistaken_for_healthy(self):
        service=self.supervisor();report=service.report(self.manager,['project'])
        self.assertEqual(['auth','health','recovery'],report['schedule_health']['missing_jobs'])
        self.assertTrue(report['schedule_health']['attention_required'])
        self.assertFalse(report['schedule_health']['scheduler_running_verified'])

    def test_overdue_age_is_diagnostic_only(self):
        service=self.supervisor();service.poll(self.manager);self.now+=90
        row=self.job(service,'health');self.assertEqual(60,row['schedule']['overdue_seconds'])
        self.assertEqual(90,row['schedule']['elapsed_seconds'])
        self.assertEqual('due',row['schedule']['reason'])
        self.assertFalse(row['schedule']['automatic_replay'])

    def test_changed_schedule_never_looks_current(self):
        first=self.supervisor();first.poll(self.manager)
        second=self.supervisor(auth_interval=120)
        row=self.job(second);self.assertTrue(row['stale'])
        self.assertFalse(row['schedule']['settings_current'])
        self.assertEqual('schedule_settings_changed',row['schedule']['reason'])

    def test_finish_requires_same_schedule_binding(self):
        service=self.supervisor();claim=service._claim(self.manager,'auth',60)
        service.binding='changed'
        with self.assertRaises(Conflict):service._finish(self.manager,'auth',claim,'observed',{'items':[]})
        self.assertEqual('running',self.job(service)['state'])

    def test_disabled_backup_history_is_preserved(self):
        root=self.root/'backups';root.mkdir(mode=0o700)
        first=GovernanceSupervision(self.host(),ContinuousManager(self.core),['project'],SupervisionSettings(backup_interval=300,backup_root=str(root)),health=lambda *_:{},owner_token_loader=lambda:self.owner)
        first.poll(self.manager)
        second=self.supervisor();row=self.job(second,'backup')
        self.assertFalse(row['schedule']['enabled'])
        self.assertEqual('disabled_history_retained',row['schedule']['reason'])
        self.assertTrue(list(root.iterdir()))

    def test_unknown_attempt_stays_blocked_after_restart(self):
        service=self.supervisor();service._claim(self.manager,'auth',60);self.now+=10000
        restarted=self.supervisor()
        self.assertIsNone(restarted._claim(self.manager,'auth',60))
        row=self.job(restarted);self.assertEqual('attempt_unresolved',row['schedule']['reason'])
        self.assertFalse(row['schedule']['worker_stop_inferred'])

    def test_auth_action_and_attention_are_deduplicated(self):
        service=self.supervisor();service.poll(self.manager)
        report=service.report(self.manager,['project'])
        self.assertEqual('check_registered_credentials',self.job(service)['schedule']['next_action'])
        reasons=report['schedule_health']['attention_reasons']
        self.assertEqual(sorted(set(reasons)),reasons)
        self.assertNotIn(self.owner,json.dumps(report));self.assertNotIn(str(self.root),json.dumps(report))

    def test_subset_report_checks_full_shared_configuration(self):
        service=GovernanceSupervision(self.host(),ContinuousManager(self.core),['project','other'],SupervisionSettings(),health=lambda *_:{})
        service.poll(self.manager)
        old=self.registry.get(self.owner,'other')
        self.registry.register(self.owner,'other',old['config'],expected_revision=old['revision'])
        report=service.report(self.manager,['project'])
        self.assertTrue(all(j['stale'] for j in report['jobs']))
        self.assertTrue(all(not j['schedule']['project_configuration_current'] for j in report['jobs']))

    def test_invalid_retained_clock_cannot_reclaim_or_break_json_report(self):
        service=self.supervisor();service.poll(self.manager)
        with self.store._transaction() as c:
            c.execute("UPDATE governance_supervision_jobs SET next_due=NULL WHERE job='auth'")
        with self.assertRaises(Conflict):service._claim(self.manager,'auth',60)
        report=service.report(self.manager,['project']);json.dumps(report,allow_nan=False)
        self.assertEqual('clock_unavailable',self.job(service)['schedule']['reason'])

    def test_subset_diagnostics_retain_shared_auth_attention(self):
        service=GovernanceSupervision(self.host(),ContinuousManager(self.core),['project','other'],SupervisionSettings(),health=lambda *_:{},expiration_provider=lambda project,role:{'state':'observed','expires_at':self.now+10000} if project=='project' else None)
        service.poll(self.manager)
        row=next(j for j in service.report(self.manager,['project'])['jobs'] if j['job']=='auth')
        self.assertTrue(all(i['state']=='current' for i in row['snapshot']['items']))
        self.assertEqual('credentials_need_attention',row['schedule']['reason'])
