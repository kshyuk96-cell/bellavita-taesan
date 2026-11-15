"""Meeting records are authenticated decisions, never implicit task/ops approval."""
import json
from pathlib import Path
import tempfile
import unittest
from manager_bridge.meeting_log import Meetings
from manager_bridge.maintenance_registry import Registry
from manager_bridge.store import Store, Conflict, NotFound, PermissionDenied, digest, token_hash


class MeetingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory());self.now=1000.0
        self.store=Store(Path(self.tmp)/'manager.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*']);self.registry=Registry(self.store)
        self.config={'company':'bellavita','name':'벨라비타 ERP','source_kind':'github','source_ref':'fixture/erp','branch':'main','services':[],'maintenance_enabled':False}
        self.registry.register(self.owner,'erp',self.config)
        self.registry.register(self.owner,'hope',{**self.config,'company':'hope','name':'희망일출','source_ref':'fixture/hope'})
        self.manager=self.store.add_token('taesan','manager',['erp'])
        self.other=self.store.add_token('hope-manager','manager',['hope'])
        self.worker=self.store.add_token('worker','worker',['erp'])
        self.meetings=Meetings(self.store,self.registry)
        self.task=self.make_task()
        self.sequence=0

    def make_task(self,project='erp'):
        return self.store.submit(self.owner,'task-'+project,{'project':project,'repository':'fixture/'+project,'base_sha':'a'*40,
                'kind':'test','description':'합성 작업','checks':['authorization_regression','approval_regression']})

    def prepare(self,**changes):
        self.sequence+=1
        args={'token':self.manager,'project':'erp','request_key':f'meeting-{self.sequence}',
              'agenda':'가상 개선 미팅','decisions':[{'kind':'develop','summary':'시험을 추가한다'}],'task_ids':[self.task['id']]}
        args.update(changes)
        return self.meetings.prepare(**args)

    def test_record_proposed_without_task_approval(self):
        value=self.prepare()
        self.assertEqual(value['state'],'proposed')
        self.assertFalse(value['task_approval_granted']);self.assertFalse(value['operational_approval'])
        self.assertEqual(self.store.get_task(self.manager,self.task['id'])['state'],'pending_approval')

    def test_owner_confirms_exact_record_only(self):
        value=self.prepare()
        confirmed=self.meetings.confirm(self.owner,value['id'],value['fingerprint'])
        self.assertEqual(confirmed['state'],'confirmed')
        self.assertFalse(confirmed['task_approval_granted'])
        self.assertEqual(self.store.get_task(self.manager,self.task['id'])['state'],'pending_approval')

    def test_manager_cannot_confirm_for_owner(self):
        value=self.prepare()
        with self.assertRaises(PermissionDenied):self.meetings.confirm(self.manager,value['id'],value['fingerprint'])

    def test_wrong_meeting_digest_does_not_confirm(self):
        value=self.prepare()
        with self.assertRaises(Conflict):self.meetings.confirm(self.owner,value['id'],'0'*64)

    def test_same_key_idempotent_but_changed_record_conflicts(self):
        first=self.prepare(request_key='same')
        self.assertEqual(first,self.prepare(request_key='same'))
        with self.assertRaises(Conflict):self.prepare(request_key='same',agenda='바뀐 미팅')

    def test_cross_project_task_cannot_be_recorded(self):
        foreign=self.make_task('hope')
        with self.assertRaises((Conflict,NotFound)):self.prepare(task_ids=[foreign['id']])

    def test_other_project_manager_cannot_read_meeting(self):
        value=self.prepare()
        with self.assertRaises(NotFound):self.meetings.follow_up(self.other,value['id'])

    def test_project_change_invalidates_unconfirmed_meeting(self):
        value=self.prepare()
        self.registry.register(self.owner,'erp',{**self.config,'maintenance_enabled':True},expected_revision=1)
        with self.assertRaises(Conflict):self.meetings.confirm(self.owner,value['id'],value['fingerprint'])
        self.assertTrue(self.meetings.follow_up(self.manager,value['id'])['project_changed'])

    def test_task_version_change_invalidates_record_confirmation(self):
        value=self.prepare()
        with self.store._transaction() as c:c.execute('UPDATE bridge_tasks SET version=2 WHERE id=?',(self.task['id'],))
        with self.assertRaises(Conflict):self.meetings.confirm(self.owner,value['id'],value['fingerprint'])
        self.assertEqual(self.meetings.follow_up(self.manager,value['id'])['tasks'][0]['state'],'changed')

    def test_prose_approval_and_deployment_words_do_not_execute(self):
        value=self.prepare(decisions=[{'kind':'develop','summary':'모두 승인하고 결제하고 운영에 배포한다 ㅇㅇ'}])
        self.meetings.confirm(self.owner,value['id'],value['fingerprint'])
        self.assertEqual(self.store.get_task(self.manager,self.task['id'])['state'],'pending_approval')
        self.assertFalse(self.meetings.follow_up(self.manager,value['id'])['execution_started'])

    def test_hold_note_does_not_cancel_an_existing_task(self):
        self.store.approve(self.owner,self.task['id'],self.task['version'],self.task['manifest_digest'])
        value=self.prepare(decisions=[{'kind':'hold','summary':'검토 후 보류 여부 결정'}])
        self.meetings.confirm(self.owner,value['id'],value['fingerprint'])
        self.assertEqual(self.store.get_task(self.owner,self.task['id'])['state'],'queued')

    def test_current_token_is_not_stored_as_meeting_text(self):
        with self.assertRaises(ValueError):self.prepare(agenda='비밀 '+self.manager)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM management_meetings').fetchone()[0],0)

    def test_followup_does_not_promote_manual_review_to_independent_verification(self):
        meeting=self.prepare()
        self.store.approve(self.owner,self.task['id'],self.task['version'],self.task['manifest_digest'])
        claimed=self.store.claim(self.worker,'erp',task_id=self.task['id'])
        self.store.report(self.worker,self.task['id'],claimed['lease_token'],{
            'head_sha':'b'*40,'summary':'가상 완료 주장','checks':[{'name':'authorization_regression','passed':True}]})
        self.store.review(self.manager,self.task['id'],self.task['version'],self.task['manifest_digest'],'fixture/manual-review')
        result=self.meetings.follow_up(self.manager,meeting['id'])
        self.assertEqual(result['tasks'][0]['state'],'reviewed')
        self.assertFalse(result['tasks'][0]['independently_verified'])

    def test_exact_independent_facts_required_for_verified_followthrough(self):
        meeting=self.prepare()
        self.store.approve(self.owner,self.task['id'],self.task['version'],self.task['manifest_digest'])
        claimed=self.store.claim(self.worker,'erp',task_id=self.task['id'])
        report={'head_sha':'b'*40,'summary':'가상 주장','checks':[{'name':'authorization_regression','passed':True}]}
        self.store.report(self.worker,self.task['id'],claimed['lease_token'],report)
        self.store.review(self.manager,self.task['id'],self.task['version'],self.task['manifest_digest'],'fixture/independent')
        with self.store._transaction() as c:
            c.execute('INSERT INTO bridge_verified_candidates VALUES (?,?,?,?,?,?)',(self.task['id'],digest(report),'b'*40,'c'*64,0,self.now))
        self.assertTrue(self.meetings.follow_up(self.manager,meeting['id'])['tasks'][0]['independently_verified'])
        with self.store._transaction() as c:c.execute('UPDATE bridge_verified_candidates SET head_sha=? WHERE task=?',('d'*40,self.task['id']))
        self.assertFalse(self.meetings.follow_up(self.manager,meeting['id'])['tasks'][0]['independently_verified'])

    def test_revoked_manager_cannot_read_followthrough(self):
        value=self.prepare();self.store.revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(PermissionDenied):self.meetings.follow_up(self.manager,value['id'])

    def test_meeting_body_defensively_copies_mutable_decision_list(self):
        decisions=[{'kind':'question','summary':'원래 질문'}]
        value=self.prepare(decisions=decisions)
        decisions[0]['summary']='뒤늦은 변경'
        self.assertEqual(self.meetings.follow_up(self.manager,value['id'])['state'],'proposed')
        confirmed=self.meetings.confirm(self.owner,value['id'],value['fingerprint'])
        self.assertEqual(confirmed['record']['decisions'][0]['summary'],'원래 질문')
