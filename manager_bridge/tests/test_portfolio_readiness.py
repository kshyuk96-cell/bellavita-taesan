import copy
import json
import sqlite3
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_three_tier as hierarchy_fixtures
from manager_bridge.tests import test_qa_admission as qa_fixtures
from manager_bridge.portfolio_readiness import PortfolioReadiness
from manager_bridge.connection_readiness import Connections,SourcePin,HostProfile,ConnectionFact
from manager_bridge.source_watch import SourceWatch
from manager_bridge.operating_budget import OperatingBudget,BudgetPolicy,BudgetAuthObservation
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.host_admission import HostAdmission,HostRequirement,HostObservation
from manager_bridge.host_preflight import local_host_identity
from manager_bridge.operations_reporting import OperationsReporting
from manager_bridge.control_digest import ControlDigest
from manager_bridge.governance_portfolio import Portfolio
from manager_bridge.store import Conflict,PermissionDenied,NotFound,token_hash,digest


class PortfolioReadinessTests(unittest.TestCase):
    def setUp(self):
        self.fx=hierarchy_fixtures.ThreeTierTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.store,self.registry,self.core=self.fx.store,self.fx.registry,self.fx.core
        self.owner,self.manager=self.fx.owner,self.fx.manager
        self.connections=Connections(self.store,self.registry);SourceWatch(self.store,self.registry)
        self.budget=OperatingBudget(self.core,require_policy=True)
        self.service=PortfolioReadiness(self.core,connections=self.connections,budget=self.budget)

    def registration(self,project='new-customer'):
        cfg={**self.fx.config,'source_ref':'fixture/'+project,'maintenance_enabled':False}
        return {'project':project,'config':cfg,'expected_revision':0,'expected_fingerprint':None}

    def seed_current_requirements(self):
        project='project';cfg=self.registry.get(self.owner,project)
        with self.store._transaction() as c:
            c.execute('INSERT INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',(project,cfg['fingerprint'],'a'*40,'v1','fixture/source',self.fx.now,1))
            c.execute('INSERT INTO manager_source_observation_status VALUES (?,?,?,?)',(project,cfg['fingerprint'],'unchanged',self.fx.now))
        pin=SourcePin(project,'github','fixture/project','main','a'*40,'v1')
        def observed(p,cap):return ConnectionFact(p,cap,'a'*40,'verified','fixture/connected',self.fx.now,passed=1 if cap=='fixture_tests' else 0)
        self.connections.profiles[project]=HostProfile(pin,observed);self.connections.inspect(self.manager,project)
        qa=qa_fixtures.QAAdmissionTests();qa.fixture()
        self.core.governance_gate.register_policy(self.owner,project,contract_version='v1',expected_contract=qa.expected_contract,required_coverage=['money'],provider_identity='fixture')
        self.core.governance_gate.providers[project]=lambda *_:(_ for _ in ()).throw(AssertionError('not called by readiness'))
        host=HostAdmission(self.core,observer=lambda requirement:HostObservation(requirement.host_identity,requirement.application_commit,True,True,self.fx.now))
        host.install(self.owner,project,HostRequirement(local_host_identity(),'c'*40,30));host.refresh(self.manager,project)
        self.budget.host_admission=host;self.service.host_admission=host
        self.budget.install(self.owner,project,BudgetPolicy(2,10,300))
        self.budget.auth_observer=lambda p:BudgetAuthObservation(p,cfg['fingerprint'],'ready',self.fx.now,self.fx.now+100,'fixture/auth')
        self.budget.refresh_auth(self.manager,project)
        ContinuousManager(self.core)
        with self.store._transaction() as c:
            c.execute('INSERT INTO continuous_leader VALUES (1,?,?,?,?,?)',('fixture',token_hash(self.manager),digest([project]),1,self.fx.now+50))

    def test_owner_batch_registration_is_atomic_idempotent_enrolled_and_inactive(self):
        items=[self.registration('new-one'),self.registration('new-two')]
        result=self.service.register_batch(self.owner,'register-new',items)
        self.assertEqual(result,self.service.register_batch(self.owner,'register-new',items))
        self.assertFalse(result['execution_authorized']);self.assertFalse(result['credentials_created'])
        for project in ('new-one','new-two'):
            cfg=self.registry.get(self.owner,project);self.assertEqual(cfg['revision'],1);self.assertFalse(cfg['config']['maintenance_enabled'])
            with self.store._transaction() as c:self.assertIsNotNone(c.execute('SELECT 1 FROM governance_enrolled_projects WHERE project=?',(project,)).fetchone())
        changed=copy.deepcopy(items);changed[0]['config']['branch']='different'
        with self.assertRaises(Conflict):self.service.register_batch(self.owner,'register-new',changed)

    def test_batch_scope_role_cas_and_partial_insert_failure_leave_no_new_customer(self):
        items=[self.registration('new-one'),self.registration('new-two')]
        with self.assertRaises(PermissionDenied):self.service.register_batch(self.manager,'manager',items)
        limited=self.store.add_token('limited-owner','owner',['new-one'])
        with self.assertRaises(PermissionDenied):self.service.register_batch(limited,'limited',items)
        with self.store._transaction() as c:
            c.execute("CREATE TRIGGER fail_customer BEFORE INSERT ON maintenance_projects WHEN NEW.project='new-two' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.service.register_batch(self.owner,'atomic',items)
        with self.store._transaction() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM bridge_projects WHERE project='new-one'").fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM portfolio_registration_batches').fetchone()[0],0)

    def test_registration_never_overwrites_config_or_enables_maintenance(self):
        item=self.registration();item['config']['maintenance_enabled']=True
        with self.assertRaises(ValueError):self.service.register_batch(self.owner,'enabled',[item])
        item=self.registration();result=self.service.register_batch(self.owner,'first',[item]);current=result['registrations'][0]
        changed=self.registration();changed.update(expected_revision=current['revision'],expected_fingerprint=current['fingerprint']);changed['config']['branch']='different'
        with self.assertRaises(Conflict):self.service.register_batch(self.owner,'different',[changed])
        current_config=self.registry.get(self.owner,'new-customer')
        self.registry.register(self.owner,'new-customer',current_config['config'],expected_revision=1)
        with self.assertRaises(Conflict):self.service.register_batch(self.owner,'first',[item])

    def test_readiness_counts_only_observed_prerequisites_not_arbitrary_success_or_uptime(self):
        initial=self.service.assess(self.manager,['project'])
        self.assertIsNone(initial['percentage']);self.assertFalse(initial['always_on_verified'])
        self.assertLess(initial['observed_requirement_coverage']['met'],initial['observed_requirement_coverage']['total'])
        self.seed_current_requirements()
        with patch.object(self.connections,'inspect',side_effect=AssertionError('no provider probe')),patch.object(self.budget,'refresh_auth',side_effect=AssertionError('no auth callback')):
            result=self.service.assess(self.manager,['project'])
        coverage=result['observed_requirement_coverage'];self.assertEqual(coverage,{'met':12,'total':12})
        self.assertFalse(result['execution_authorized']);self.assertFalse(result['uptime_measured'])
        with self.assertRaises(TypeError):self.service.assess(self.manager,['project'],ready=True)
        self.fx.now+=101
        self.service.host_admission.refresh(self.manager,'project')
        expired=self.service.assess(self.manager,['project']);self.assertLess(expired['observed_requirement_coverage']['met'],12)
        budget=next(c for c in expired['projects'][0]['requirements'] if c['requirement']=='admission_budget')
        self.assertEqual(budget['reason'],'authentication_expired')

    def test_source_and_role_revocation_and_budget_consumption_remove_coverage(self):
        self.seed_current_requirements()
        self.store.revoke_token(self.owner,token_hash(self.fx.reviewer))
        with self.store._transaction() as c:c.execute("UPDATE manager_source_observation_status SET state='permission_denied' WHERE project='project'")
        result=self.service.assess(self.manager,['project'])
        checks={c['requirement']:c for c in result['projects'][0]['requirements']}
        self.assertEqual(checks['fresh_source']['state'],'unmet_or_unknown');self.assertEqual(checks['independent_live_role_registrations']['state'],'unmet_or_unknown')
        self.assertFalse(result['execution_authorized'])

    def test_cached_connections_for_wrong_source_never_count_as_current_ready(self):
        self.seed_current_requirements()
        with self.store._transaction() as c:
            row=c.execute("SELECT report FROM manager_connection_reports WHERE project='project'").fetchone();value=json.loads(row['report']);value['source_pin']['sha']='b'*40
            c.execute("UPDATE manager_connection_reports SET report=? WHERE project='project'",(json.dumps(value),))
        check=next(c for c in self.service.assess(self.manager,['project'])['projects'][0]['requirements'] if c['requirement']=='source_bound_connections')
        self.assertEqual(check['state'],'unmet_or_unknown')

    def test_exception_report_is_grouped_bounded_and_reuses_operations_reporting(self):
        reporting=OperationsReporting(self.core,ControlDigest(Portfolio(self.core)),budget=self.budget)
        service=PortfolioReadiness(self.core,connections=self.connections,budget=self.budget,operations_reporting=reporting)
        with patch.object(reporting,'report',wraps=reporting.report) as reused:
            result=service.exceptions(self.manager,['project'],'exceptions',limit=1)
        self.assertEqual(reused.call_count,1);self.assertLessEqual(len(result['readiness_exceptions']),1)
        self.assertTrue(result['readiness_window']['partial']);self.assertFalse(result['summary_complete'])
        self.assertFalse(result['messages_sent']);self.assertEqual(result['per_task_messages_sent'],0)
        self.assertNotIn('description',json.dumps(result));self.assertIsNone(result['percentage'])

    def test_scope_and_revocation_and_known_bearer_fail_closed(self):
        limited=self.store.add_token('limited','manager',['other'])
        with self.assertRaises((NotFound,PermissionDenied)):self.service.assess(limited,['project'])
        with self.assertRaises(ValueError):self.service.exceptions(self.manager,['project'],self.fx.dev)
        item=self.registration();item['config']['name']=self.fx.dev
        with self.assertRaises(ValueError):self.service.register_batch(self.owner,'token',[item])
        self.store.revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(PermissionDenied):self.service.assess(self.manager,['project'])

    def test_missing_gate_and_tables_and_host_gate_are_unknown_not_success(self):
        self.core.governance_gate=None
        with self.store._transaction() as c:c.execute('DROP TABLE governance_qa_policies')
        value=self.service.assess(self.manager,['project'])
        checks={c['requirement']:c['state'] for c in value['projects'][0]['requirements']}
        self.assertEqual(checks['qa_provider_registered'],'unmet_or_unknown')
        self.assertEqual(checks['qa_policy_registered'],'unmet_or_unknown')
        self.assertEqual(checks['host_admission'],'unmet_or_unknown')
        self.assertFalse(value['always_on_verified'])

    def test_budget_pass_does_not_replace_host_admission_and_no_policy_legacy_allow_not_counted(self):
        legacy=OperatingBudget(self.core,require_policy=False)
        value=PortfolioReadiness(self.core,connections=self.connections,budget=legacy).assess(self.manager,['project'])
        checks={c['requirement']:c['state'] for c in value['projects'][0]['requirements']}
        self.assertEqual(checks['admission_budget'],'unmet_or_unknown')
        self.assertEqual(checks['host_admission'],'unmet_or_unknown')

    def test_registration_does_not_expand_existing_manager_scope_or_assign_roles(self):
        self.service.register_batch(self.owner,'new-scope',[self.registration()])
        with self.assertRaises((NotFound,PermissionDenied)):self.service.assess(self.manager,['new-customer'])
        with self.store._transaction() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM hierarchy_members WHERE project='new-customer'").fetchone()[0],0)
        self.assertFalse(self.registry.get(self.owner,'new-customer')['config']['maintenance_enabled'])

    def test_reporting_callback_revocation_is_rechecked_before_returning_exceptions(self):
        reporting=OperationsReporting(self.core,ControlDigest(Portfolio(self.core)))
        service=PortfolioReadiness(self.core,connections=self.connections,budget=self.budget,operations_reporting=reporting)
        def revoked(*args,**kwargs):
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return {'urgent':[],'normal':[],'group_window':{},'snapshot_state':'recorded','summary_complete':True}
        with patch.object(reporting,'report',side_effect=revoked):
            with self.assertRaises(PermissionDenied):service.exceptions(self.manager,['project'],'revoked-report')
