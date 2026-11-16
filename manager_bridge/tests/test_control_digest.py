"""One user digest from scoped bounded facts, without message delivery."""
import json
from unittest.mock import patch
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.control_digest import ControlDigest
from manager_bridge.governance_portfolio import Portfolio
from manager_bridge.governance_routing import GovernanceRouting
from manager_bridge.store import Conflict,PermissionDenied,NotFound


class ControlDigestTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    task=fixtures.ThreeTierTests.task
    start=fixtures.ThreeTierTests.start
    reported=fixtures.ThreeTierTests.reported
    reviewed=fixtures.ThreeTierTests.reviewed
    accepted=fixtures.ThreeTierTests.accepted

    def test_single_portfolio_call_and_no_extra_host_callbacks_or_task_messages(self):
        self.accepted();calls=[]
        def health(*_):
            calls.append('health');return {'state':'running','http_state':'listening','manager_state':'idle','observed_at':self.now}
        portfolio=Portfolio(self.core,health=health);digest=ControlDigest(portfolio,routing=GovernanceRouting(self.core))
        with patch.object(portfolio,'report',wraps=portfolio.report) as report:
            result=digest.report(self.manager,['project'])
        self.assertEqual(report.call_count,1);self.assertEqual(calls,['health'])
        self.assertEqual(result['summary']['completed_current'],1)
        self.assertEqual(result['per_task_messages_sent'],0);self.assertFalse(result['messages_sent'])
        self.assertIsNone(result['completion_percentage']);self.assertFalse(result['always_on_verified'])

    def test_stale_acceptance_is_unchecked_and_missing_observations_unknown(self):
        self.accepted();self.source_changes={'head_sha':'d'*40}
        result=ControlDigest(Portfolio(self.core)).report(self.manager,['project'])
        self.assertEqual(result['summary']['completed_current'],0)
        self.assertEqual(result['summary']['completion_unchecked'],1)
        self.assertGreater(result['summary']['unknown_checks'],0)
        self.assertGreater(result['summary']['blocked_checks'],0)
        for forbidden in ('description','source_ref',self.manager,'90%'):
            self.assertNotIn(forbidden,json.dumps(result))

    def test_scope_is_checked_before_callbacks_and_after_revocation(self):
        limited=self.store.add_token('limited','manager',['project']);calls=[]
        def health(*_):calls.append('health');return {}
        digest=ControlDigest(Portfolio(self.core,health=health))
        with self.assertRaises((PermissionDenied,NotFound)):digest.report(limited,['other'])
        self.assertEqual(calls,[])
        portfolio=Portfolio(self.core);original=portfolio.report
        def revoked(token,projects):
            result=original(token,projects)
            with self.store._transaction() as c:c.execute('UPDATE bridge_tokens SET revoked=? WHERE subject=?',(self.now,'taesan'))
            return result
        with patch.object(portfolio,'report',side_effect=revoked):
            with self.assertRaises(PermissionDenied):ControlDigest(portfolio).report(self.manager,['project'])

    def test_config_change_after_report_rejected(self):
        portfolio=Portfolio(self.core);original=portfolio.report
        def changed(token,projects):
            result=original(token,projects)
            self.registry.register(self.owner,'project',self.config,expected_revision=1)
            return result
        with patch.object(portfolio,'report',side_effect=changed):
            with self.assertRaises(Conflict):ControlDigest(portfolio).report(self.manager,['project'])

    def test_all_windows_partial_and_owner_decisions_bounded_without_second_report(self):
        portfolio=Portfolio(self.core);source=portfolio.report(self.manager,['project'])
        source['task_window']['partial']=True;source['batch_window'].update(configured=True,partial=True)
        source['batches']=[{'id':str(i).zfill(32),'digest':'a'*64,'state':'pending_owner_approval','required_decision':'owner_business_approval','proposal_count':1} for i in range(3)]
        with patch.object(portfolio,'report',return_value=source) as report:
            result=ControlDigest(portfolio).report(self.manager,['project'],decision_limit=2)
        self.assertEqual(report.call_count,1);self.assertEqual(len(result['owner_decisions']),2)
        self.assertTrue(result['decision_window']['partial']);self.assertFalse(result['summary_complete'])
        self.assertTrue(result['task_window']['partial']);self.assertTrue(result['batch_window']['partial'])
        self.assertEqual(result['owner_decisions'][0]['cost_estimate_state'],'not_measured')
        self.assertFalse(result['owner_decisions'][0]['additional_spend_authorized'])

    def test_supervision_shared_scope_and_after_report_reauthentication(self):
        from types import SimpleNamespace
        from manager_bridge.continuous_manager import ContinuousManager
        from manager_bridge.governance_supervision import GovernanceSupervision,SupervisionSettings
        host=SimpleNamespace(core=self.core,store=self.store,registry=self.registry)
        supervision=GovernanceSupervision(host,ContinuousManager(self.core),['project','other'],SupervisionSettings(),health=lambda *_:{})
        digest=ControlDigest(Portfolio(self.core),supervision=supervision)
        limited=self.store.add_token('limited-digest','manager',['project'])
        with self.assertRaises((NotFound,PermissionDenied)):digest.report(limited,['project'])
        original=supervision.report
        def revoke(token,projects):
            result=original(token,projects)
            with self.store._transaction() as c:c.execute('UPDATE bridge_tokens SET revoked=? WHERE subject=?',(self.now,'taesan'))
            return result
        with patch.object(supervision,'report',side_effect=revoke):
            with self.assertRaises(PermissionDenied):digest.report(self.manager,['project'])

    def test_project_names_are_not_engine_permissions_and_windows_are_bounded(self):
        digest=ControlDigest(Portfolio(self.core))
        self.registry.register(self.owner,'tester',self.config)
        result=digest.report(self.owner,['tester'])
        self.assertEqual(result['projects'],['tester'])
        self.assertFalse(result['operational_authorization'])
        for limit in (True,0,101):
            with self.assertRaises(ValueError):digest.report(self.manager,['project'],decision_limit=limit)


if __name__=='__main__':unittest.main()
