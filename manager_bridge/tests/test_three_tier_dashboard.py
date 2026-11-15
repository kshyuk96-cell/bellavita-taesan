from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from manager_bridge.maintenance_registry import Registry
from manager_bridge.store import Store, Conflict, PermissionDenied, NotFound, token_hash
from manager_bridge.three_tier_dashboard import build_summary, render_dashboard


class FakeCore:
    def __init__(self,store,registry):
        self.store,self.registry=store,registry
        self.callback=None;self.calls=[];self.entries={}
    def overview(self,token,projects):
        self.calls.append(tuple(projects))
        if self.callback:self.callback()
        return {'projects':[deepcopy(self.entries[p]) for p in projects],
                'operational_change':False,'existing_chat_control_verified':False}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory())
        self.now=1000
        self.store=Store(Path(self.tmp)/'state.sqlite',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        self.config={'company':'company-a','name':'Project A','source_kind':'github','source_ref':'fixture/a',
                     'branch':'main','services':[],'maintenance_enabled':False}
        self.registry.register(self.owner,'a',self.config)
        self.registry.register(self.owner,'b',{**self.config,'company':'company-b','name':'Project B','source_ref':'fixture/b'})
        self.manager=self.store.add_token('taesan','manager',['a'])
        self.multi=self.store.add_token('taesan-multi','manager',['a','b'])
        self.worker=self.store.add_token('developer','worker',['a'])
        self.core=FakeCore(self.store,self.registry)
        for p in ('a','b'):
            self.core.entries[p]={'project':p,'config_fingerprint':self.registry.get(self.owner,p)['fingerprint'],
                 'capacity':{'configured':True,'max_active':2,'max_review':1},
                 'members':[{'member_id':'dev-a','role':'developer','subject':'private-subject','agent_id':'agent-a','connection_verified':False}],
                 'tasks':[]}

    def task(self,**changes):
        value={'task_id':'t-1','state':'review_passed','version':1,'manifest_digest':'a'*64,
               'developer_id':'dev-a','reviewer_id':'review-a','rework_count':0,'candidate_digest':'b'*64,
               'source':{'head_sha':'c'*40,'base_sha':'d'*40,'artifact_digest':'e'*64},
               'checks':[{'name':'authorization','exit_code':0}],
               'review_state':'passed','technical_accepted':False,'operational_approved':False,
               'connection_verified':False,'requires_owner_decision':False,'blocked_reason':None}
        value.update(changes);return value

    def summary(self):return build_summary(self.core,self.manager,['a'])

    def test_technical_acceptance_is_never_operational_or_existing_chat_control(self):
        self.core.entries['a']['tasks']=[self.task(state='accepted',technical_accepted=True)]
        result=self.summary();task=result['projects'][0]['tasks'][0]
        self.assertTrue(task['technical_accepted']);self.assertFalse(task['operational_approved'])
        self.assertFalse(result['deployment_verified']);self.assertFalse(result['existing_chat_control_verified'])
        self.assertFalse(result['background_management_verified']);self.assertFalse(result['usage_or_cost_measured'])
        self.assertEqual(result['owner_decisions'],[])

    def test_review_record_alone_and_forged_boolean_are_not_accepted(self):
        for changes in ({'state':'reviewed','technical_accepted':True},
                        {'state':'accepted','technical_accepted':True,'review_state':'not_measured'},
                        {'state':'accepted','technical_accepted':True,'checks':[{'passed':True}]},
                        {'state':'accepted','technical_accepted':True,'source':None}):
            self.core.entries['a']['tasks']=[self.task(**changes)]
            self.assertFalse(self.summary()['projects'][0]['tasks'][0]['technical_accepted'])

    def test_boolean_exit_code_is_unmeasured(self):
        self.core.entries['a']['tasks']=[self.task(state='accepted',technical_accepted=True,checks=[{'exit_code':False}])]
        result=self.summary();task=result['projects'][0]['tasks'][0]
        self.assertEqual(task['checks']['unmeasured'],1);self.assertFalse(task['technical_accepted'])
        self.assertEqual(result['manager_attention_count'],1)

    def test_owner_packet_contains_only_explicit_owner_decisions(self):
        self.core.entries['a']['tasks']=[self.task(state='review_failed',review_state='failed',checks=[{'exit_code':1}]),
             self.task(task_id='t-2',state='needs_verification',requires_owner_decision=True,blocked_reason='budget_limit')]
        result=self.summary()
        self.assertEqual(result['owner_decision_count'],1);self.assertEqual(result['owner_decisions'][0]['task_id'],'t-2')
        self.assertEqual(result['manager_attention_count'],1)
        self.assertEqual(result['owner_decisions'][0]['message'],'추가 비용 결정 필요')

    def test_no_credentials_prompt_logs_subjects_or_arbitrary_provider_fields_projected(self):
        task=self.task(prompt='private customer request',stdout='private stderr',secret='sk-private',report={'raw':'private'})
        self.core.entries['a']['tasks']=[task]
        encoded=str(self.summary())
        for private in ('private customer request','private stderr','sk-private','private-subject'):
            self.assertNotIn(private,encoded)

    def test_current_project_authorization_precedes_core_collection(self):
        with self.assertRaises(NotFound):build_summary(self.core,self.manager,['a','b'])
        self.assertEqual(self.core.calls,[])
        with self.assertRaises(PermissionDenied):build_summary(self.core,self.worker,['a'])
        self.assertEqual(self.core.calls,[])

    def test_revoked_access_during_collection_returns_no_snapshot(self):
        self.core.callback=lambda:self.store.revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(PermissionDenied):self.summary()

    def test_configuration_change_during_collection_aborts(self):
        self.core.callback=lambda:self.registry.register(self.owner,'a',{**self.config,'name':'Changed'},expected_revision=1)
        with self.assertRaises(Conflict):self.summary()

    def test_configured_limits_and_observed_review_backlog_are_distinct(self):
        self.core.entries['a']['tasks']=[self.task(state='candidate_ready')]
        result=self.summary()['projects'][0]
        self.assertTrue(result['capacity']['review_backlog_observed'])
        self.core.entries['a']['capacity']={'configured':False,'max_active':2,'max_review':1}
        result=self.summary()['projects'][0]
        self.assertFalse(result['capacity']['configured']);self.assertFalse(result['capacity']['review_backlog_observed'])
        self.assertIsNone(result['capacity']['max_review'])

    def test_registration_is_not_connection(self):
        project=self.summary()['projects'][0]
        self.assertEqual(project['registered_members'],1);self.assertEqual(project['connection_verified_members'],0)
        self.assertEqual(project['roles']['developer'],1)

    def test_multiple_companies_remain_separate(self):
        self.core.entries['a']['tasks']=[self.task()]
        self.core.entries['b']['tasks']=[self.task(task_id='b-task')]
        result=build_summary(self.core,self.multi,['a','b'])
        self.assertEqual([p['company'] for p in result['projects']],['company-a','company-b'])
        self.assertEqual(result['projects'][0]['tasks'][0]['project'],'a')
        self.assertEqual(result['projects'][1]['tasks'][0]['project'],'b')

    def test_truncated_view_does_not_claim_all_tasks(self):
        self.core.entries['a']['tasks']=[self.task(task_id=f't-{i}') for i in range(201)]
        result=self.summary();project=result['projects'][0]
        self.assertEqual(project['displayed_tasks'],200);self.assertTrue(project['tasks_truncated'])
        self.assertEqual(sum(result['counts_displayed'].values()),200)
        self.assertIn('일부 작업만 표시',render_dashboard(result))

    def test_html_escapes_display_values_has_no_script_or_approval_action(self):
        self.registry.register(self.owner,'a',{**self.config,'name':'<img src=x onerror=alert(1)>'},expected_revision=1)
        self.core.entries['a']['config_fingerprint']=self.registry.get(self.owner,'a')['fingerprint']
        html=render_dashboard(self.summary())
        self.assertNotIn('<img',html);self.assertIn('&lt;img',html)
        self.assertNotIn('<script',html);self.assertNotIn('<button',html)
        self.assertIn('lang="ko"',html);self.assertIn('기술 인수는 운영 배포 승인이 아닙니다',html)

    def test_html_declines_unvalidated_projection(self):
        with self.assertRaises(ValueError):render_dashboard({'projects':[]})
        with self.assertRaises(ValueError):render_dashboard({'snapshot_scope':'authorized_registered_hierarchy','operational_change':True,'projects':[]})

    def test_invalid_or_duplicate_projects_do_not_collect(self):
        for projects in ([],['a','a'],[['a']],('a',),['a']*101):
            with self.assertRaises(ValueError):build_summary(self.core,self.manager,projects)
        self.assertEqual(self.core.calls,[])

    def test_registered_token_in_safe_looking_identifier_is_rejected(self):
        self.core.entries['a']['tasks']=[self.task(task_id=self.manager)]
        with self.assertRaises(ValueError):self.summary()

    def test_actual_core_empty_overview_contract_is_read_only_and_unconnected(self):
        from manager_bridge.three_tier import ThreeTier
        core=ThreeTier(self.store,self.registry)
        result=build_summary(core,self.manager,['a'])
        self.assertEqual(result['projects'][0]['registered_members'],0)
        self.assertFalse(result['projects'][0]['capacity']['configured'])
        self.assertEqual(result['owner_decisions'],[])
        self.assertFalse(result['existing_chat_control_verified'])
        self.assertIn('현재 표시할 작업이 없습니다',render_dashboard(result))
