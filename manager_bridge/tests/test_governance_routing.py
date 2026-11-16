"""Routing diagnostics use real durable core records; no native/model calls."""
import json
import unittest
from manager_bridge.tests import test_three_tier as hierarchy_fixtures
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.governance_routing import GovernanceRouting
from manager_bridge.source_watch import SourceWatch, HeadProbe, SourceHead
from manager_bridge.store import PermissionDenied, token_hash, digest
from manager_bridge.tests import test_qa_admission as qa_fixtures


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.fx = hierarchy_fixtures.ThreeTierTests(); self.fx.setUp(); self.addCleanup(self.fx.doCleanups)
        self.controller = ContinuousManager(self.fx.core)
        self.routing = GovernanceRouting(self.fx.core, self.controller)

    def project(self):
        return self.routing.plan(self.fx.manager, ['project'])['projects'][0]

    def ready(self):
        qa = qa_fixtures.QAAdmissionTests(); qa.fixture()
        self.fx.core.governance_gate.providers['project'] = lambda candidate, subject: None
        self.fx.core.governance_gate.enroll(self.fx.owner,['project'])
        self.fx.core.governance_gate.register_policy(self.fx.owner, 'project', contract_version='1', expected_contract=qa.expected_contract, required_coverage=['money'], provider_identity='synthetic')
        watch = SourceWatch(self.fx.store, self.fx.registry, {'project': HeadProbe('github','fixture/project','main', lambda p: SourceHead(p,'github','fixture/project','main','a'*40,'fixture','fixture/head',self.fx.now))})
        watch.inspect(self.fx.owner,'project')

    def test_missing_revoked_and_distinct_members_sanitized(self):
        value = self.project()
        self.assertEqual(['dev'], value['eligible_roles']['developer'])
        self.assertEqual(['reviewer'], value['eligible_roles']['reviewer'])
        self.assertNotIn('credential_hash', json.dumps(value))
        self.assertNotIn('subject',json.dumps(value))
        self.fx.store.revoke_token(self.fx.owner, token_hash(self.fx.reviewer))
        self.assertIn('missing_reviewer', self.project()['reasons'])
        with self.fx.store._transaction() as c:
            c.execute("DELETE FROM hierarchy_members WHERE project='project' AND role='developer'")
        self.assertIn('missing_developer', self.project()['reasons'])
        with self.assertRaises(PermissionDenied): self.routing.plan(self.fx.dev,['project'])

    def test_durable_qa_source_and_staleness(self):
        self.assertFalse(self.project()['ready'])
        self.ready()
        self.assertTrue(self.project()['ready'])
        self.fx.now += 301
        self.assertEqual('stale',self.project()['source']['state'])
        self.assertFalse(self.project()['ready'])
        self.fx.registry.register(self.fx.owner,'project',{**self.fx.config,'branch':'changed','source_ref':'fixture/project'},expected_revision=1)
        value = self.project()
        self.assertEqual('configuration_changed',value['source']['state'])
        self.assertEqual('invalid_binding',value['qa']['state'])
        self.assertTrue(all(m['state']=='binding_changed' for m in value['members']))

    def test_latest_negative_observation_overrides_old_good_head(self):
        self.ready()
        self.assertTrue(self.project()['ready'])
        watch = SourceWatch(self.fx.store,self.fx.registry,{'project':HeadProbe('github','fixture/project','main',lambda p: (_ for _ in ()).throw(RuntimeError('offline')))})
        self.assertEqual('unavailable',watch.inspect(self.fx.owner,'project')['state'])
        value=self.project()
        self.assertEqual('latest_observation_unavailable',value['source']['state'])
        self.assertFalse(value['ready'])
        self.assertEqual('a'*40,value['source']['head_sha'])

    def test_future_head_and_bounded_durable_recovery(self):
        self.ready()
        with self.fx.store._transaction() as c:
            c.execute('UPDATE manager_source_heads SET observed_at=? WHERE project=?',(self.fx.now+1,'project'))
            for index in range(105):
                c.execute("INSERT INTO continuous_actions VALUES (?,?,?,?,?,?,?,'uncertain',1,?,NULL,'lost',?,?)",(format(index,'064x'),'b'*32,'project','start','{}',token_hash(self.fx.manager),1,self.fx.now,self.fx.now,self.fx.now))
        value=self.routing.plan(self.fx.owner,['project'])
        self.assertFalse(value['projects'][0]['source']['fresh'])
        self.assertEqual(100,len(value['recovery']['actions']))
        self.assertEqual(105,value['recovery']['action_count'])
        self.assertTrue(value['recovery']['truncated'])
        self.assertTrue(value['recovery']['reconciliation_required'])

    def test_capacity_and_backlog_observed_without_dispatch(self):
        self.ready()
        tasks=[self.fx.task() for _ in range(2)]
        for task in tasks:
            with self.fx.store._transaction() as c:
                c.execute("UPDATE bridge_tasks SET state='running' WHERE id=?",(task['id'],))
        value=self.project()
        self.assertEqual(2,value['backlog']['running'])
        self.assertIn('capacity_unavailable',value['reasons'])
        self.assertEqual(0,self.fx.host_calls)

    def test_restart_uncertainty_and_expired_leader_require_reconciliation(self):
        with self.fx.store._transaction() as c:
            c.execute('INSERT INTO continuous_leader VALUES (1,?,?,?,?,?)',('old',token_hash(self.fx.manager),digest(['project']),1,self.fx.now-1))
            c.execute("INSERT INTO continuous_actions VALUES (?,?,?,?,?,?,?,'uncertain',1,?,NULL,'lost',?,?)",('a'*64,'b'*32,'project','start','{}',token_hash(self.fx.manager),1,self.fx.now,self.fx.now,self.fx.now))
        restarted=GovernanceRouting(self.fx.core,ContinuousManager(self.fx.core))
        value=restarted.plan(self.fx.owner,['project'])
        self.assertEqual('expired',value['recovery']['leader_state'])
        self.assertTrue(value['recovery']['reconciliation_required'])
        self.assertFalse(value['recovery']['automatic_replay'])
        self.assertEqual('unmeasured',value['persistent_host_health'])
        with self.fx.store._transaction() as c:
            self.assertEqual('uncertain',c.execute('SELECT state FROM continuous_actions').fetchone()[0])

    def test_large_registration_lists_are_bounded_and_partial(self):
        from manager_bridge.fleet import Fleet
        Fleet(self.fx.store,self.fx.registry)
        self.ready()
        with self.fx.store._transaction() as c:
            for index in range(105):
                c.execute("INSERT INTO hierarchy_members SELECT project,?,credential_hash,subject,role,agent_id,config_fingerprint FROM hierarchy_members WHERE project='project' AND member_id='dev'",('extra-'+str(index),))
                c.execute('INSERT INTO fleet_hosts VALUES (?,?,?,?,?)',('project','host-'+str(index),'f'*64,token_hash(self.fx.owner),self.fx.now))
                c.execute('INSERT INTO fleet_templates VALUES (?,?,?,?,?,?,?,?)',('project','template-'+str(index),'host-'+str(index),'f'*64,token_hash(self.fx.owner),2,self.fx.now+300,self.fx.now))
        value=self.project()
        self.assertEqual(100,len(value['members']))
        self.assertEqual(108,value['member_total'])
        self.assertTrue(value['members_truncated'])
        self.assertEqual(100,len(value['fleet_hosts']))
        self.assertEqual(105,value['fleet_host_total'])
        self.assertEqual(100,len(value['fleet_templates']))
        self.assertTrue(value['fleet_templates_truncated'])
        self.assertIn('routing_partial',value['reasons'])
        self.assertFalse(value['ready'])

    def test_missing_limits_and_project_scope(self):
        with self.fx.store._transaction() as c: c.execute("DELETE FROM bridge_capacity_limits WHERE scope='project'")
        self.assertEqual('not_configured',self.project()['capacity']['state'])
        for value in ([],['project','project'],'project'):
            with self.assertRaises(ValueError):self.routing.plan(self.fx.manager,value)
        self.assertEqual('not_connected',GovernanceRouting(self.fx.core).plan(self.fx.owner,['project'])['recovery']['state'])
