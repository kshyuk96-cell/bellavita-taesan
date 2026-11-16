import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_continuous_host as host_fixtures
from manager_bridge.project_link_plan import ProjectLinkPlan
from manager_bridge.subscription_broker import SubscriptionBroker,SubscriptionTarget
from manager_bridge.source_watch import SourceWatch
from manager_bridge.store import Conflict,PermissionDenied,token_hash


class ProjectLinkPlanTests(unittest.TestCase):
    def setUp(self):
        self.fx=host_fixtures.HostTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.store,self.registry,self.core,self.host=self.fx.store,self.fx.registry,self.fx.core,self.fx.host
        self.owner,self.manager=self.fx.owner,self.fx.manager
        homes=[self.fx.root/name for name in ('canonical-profile','dev-profile','review-profile')]
        for home in homes:home.mkdir(mode=0o700)
        def auth(home,version):
            value={'auth_mode':'chatgpt','OPENAI_API_KEY':None,'tokens':{'access_token':'synthetic-access-'+version,'refresh_token':'synthetic-refresh-'+version,'id_token':'synthetic-id-'+version,'account_id':'synthetic-account'}}
            path=home/'auth.json';path.write_text(json.dumps(value));path.chmod(0o600)
        for home in homes:auth(home,'first')
        def refresh(home):auth(home,'second');return {'state':'authenticated','account_type':'chatgpt'}
        self.broker=SubscriptionBroker(self.core,'fixture-broker',homes[0],[SubscriptionTarget('project','dev','developer',homes[1]),SubscriptionTarget('project','reviewer','reviewer',homes[2])],refresh_callback=refresh)
        self.broker.refresh_and_distribute(self.owner,0,'ready')
        self.entry={'project':'project','repository':'fixture/project','source':str(self.fx.source),'candidates':str(self.fx.candidates),'runtime':str(self.fx.runtime),
                    'developer_id':'dev','reviewer_id':'reviewer','developer_token_file':str(self.fx.devfile),'reviewer_token_file':str(self.fx.reviewfile),
                    'checks':{'fixture-check':['/bin/true']},'developer':{'binary':'/usr/bin/codex','codex_home':str(homes[1])},
                    'reviewer':{'binary':'/usr/bin/codex','codex_home':str(homes[2])},'session_id':host_fixtures.SESSION}
        self.service=ProjectLinkPlan(self.core,self.host,brokers=[self.broker],templates={'project':self.entry})
        SourceWatch(self.store,self.registry);cfg=self.registry.get(self.owner,'project')
        with self.store._transaction() as c:
            c.execute('INSERT INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',('project',cfg['fingerprint'],self.fx.base,'v1','fixture/source',1000,1))
            c.execute('INSERT INTO manager_source_observation_status VALUES (?,?,?,?)',('project',cfg['fingerprint'],'unchanged',1000))

    def plan(self,key='link-plan',**changes):
        inv=self.service.inventory(self.owner,['project'])
        values={'owner':self.owner,'request_key':key,'projects':['project'],'expected_bindings':inv['bindings'],
                'role_mappings':{'project':{'taesan':'taesan','developer':'dev','reviewer':'reviewer'}},
                'expected_host_map_digest':inv['host_map_digest'],'broker_refs':inv['broker_refs']}
        values.update(changes)
        return self.service.plan(**values)

    def test_candidate_is_exact_idempotent_reference_only_and_never_reads_auth(self):
        before=(self.fx.devfile.read_bytes(),self.fx.reviewfile.read_bytes(),set(self.host.projects))
        with patch('manager_bridge.subscription_broker._read',side_effect=AssertionError('no auth read')):
            result=self.plan();self.assertEqual(result,self.plan())
        candidate=result['plan']['candidates'][0]
        self.assertEqual(candidate['state'],'candidate');self.assertTrue(candidate['generated_host_settings']['requires_trusted_private_materialization'])
        raw=json.dumps(result)
        for forbidden in ('synthetic-access','synthetic-refresh','synthetic-account',str(self.broker.home),str(self.fx.devfile),self.fx.dev):self.assertNotIn(forbidden,raw)
        self.assertFalse(result['application_performed']);self.assertFalse(result['plan']['activation_performed'])
        self.assertEqual(before,(self.fx.devfile.read_bytes(),self.fx.reviewfile.read_bytes(),set(self.host.projects)))

    def test_owner_scope_cas_generation_and_known_bearer_fail_closed(self):
        with self.assertRaises(PermissionDenied):self.plan(owner=self.manager)
        inv=self.service.inventory(self.owner,['project']);wrong=copy.deepcopy(inv['broker_refs']);wrong['project'][0]['generation']=0
        with self.assertRaises(Conflict):self.plan(broker_refs=wrong)
        with self.assertRaises(Conflict):self.plan(expected_host_map_digest='b'*64)
        with self.assertRaises(ValueError):self.plan(key=self.fx.dev)
        with self.assertRaises(ValueError):self.plan(expected_bindings={'project':{'revision':True,'fingerprint':inv['bindings']['project']['fingerprint']}})

    def test_current_task_or_live_lease_prevents_generated_settings(self):
        self.fx.task()
        item=self.plan()['plan']['candidates'][0]
        self.assertEqual(item['state'],'blocked');self.assertIsNone(item['generated_host_settings'])
        self.assertIn('current_task_or_live_lease',item['blocks'])

    def test_uncertain_host_or_broker_blocks_and_invalidates_old_candidate(self):
        previous=self.plan()
        with self.store._transaction() as c:c.execute("UPDATE subscription_broker_state SET state='uncertain' WHERE ref='fixture-broker'")
        current=self.service.get_plan(self.owner,previous['id'])
        self.assertEqual(current['state'],'invalidated');self.assertFalse(current['current_candidate_generation_allowed'])
        with self.assertRaises(Conflict):self.plan()
        blocked=self.plan('new-key')['plan']['candidates'][0]
        self.assertIsNone(blocked['generated_host_settings'])

    def test_role_revocation_and_config_revision_change_invalidate_exact_candidate(self):
        previous=self.plan();self.store.revoke_token(self.owner,token_hash(self.fx.reviewer))
        current=self.service.get_plan(self.owner,previous['id']);self.assertEqual(current['state'],'invalidated')
        self.assertIn('role_reviewer_missing_or_changed',current['current_blocks']['project'])
        config=self.registry.get(self.owner,'project')
        self.registry.register(self.owner,'project',config['config'],expected_revision=config['revision'])
        with self.assertRaises(Conflict):self.plan()

    def test_template_unknown_secret_field_and_shared_profile_are_rejected(self):
        bad=copy.deepcopy(self.entry);bad['developer']['refresh_token']='must never serialize'
        with self.assertRaises(ValueError):ProjectLinkPlan(self.core,self.host,templates={'project':bad})
        bad=copy.deepcopy(self.entry);bad['reviewer']['codex_home']=bad['developer']['codex_home']
        with self.assertRaises(ValueError):ProjectLinkPlan(self.core,self.host,templates={'project':bad})
        bad=copy.deepcopy(self.entry);bad['checks']['fixture-check'].append(self.fx.reviewer)
        with self.assertRaises(ValueError):ProjectLinkPlan(self.core,self.host,templates={'project':bad})

    def test_host_map_change_and_ongoing_action_are_reflected_in_conflicts(self):
        result=self.plan()
        self.host.projects={}
        current=self.service.get_plan(self.owner,result['id'])
        self.assertEqual(current['state'],'invalidated');self.assertIn('existing_host_session_reference_required',current['current_blocks']['project'])
        with self.store._transaction() as c:
            c.execute("INSERT INTO continuous_actions (action_key,project,state) VALUES ('fixture-uncertain','project','uncertain')")
        self.assertIn('continuous_execution_running_or_uncertain',self.plan('blocked')['plan']['candidates'][0]['blocks'])

    def test_private_canonical_profile_overlap_blocks_without_reading_credentials(self):
        entry=copy.deepcopy(self.entry);entry['source']=str(self.broker.home)
        self.service=ProjectLinkPlan(self.core,self.host,brokers=[self.broker],templates={'project':entry})
        with patch('manager_bridge.subscription_broker._read',side_effect=AssertionError('no credential reads')):
            item=self.plan()['plan']['candidates'][0]
        self.assertIn('candidate_overlaps_private_state_or_subscription_profile',item['blocks'])
        self.assertIsNone(item['generated_host_settings'])

    def test_other_candidate_template_resource_collision_is_explicit(self):
        other=copy.deepcopy(self.entry);other.update(project='another',developer_id='other-dev',reviewer_id='other-review')
        self.service=ProjectLinkPlan(self.core,self.host,brokers=[self.broker],templates={'project':self.entry,'another':other})
        item=self.plan()['plan']['candidates'][0]
        self.assertIn('candidate_overlaps_other_trusted_template',item['blocks'])
        self.assertIsNone(item['generated_host_settings'])

    def test_registry_drift_inside_candidate_build_rolls_back_plan(self):
        original=self.core._member
        def changed(c,project,member_id,role,config):
            member=original(c,project,member_id,role,config)
            if role=='reviewer':
                c.execute("UPDATE maintenance_projects SET revision=revision+1,fingerprint=? WHERE project='project'",('b'*64,))
            return member
        with patch.object(self.core,'_member',side_effect=changed):
            with self.assertRaises(Conflict):self.plan()
        self.assertEqual(self.registry.get(self.owner,'project')['revision'],1)
