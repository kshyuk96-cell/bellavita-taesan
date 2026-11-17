import json
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.tests import test_governance_service as service_fixtures
from manager_bridge.workflow_trace import WorkflowTrace
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.control import request
from manager_bridge.operations_control_api import PREFIX
from manager_bridge.store import PermissionDenied,NotFound,token_hash


class WorkflowTraceTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.ThreeTierTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.trace=WorkflowTrace(self.fx.core)

    def test_recorded_acceptance_is_never_promoted_to_fresh_authority(self):
        task,reviewed=self.fx.reviewed()
        self.fx.core.decide(self.fx.manager,task['id'],reviewed['candidate_digest'])
        with patch.object(self.fx.core,'get',side_effect=AssertionError('no observer refresh')):
            report=self.trace.report(self.fx.manager,task['id'])
        self.assertEqual(report['recorded_hierarchy_state'],'accepted')
        self.assertTrue(report['binding_matches_task']);self.assertGreater(report['event_count'],0)
        self.assertFalse(report['current_technical_acceptance_verified']);self.assertFalse(report['operational_authorized'])
        self.assertFalse(report['always_on_verified']);self.assertFalse(report['fresh_observers_invoked'])
        self.assertNotIn(self.fx.dev,json.dumps(report));self.assertNotIn('lease_hash',json.dumps(report))

    def test_bounded_events_and_next_assignments_use_exact_parent(self):
        parent,reviewed=self.fx.reviewed();self.fx.core.decide(self.fx.manager,parent['id'],reviewed['candidate_digest'])
        child=self.fx.task(dependencies=[parent['id']],base='b'*40);unapproved=self.fx.task(dependencies=[parent['id']],approved=False,base='b'*40)
        self.fx.core.approve_next(self.fx.manager,parent['id'],child['id'],reviewed['candidate_digest'],'next-fixture',developer_id='dev')
        report=self.trace.report(self.fx.manager,parent['id'],limit=1)
        self.assertTrue(report['events_truncated']);self.assertEqual(len(report['events']),1)
        self.assertEqual(report['next_assignments'][0]['task_id'],child['id'])
        self.assertNotEqual(report['next_assignments'][0]['task_id'],unapproved['id'])
        self.assertFalse(report['next_assignments'][0]['execution_verified'])

    def test_unassigned_without_controller_and_stale_config_are_explicit(self):
        task=self.fx.task();report=self.trace.report(self.fx.owner,task['id'])
        self.assertEqual(report['recorded_hierarchy_state'],'unassigned');self.assertFalse(report['controller_history_available'])
        self.fx.core.start(self.fx.manager,task['id'],'dev','trace-start')
        config=self.fx.registry.get(self.fx.owner,'project')
        self.fx.registry.register(self.fx.owner,'project',{**config['config'],'name':'Changed'},expected_revision=config['revision'])
        self.assertFalse(self.trace.report(self.fx.manager,task['id'])['binding_matches_task'])

    def test_worker_cross_project_revoked_and_bad_limits_denied(self):
        task=self.fx.task();limited=self.fx.store.add_token('limited','manager',['other'])
        with self.assertRaises(PermissionDenied):self.trace.report(self.fx.dev,task['id'])
        with self.assertRaises(NotFound):self.trace.report(limited,task['id'])
        for limit in (0,101,True,1.5):
            with self.assertRaises(ValueError):self.trace.report(self.fx.manager,task['id'],limit=limit)
        self.fx.store.revoke_token(self.fx.owner,token_hash(self.fx.manager))
        with self.assertRaises(PermissionDenied):self.trace.report(self.fx.manager,task['id'])

    def test_action_window_checks_bindings_without_exposing_requests_or_reasons(self):
        ContinuousManager(self.fx.core)
        task=self.fx.task()
        with self.fx.store._transaction() as c:
            c.execute('INSERT INTO continuous_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      ('a'*64,task['id'],'project','start',json.dumps({'task_id':'other'}),token_hash(self.fx.manager),1,'uncertain',1,1000,
                       'private-reference','private-reason',1000,1000))
        report=self.trace.report(self.fx.manager,task['id'])
        self.assertTrue(report['controller_history_available']);self.assertEqual(report['action_count'],1)
        self.assertFalse(report['actions'][0]['binding_matches_task']);self.assertEqual(report['actions'][0]['recorded_state'],'uncertain')
        self.assertTrue(report['recovery']['project_quarantined_recorded'])
        self.assertEqual(report['recovery']['state'],'manual_reconciliation_required')
        self.assertFalse(report['recovery']['automatic_retry_authorized'])
        self.assertNotIn('private-',json.dumps(report));self.assertNotIn('actor_hash',json.dumps(report))

    def test_other_task_uncertainty_quarantines_selected_project_without_revealing_identity(self):
        ContinuousManager(self.fx.core);selected=self.fx.task();uncertain=self.fx.task()
        with self.fx.store._transaction() as c:
            c.execute('INSERT INTO continuous_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      ('b'*64,uncertain['id'],'project','dispatch_development','{}',token_hash(self.fx.manager),1,'uncertain',1,1000,None,None,1000,1000))
        report=self.trace.report(self.fx.manager,selected['id'])
        self.assertEqual(report['recovery']['project_uncertain_actions'],1)
        self.assertEqual(report['recovery']['task_uncertain_actions'],0)
        self.assertEqual(report['action_count'],0);self.assertNotIn(uncertain['id'],json.dumps(report))

    def test_running_lease_expiry_never_grants_replay_or_marks_worker_stopped(self):
        ContinuousManager(self.fx.core);task=self.fx.task()
        with self.fx.store._transaction() as c:
            c.execute('INSERT INTO continuous_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      ('c'*64,task['id'],'project','dispatch_development','{}',token_hash(self.fx.manager),1,'running',1,900,None,None,900,900))
        self.fx.now+=1000
        report=self.trace.report(self.fx.manager,task['id'])
        self.assertEqual(report['recovery']['state'],'running_outcome_pending')
        self.assertFalse(report['recovery']['lease_expiry_allows_retry']);self.assertFalse(report['recovery']['processes_stopped_verified'])
        with self.fx.store._transaction() as c:
            self.assertEqual(c.execute("SELECT state FROM continuous_actions WHERE action_key=?",('c'*64,)).fetchone()[0],'running')

    def test_other_project_uncertainty_does_not_change_this_projects_status(self):
        ContinuousManager(self.fx.core);task=self.fx.task();other=self.fx.task(project='other')
        with self.fx.store._transaction() as c:
            c.execute('INSERT INTO continuous_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      ('d'*64,other['id'],'other','start','{}',token_hash(self.fx.manager),1,'uncertain',1,1000,None,None,1000,1000))
        report=self.trace.report(self.fx.manager,task['id'])
        self.assertEqual(report['recovery']['state'],'no_uncertainty_recorded');self.assertFalse(report['recovery']['project_quarantined_recorded'])

    def test_actual_host_assignment_schema_joins_exact_action_binding(self):
        from manager_bridge.tests import test_continuous_host as host_fixtures
        host=host_fixtures.HostTests();host.setUp();self.addCleanup(host.doCleanups)
        task=host.task()
        for _ in range(3):host.tick()
        report=WorkflowTrace(host.core).report(host.manager,task['id'])
        self.assertIsNotNone(report['host']);self.assertTrue(report['host']['task_version_manifest_match'])
        self.assertTrue(report['host']['config_binding_verified'])
        self.assertNotIn(str(host.root),json.dumps(report));self.assertNotIn('lease_token',json.dumps(report))


class WorkflowTraceHTTPTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    compose=service_fixtures.GovernanceServiceTests.compose
    start_http=service_fixtures.GovernanceServiceTests.start_http
    task=fixtures.ThreeTierTests.task

    def test_authenticated_read_only_trace_route(self):
        service=self.compose();base=self.start_http(service);task=self.task()
        route=PREFIX+'workflow/trace';body={'task_id':task['id']}
        with patch.object(self.store,'_expire',side_effect=AssertionError('read-only trace must not expire leases')):
            result=request(base,self.manager,'POST',route,body)
        self.assertTrue(result['ok'],result);self.assertEqual(result['result']['scope'],'recorded_database_snapshot')
        self.assertEqual(request(base,self.dev,'POST',route,body)['status'],403)
        self.assertEqual(request(base,self.manager,'POST',route,{**body,'command':'execute'})['status'],400)
        self.assertEqual(request(base,self.manager,'POST',route,{**body,'limit':True})['status'],400)
