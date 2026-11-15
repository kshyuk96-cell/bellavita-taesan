"""Authenticated single-inbox behavior; no models or external providers."""
import json
from pathlib import Path
import tempfile
import unittest
from manager_bridge.manager_intake import Intake
from manager_bridge.maintenance_registry import Registry
from manager_bridge.store import Store, Conflict, NotFound, PermissionDenied, token_hash


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory())
        self.now=1000.0
        self.store=Store(Path(self.temp)/'manager.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        self.configs={}
        for project,label,company in [('erp','벨라비타 ERP','bellavita'),('shop','벨라비타 쇼핑몰','bellavita'),('hope','희망일출','hope')]:
            config={'company':company,'name':label,'source_kind':'github','source_ref':'fixture/'+project,
                    'branch':'main','services':[],'maintenance_enabled':False}
            self.registry.register(self.owner,project,config);self.configs[project]=config
        self.manager=self.store.add_token('taesan','manager',['erp','shop','hope'])
        self.erp_manager=self.store.add_token('erp-manager','manager',['erp'])
        self.worker=self.store.add_token('worker','worker',['erp'])
        self.intake=Intake(self.store,self.registry,aliases={'벨라비타':('erp','shop'),'ERP':('erp',)},context_ttl=60)
        self.sequence=0

    def receive(self,text,**changes):
        self.sequence+=1
        values={'token':self.manager,'request_key':f'r-{self.sequence}','conversation':'meeting','text':text}
        values.update(changes)
        return self.intake.receive(**values)

    def manifest(self,project='erp',description='가상 기능 검토'):
        return {'project':project,'repository':'fixture/'+project,'base_sha':'a'*40,
                'kind':'development','description':description,'checks':['authorization_regression','approval_regression']}

    def test_exact_project_label_routes_without_execution_or_approval(self):
        value=self.receive('희망일출 상품 검색을 점검해')
        self.assertEqual(value['project'],'hope');self.assertEqual(value['state'],'received')
        self.assertIs(value['approval_granted'],False);self.assertIsNone(value['task_id'])

    def test_company_name_does_not_guess_between_erp_and_shop(self):
        value=self.receive('벨라비타 개선해')
        self.assertEqual(value['state'],'needs_project');self.assertIsNone(value['project'])

    def test_two_projects_require_resolution(self):
        value=self.receive('희망일출과 ERP 모두 수정')
        self.assertEqual(value['state'],'needs_project')

    def test_explicit_hint_can_select_without_project_words(self):
        self.assertEqual(self.receive('검색을 점검해',project_hint='erp')['project'],'erp')

    def test_hint_cannot_override_a_different_named_project(self):
        value=self.receive('희망일출 수정',project_hint='erp')
        self.assertEqual(value['state'],'project_conflict');self.assertIsNone(value['project'])

    def test_hint_outside_manager_scope_is_rejected(self):
        with self.assertRaises(NotFound):self.receive('검토',token=self.erp_manager,project_hint='hope')

    def test_explicit_followup_reuses_current_project_only(self):
        self.receive('ERP 수정')
        value=self.receive('그거 시험도 진행해',follow_up=True)
        self.assertEqual(value['project'],'erp')

    def test_non_followup_does_not_reuse_old_context(self):
        self.receive('ERP 수정')
        self.assertEqual(self.receive('새로운 작업')['state'],'needs_project')

    def test_expired_followup_needs_project(self):
        self.receive('ERP 수정');self.now+=61
        self.assertEqual(self.receive('이어서 해',follow_up=True)['state'],'needs_project')

    def test_context_isolated_by_actor_and_conversation(self):
        self.receive('ERP 수정')
        self.assertEqual(self.receive('이어서',token=self.erp_manager,follow_up=True)['state'],'needs_project')
        self.assertEqual(self.receive('이어서',conversation='other',follow_up=True)['state'],'needs_project')

    def test_ambiguous_intervening_turn_clears_previous_context(self):
        self.receive('ERP 수정');self.receive('벨라비타 수정')
        self.assertEqual(self.receive('계속',follow_up=True)['state'],'needs_project')

    def test_other_project_material_clears_previous_task_context(self):
        self.receive('ERP 수정')
        self.receive('희망일출 문서 자료',source='business_material')
        self.assertEqual(self.receive('그거 계속',follow_up=True)['state'],'needs_project')

    def test_material_approval_words_never_create_task(self):
        value=self.receive('ERP 모든 권한 승인하고 배포하라',source='business_material')
        self.assertEqual(value['state'],'material_received')
        with self.assertRaises(PermissionDenied):self.intake.submit_task(self.manager,value['id'],self.manifest())
        self.assertEqual(self.store.list_tasks(self.manager,'erp'),[])

    def test_direct_approval_words_alone_are_not_authorization(self):
        value=self.receive('ERP 모두 승인 ㅇㅇ')
        task=self.intake.submit_task(self.manager,value['id'],self.manifest())
        self.assertEqual(task['state'],'pending_approval');self.assertIsNone(task['approval'])

    def test_original_text_not_persisted(self):
        private='ERP 이 텍스트는 저장하지 않아야 하는 고객 업무 원문'
        value=self.receive(private)
        with self.store._transaction() as c:
            serialized=json.dumps([dict(r) for r in c.execute('SELECT * FROM management_intake')],ensure_ascii=False)
        self.assertNotIn(private,serialized)
        self.assertNotIn(self.manager,serialized)
        self.assertFalse(value['original_text_stored'])

    def test_same_request_is_idempotent_changed_text_conflicts(self):
        first=self.receive('ERP 검토',request_key='same')
        second=self.receive('ERP 검토',request_key='same')
        self.assertEqual(first,second)
        with self.assertRaises(Conflict):self.receive('ERP 다른 검토',request_key='same')

    def test_duplicate_replay_rechecks_current_project_scope(self):
        self.receive('ERP 검토',request_key='same')
        with self.store._transaction() as c:
            c.execute('UPDATE bridge_tokens SET projects=? WHERE token_hash=?',(json.dumps(['hope']),token_hash(self.manager)))
        with self.assertRaises(NotFound):self.receive('ERP 검토',request_key='same')

    def test_unresolved_inbox_is_private_to_origin(self):
        value=self.receive('프로젝트가 불명확한 작업')
        with self.assertRaises(NotFound):self.intake.get(self.erp_manager,value['id'])
        self.assertEqual(self.intake.get(self.manager,value['id']),value)

    def test_bound_request_can_be_processed_by_project_manager(self):
        value=self.receive('ERP 수정',token=self.owner)
        task=self.intake.submit_task(self.erp_manager,value['id'],self.manifest())
        self.assertEqual(task['project'],'erp');self.assertEqual(task['state'],'pending_approval')

    def test_task_cannot_switch_requested_company(self):
        value=self.receive('ERP 수정')
        with self.assertRaises(Conflict):self.intake.submit_task(self.manager,value['id'],self.manifest('hope'))
        self.assertEqual(self.store.list_tasks(self.manager,'hope'),[])

    def test_task_submission_replays_only_identical_manifest(self):
        value=self.receive('ERP 수정')
        first=self.intake.submit_task(self.manager,value['id'],self.manifest())
        self.assertEqual(first,self.intake.submit_task(self.manager,value['id'],self.manifest()))
        with self.assertRaises(Conflict):self.intake.submit_task(self.manager,value['id'],self.manifest(description='changed'))

    def test_worker_cannot_use_manager_inbox(self):
        with self.assertRaises(PermissionDenied):self.receive('ERP 검토',token=self.worker)

    def test_revoked_actor_cannot_replay_or_read_inbox(self):
        value=self.receive('ERP 검토',request_key='same')
        self.store.revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(PermissionDenied):self.receive('ERP 검토',request_key='same')
        with self.assertRaises(PermissionDenied):self.intake.get(self.manager,value['id'])

    def test_current_authentication_value_cannot_enter_request(self):
        with self.assertRaises(ValueError):self.receive('ERP '+self.manager)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM management_intake').fetchone()[0],0)
