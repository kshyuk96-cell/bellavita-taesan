"""Scoped evidence composition; fixtures make no external calls."""
import json
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.governance_portfolio import Portfolio
from manager_bridge.connection_readiness import Connections,HostProfile,SourcePin,ConnectionFact
from manager_bridge.source_watch import SourceWatch,SourceHead,HeadProbe
from manager_bridge.proposal_board import ProposalBoard
from manager_bridge.store import PermissionDenied,Conflict


class PortfolioTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    task=fixtures.ThreeTierTests.task
    start=fixtures.ThreeTierTests.start
    reported=fixtures.ThreeTierTests.reported
    reviewed=fixtures.ThreeTierTests.reviewed
    accepted=fixtures.ThreeTierTests.accepted

    def test_missing_connections_and_qa_are_blocked_not_owner_approvals(self):
        result=Portfolio(self.core).report(self.manager,['project','other'])
        self.assertEqual(len(result['projects']),2)
        for project in result['projects']:
            self.assertEqual(len(project['progress']),10)
            self.assertEqual(project['connections']['state'],'not_configured')
            self.assertEqual(project['qa']['policy'],'missing')
        self.assertFalse(result['always_on_verified'])
        packet=Portfolio(self.core).decision_packet(self.manager,['project'])
        self.assertEqual(packet['owner_required'],[{'kind':'qa_policy_setup','project':'project','action':'owner_register_qa_policy','execution_authorized':False}])
        self.assertNotIn('connection',json.dumps(packet['owner_required']))

    def test_source_negative_overrides_fresh_head_and_output_excludes_private_payloads(self):
        failure=[False]
        def observe(project):
            if failure[0]:raise ConnectionError('PRIVATE_PROMPT')
            return SourceHead(project,'github','fixture/project','main','a'*40,'fixture-revision','fixture/head',self.now)
        watch=SourceWatch(self.store,self.registry,{'project':HeadProbe('github','fixture/project','main',observe)})
        watch.inspect(self.manager,'project')
        service=Portfolio(self.core)
        self.assertTrue(service.report(self.manager,['project'])['projects'][0]['source']['fresh'])
        failure[0]=True;watch.inspect(self.manager,'project')
        result=service.report(self.manager,['project'])
        self.assertEqual(result['projects'][0]['source']['state'],'unavailable')
        self.assertFalse(result['projects'][0]['source']['fresh'])
        for forbidden in ('PRIVATE_PROMPT','source_ref',self.manager,'codex_home'):
            self.assertNotIn(forbidden,json.dumps(result))

    def test_health_reauthentication_after_callback_revokes_access(self):
        def health(token,projects):
            with self.store._transaction() as c:c.execute('UPDATE bridge_tokens SET revoked=? WHERE subject=?',(self.now,'taesan'))
            return {'state':'running','http_state':'listening','manager_state':'idle','observed_at':self.now}
        with self.assertRaises(PermissionDenied):Portfolio(self.core,health=health).report(self.manager,['project'])

    def test_health_source_change_precedes_bounded_acceptance_refresh(self):
        task,accepted=self.accepted()
        self.assertTrue(accepted['technical_accepted'])
        def health(token,projects):
            self.source_changes={'head_sha':'d'*40}
            return {'state':'running','http_state':'listening','manager_state':'idle','observed_at':self.now}
        result=Portfolio(self.core,health=health).report(self.manager,['project'])
        returned=result['projects'][0]['tasks'][0]
        self.assertFalse(returned['technical_accepted'])
        self.assertEqual(returned['source_freshness'],'unavailable')
        self.assertLessEqual(result['task_window']['freshness_checks'],3)
        self.assertFalse(self.core.get(self.manager,task['id'])['technical_accepted'])

    def test_overview_delay_expires_health_and_connection_evidence(self):
        self.accepted()
        pin=SourcePin('project','github','fixture/project','main','a'*40,'fixture-revision')
        profile=HostProfile(pin,lambda project,cap:ConnectionFact(project,cap,pin.sha,'verified','fixture/ready',self.now,passed=1))
        connections=Connections(self.store,self.registry,{'project':profile})
        connections.inspect(self.manager,'project')
        observed=self.now
        def slow_source(task):
            self.now+=301
            return self.observe(task)
        self.core.source_observers['project']=slow_source
        service=Portfolio(self.core,connections=connections,health=lambda *_:{'state':'running','http_state':'listening','manager_state':'idle','observed_at':observed})
        result=service.report(self.manager,['project'])
        self.assertFalse(result['service_health']['fresh'])
        self.assertEqual(result['service_health']['state'],'unavailable')
        self.assertEqual(result['projects'][0]['connections']['state'],'stale')
        self.assertNotIn('_observed_at',json.dumps(result))

    def test_overview_61_second_delay_expires_health_and_revoked_member_route(self):
        from manager_bridge.governance_routing import GovernanceRouting
        from manager_bridge.store import token_hash
        self.accepted();observed=self.now
        def slow_source(task):
            self.now+=61
            with self.store._transaction() as c:
                c.execute('UPDATE bridge_tokens SET revoked=? WHERE token_hash=?',(self.now,token_hash(self.dev)))
            return self.observe(task)
        self.core.source_observers['project']=slow_source
        service=Portfolio(self.core,routing=GovernanceRouting(self.core),health=lambda *_:{'state':'running','http_state':'listening','manager_state':'idle','observed_at':observed})
        result=service.report(self.manager,['project'])
        self.assertFalse(result['service_health']['fresh'])
        self.assertEqual(result['projects'][0]['routing']['state'],'blocked')
        self.assertEqual(result['projects'][0]['routing']['eligible_role_counts']['developer'],0)

    def test_config_change_during_callback_invalidates_report(self):
        def health(token,projects):
            self.registry.register(self.owner,'project',self.config,expected_revision=1)
            return {'state':'running','http_state':'listening','manager_state':'idle','observed_at':self.now}
        with self.assertRaises(Conflict):Portfolio(self.core,health=health).report(self.manager,['project'])

    def test_scope_checked_before_any_provider_callback(self):
        calls=[];limited=self.store.add_token('limited','manager',['project'])
        service=Portfolio(self.core,health=lambda *a:calls.append(a))
        with self.assertRaises(Exception):service.report(limited,['other'])
        self.assertEqual(calls,[])
        for projects in ([],['project','project'],[{}]):
            with self.assertRaises(ValueError):service.report(self.manager,projects)

    def test_health_and_connections_are_fresh_evidence_only(self):
        pin=SourcePin('project','github','fixture/project','main','a'*40,'fixture-revision')
        profile=HostProfile(pin,lambda project,cap:ConnectionFact(project,cap,pin.sha,'verified','fixture/ready',self.now,passed=1))
        connections=Connections(self.store,self.registry,{'project':profile})
        connections.inspect(self.manager,'project')
        service=Portfolio(self.core,connections=connections,health=lambda *_:{'state':'running','http_state':'listening','manager_state':'idle','observed_at':self.now,'private':'NEVER_RETURN'})
        result=service.report(self.manager,['project'])
        self.assertEqual(result['projects'][0]['connections']['state'],'ready')
        self.assertTrue(result['service_health']['fresh']);self.assertNotIn('NEVER_RETURN',json.dumps(result))
        self.now+=301
        self.assertEqual(service.report(self.manager,['project'])['projects'][0]['connections']['state'],'stale')

    def test_safe_dashboard_and_strict_api(self):
        from types import SimpleNamespace
        from manager_bridge.governance_portfolio import render_dashboard
        from manager_bridge.governance_portfolio_api import dispatch,PREFIX
        html=render_dashboard({'projects':[{'project':'<script>alert(1)</script>','progress':[{'item':'source','state':'blocked'}]}]})
        self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('완료율',html)
        handler=SimpleNamespace(server=SimpleNamespace(store=self.store,governance_portfolio=Portfolio(self.core)))
        answers=[];handler.respond=lambda code,body:answers.append((code,body))
        self.assertTrue(dispatch(handler,'POST',PREFIX+'dashboard',self.manager,{'projects':['project']}))
        self.assertEqual(answers[-1][1]['result']['content_type'],'text/html; charset=utf-8')
        with self.assertRaises(ValueError):dispatch(handler,'POST',PREFIX+'report',self.manager,{'projects':['project'],'provider':'private'})
        self.assertFalse(dispatch(handler,'GET',PREFIX+'report',self.manager))

    def test_qa_policy_from_old_configuration_is_blocked(self):
        from manager_bridge.tests.test_qa_admission import QAAdmissionTests
        fixture=QAAdmissionTests();fixture.fixture()
        self.core.governance_gate.register_policy(self.owner,'project',contract_version='1',expected_contract=fixture.expected_contract,required_coverage=['money'],provider_identity='fixture')
        service=Portfolio(self.core)
        self.assertEqual(service.report(self.manager,['project'])['projects'][0]['qa']['policy'],'registered')
        self.registry.register(self.owner,'project',self.config,expected_revision=1)
        self.assertEqual(service.report(self.manager,['project'])['projects'][0]['qa']['policy'],'missing')

    def test_older_pending_batch_hidden_by_window_marks_packet_incomplete(self):
        from manager_bridge.governance_portfolio import render_dashboard
        from manager_bridge.store import token_hash
        watch=SourceWatch(self.store,self.registry,{'project':HeadProbe('github','fixture/project','main',lambda project:SourceHead(project,'github','fixture/project','main','a'*40,'fixture-revision','fixture/head',self.now))})
        watch.inspect(self.manager,'project');board=ProposalBoard(self.store,self.registry)
        spec={'project':'project','repository':'fixture/project','base_sha':'a'*40,
              'description':'Synthetic batch window','checks':['fixture-check'],'kind':'development',
              'allowed_paths':['src/'],'dependencies':[],'qa_contract_ref':'fixture/qa',
              'qa_contract_digest':'b'*64,'priority':'P2','risk_rationale':'Synthetic fixture'}
        proposal=board.propose(self.manager,'window-proposal',spec)
        old=board.create_batch(self.manager,'window-old',[proposal['id']])
        pending_packet=Portfolio(self.core,board=board).decision_packet(self.manager,['project'])
        business=next(d for d in pending_packet['owner_required'] if d['kind']=='business_batch')
        self.assertEqual(business['cost_estimate_state'],'not_measured')
        self.assertFalse(business['additional_spend_authorized'])
        for i in range(20):
            batch=board.create_batch(self.manager,'window-'+str(i),[proposal['id']])
            with self.store._transaction() as c:
                c.execute('UPDATE manager_proposal_batches SET approved_by=? WHERE id=?',(token_hash(self.owner),batch['id']))
        service=Portfolio(self.core,board=board);report=service.report(self.manager,['project'])
        self.assertTrue(report['batch_window']['partial'])
        self.assertEqual(report['batch_window']['candidate_counts']['project'],21)
        self.assertNotIn(old['id'],[b['id'] for b in report['batches']])
        packet=service.decision_packet(self.manager,['project'])
        self.assertFalse(packet['summary_complete'])
        self.assertEqual([d for d in packet['owner_required'] if d['kind']=='business_batch'],[])
        self.assertIn('오래된 승인 대기 배치가 더 있을 수 있습니다',render_dashboard(report))

    def test_task_window_marks_partial_instead_of_claiming_all_tasks(self):
        for _ in range(22):
            task=self.task();self.core.start(self.manager,task['id'],'dev','start-'+str(self.n))
        result=Portfolio(self.core).report(self.manager,['project'])
        self.assertTrue(result['task_window']['partial']);self.assertTrue(result['projects'][0]['tasks_partial'])
        self.assertEqual(len(result['projects'][0]['tasks']),20)


if __name__=='__main__':unittest.main()
