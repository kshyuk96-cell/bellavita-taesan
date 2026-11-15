from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from manager_bridge.store import Store, Conflict, PermissionDenied, NotFound, token_hash
from manager_bridge.maintenance_registry import Registry
from manager_bridge.agent_directory import AgentObservation, Connector
from manager_bridge.delegation import SourceProof
from manager_bridge.representative_api import dispatch

BASE='a'*40
CHECKS=['authorization_regression','approval_regression']


class Handler:
    def __init__(self,server):self.server=server;self.status=None;self.result=None
    def respond(self,status,result):self.status,self.result=status,result


class RepresentativeAPI(unittest.TestCase):
    def setUp(self):
        root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now=1000.0
        self.store=Store(root/'manager.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        self.config={'company':'a','name':'Company A','source_kind':'github','source_ref':'fixture/a','branch':'main','services':[],'maintenance_enabled':True}
        self.registry.register(self.owner,'a',self.config)
        self.registry.register(self.owner,'b',{**self.config,'company':'b','name':'Company B','source_ref':'fixture/b'})
        self.manager=self.store.add_token('manager','manager',['a'])
        self.worker=self.store.add_token('worker','worker',['a'])
        self.server=SimpleNamespace(store=self.store,manager_aliases={'ERP':('a',)})

    def call(self,method,route,body=None,token=None):
        handler=Handler(self.server)
        handled=dispatch(handler,method,'/v1/representative/'+route,token or self.manager,body)
        self.assertTrue(handled)
        self.assertEqual(handler.status,200)
        self.assertTrue(handler.result['ok'])
        return handler.result['result']

    def task(self,key='task',approve=False):
        task=self.store.submit(self.manager,key,{'project':'a','repository':'fixture/a','base_sha':BASE,'kind':'test','description':'Synthetic test task','checks':CHECKS})
        if approve:task=self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'])
        return task

    def intake(self,source='direct_request'):
        return self.call('POST','intake',{'request_key':'request','conversation':'chat','text':'ERP test','source':source})

    def grant(self):
        return self.call('POST','delegation/propose',{'project':'a','manager_hash':token_hash(self.manager),'allowed_paths':['tests/'],'allowed_checks':CHECKS,'kinds':['test'],'max_tasks':2,'expires_at':2000},token=self.owner)

    def agent(self):
        self.server.agent_connectors={('a','agent'):Connector(lambda *_:AgentObservation('a','agent','session',token_hash(self.worker),True,'fixture/ready',self.now,self.now+120))}
        self.call('POST','agents/register',{'project':'a','agent_id':'agent','worker_hash':token_hash(self.worker),'session_id':'session','enabled':True})
        self.call('POST','agents/probe',{'project':'a','agent_id':'agent'})

    def test_unrelated_or_unknown_paths_are_not_handled(self):
        handler=Handler(self.server)
        self.assertFalse(dispatch(handler,'GET','/healthz',self.manager))
        self.assertFalse(dispatch(handler,'POST','/v1/representative/unknown',self.manager,{}))
        self.assertFalse(dispatch(handler,'DELETE','/v1/representative/intake',self.manager,{}))
        self.assertIsNone(handler.result)

    def test_invalid_authentication_cannot_reach_sdk(self):
        with self.assertRaises(PermissionDenied):self.call('GET','agents?project=a',token='x'*40)

    def test_intake_get_and_task_proposal_do_not_approve(self):
        intake=self.intake()
        self.assertEqual(intake['project'],'a')
        saved=self.call('GET','intake?id='+intake['id'])
        self.assertEqual(saved['id'],intake['id'])
        task=self.task()
        proposed=self.call('POST','intake/task',{'id':intake['id'],'manifest':task['manifest']})
        self.assertEqual(proposed['state'],'pending_approval')
        self.assertFalse(saved['approval_granted'])

    def test_business_material_cannot_create_task(self):
        intake=self.intake('business_material')
        with self.assertRaises(PermissionDenied):
            self.call('POST','intake/task',{'id':intake['id'],'manifest':self.task()['manifest']})

    def test_aliases_are_server_configuration_only(self):
        body={'request_key':'r','conversation':'chat','text':'ERP test','aliases':{'ERP':['b']}}
        with self.assertRaises(ValueError):self.call('POST','intake',body)
        self.assertEqual(self.intake()['project'],'a')

    def test_extra_execution_or_credential_fields_rejected(self):
        for field in ('connectors','executor','environment','program','owner_token','source_observers'):
            with self.subTest(field=field),self.assertRaises(ValueError):
                self.call('POST','agents/register',{'project':'a','agent_id':'agent','worker_hash':token_hash(self.worker),'session_id':'session',field:'anything'})

    def test_missing_and_duplicate_query_values_rejected(self):
        for route in ('agents','agents?project=a&project=b','agents?project=','agents?project=a&owner_token=anything'):
            with self.subTest(route=route),self.assertRaises(ValueError):self.call('GET',route)

    def test_post_query_and_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):self.call('POST','intake?project=a',{'request_key':'r','conversation':'c','text':'ERP'})
        with self.assertRaises(ValueError):self.call('POST','intake',{'request_key':'r','conversation':'c'})

    def test_cross_project_agent_listing_and_worker_admin_denied(self):
        with self.assertRaises(NotFound):self.call('GET','agents?project=b')
        with self.assertRaises(PermissionDenied):self.call('GET','agents?project=a',token=self.worker)

    def test_delegate_requires_owner_and_exact_activation(self):
        with self.assertRaises(PermissionDenied):
            self.call('POST','delegation/propose',{'project':'a','manager_hash':token_hash(self.manager),'allowed_paths':['tests/'],'allowed_checks':CHECKS,'kinds':['test'],'max_tasks':2,'expires_at':2000})
        grant=self.grant()
        with self.assertRaises(Conflict):self.call('POST','delegation/activate',{'id':grant['id'],'fingerprint':'0'*64},token=self.owner)
        self.call('POST','delegation/activate',{'id':grant['id'],'fingerprint':grant['fingerprint']},token=self.owner)
        self.assertEqual(self.call('GET','delegation?id='+grant['id'])['state'],'active')
        self.assertEqual(self.call('POST','delegation/revoke',{'id':grant['id']},token=self.owner)['state'],'revoked')

    def test_disconnected_source_does_not_authorize(self):
        grant=self.grant();self.call('POST','delegation/activate',{'id':grant['id'],'fingerprint':grant['fingerprint']},token=self.owner)
        task=self.task()
        with self.assertRaises(Conflict):
            self.call('POST','delegation/authorize',{'grant_id':grant['id'],'task_id':task['id'],'version':task['version'],'manifest_digest':task['manifest_digest'],'requested_paths':['tests/']})
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'pending_approval')

    def test_registered_source_can_authorize_bounded_task(self):
        self.server.delegation_source_observers={'a':lambda p:SourceProof(p,'fixture/a','main',BASE,'fixture/source',self.now)}
        grant=self.grant();self.call('POST','delegation/activate',{'id':grant['id'],'fingerprint':grant['fingerprint']},token=self.owner)
        task=self.task()
        result=self.call('POST','delegation/authorize',{'grant_id':grant['id'],'task_id':task['id'],'version':task['version'],'manifest_digest':task['manifest_digest'],'requested_paths':['tests/']})
        self.assertEqual(result['state'],'queued')
        self.assertNotIn(self.owner,json.dumps(result))

    def test_disabled_agent_is_not_connected_by_registration(self):
        result=self.call('POST','agents/register',{'project':'a','agent_id':'agent','worker_hash':token_hash(self.worker),'session_id':'session'})
        self.assertEqual(result['state'],'disabled')
        self.assertEqual(self.call('GET','agents?project=a')[0]['state'],'disabled')

    def test_instruction_ack_and_report_require_exact_worker_claim(self):
        self.agent();task=self.task(approve=True)
        message=self.call('POST','agents/enqueue',{'project':'a','agent_id':'agent','request_key':'delivery','task_id':task['id'],'version':task['version'],'manifest_digest':task['manifest_digest']})
        self.assertEqual(len(self.call('GET','agents/poll?project=a&agent_id=agent',token=self.worker)),1)
        assignment=self.store.claim(self.worker,'a',task_id=task['id'])
        with self.assertRaises(PermissionDenied):self.call('POST','agents/ack',{'id':message['id'],'lease_token':assignment['lease_token']})
        ack=self.call('POST','agents/ack',{'id':message['id'],'lease_token':assignment['lease_token']},token=self.worker)
        self.assertFalse(ack['execution_verified'])
        result=self.call('POST','agents/report',{'id':message['id'],'lease_token':assignment['lease_token'],'report':{'head_sha':BASE,'summary':'synthetic outcome','checks':[{'name':'fixture','passed':True}]}},token=self.worker)
        self.assertNotIn(assignment['lease_token'],json.dumps(result))
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'reported')
        self.assertEqual(self.call('GET','agents/delivery?id='+message['id'])['state'],'reported')

    def test_send_without_sender_does_not_claim_delivery_or_execution(self):
        self.agent();task=self.task(approve=True)
        message=self.call('POST','agents/enqueue',{'project':'a','agent_id':'agent','request_key':'delivery','task_id':task['id'],'version':1,'manifest_digest':task['manifest_digest']})
        result=self.call('POST','agents/send',{'id':message['id']})
        self.assertEqual(result['state'],'pending')
        self.assertEqual(result['notification'],'not_configured')
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'queued')

    def test_control_uses_existing_cancel_state(self):
        self.agent();task=self.task(approve=True)
        message=self.call('POST','agents/enqueue',{'project':'a','agent_id':'agent','request_key':'delivery','task_id':task['id'],'version':1,'manifest_digest':task['manifest_digest']})
        self.call('POST','agents/control',{'id':message['id'],'action':'cancel'})
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'cancelled')

    def test_meeting_confirmation_records_decision_not_task_approval(self):
        task=self.task()
        record=self.call('POST','meetings/prepare',{'project':'a','request_key':'meeting','agenda':'Review synthetic scope','decisions':[{'kind':'develop','summary':'approve routine fixture work'}],'task_ids':[task['id']]})
        with self.assertRaises(PermissionDenied):self.call('POST','meetings/confirm',{'id':record['id'],'fingerprint':record['fingerprint']})
        self.call('POST','meetings/confirm',{'id':record['id'],'fingerprint':record['fingerprint']},token=self.owner)
        follow=self.call('GET','meetings/followup?id='+record['id'])
        self.assertEqual(follow['state'],'confirmed')
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'pending_approval')

    def test_one_exact_packet_can_approve_two_development_tasks(self):
        tasks=[self.task('one'),self.task('two')]
        packet=self.call('POST','packets/prepare',{'request_key':'packet','task_ids':[x['id'] for x in tasks]})
        self.assertEqual(len(self.call('GET','packets?id='+packet['id'])['approval_required_tasks']),2)
        with self.assertRaises(PermissionDenied):self.call('POST','packets/decide',{'id':packet['id'],'fingerprint':packet['fingerprint']})
        self.call('POST','packets/decide',{'id':packet['id'],'fingerprint':packet['fingerprint']},token=self.owner)
        self.assertTrue(all(self.store.get_task(self.manager,t['id'])['state']=='queued' for t in tasks))

    def test_packet_cannot_request_deployment(self):
        task=self.task();packet=self.call('POST','packets/prepare',{'request_key':'packet','task_ids':[task['id']]})
        with self.assertRaises(ValueError):self.call('POST','packets/decide',{'id':packet['id'],'fingerprint':packet['fingerprint'],'decision':'deploy'},token=self.owner)

    def test_review_defaults_unmeasured_and_blocker_does_not_retry(self):
        task=self.task(approve=True)
        self.assertFalse(self.call('POST','review/verify',{'task_id':task['id']})['independently_verified'])
        result=self.call('POST','review/changes',{'projects':['a']})
        self.assertIn('not_measured',json.dumps(result))
        blocker=self.call('POST','review/blocker',{'task_id':task['id'],'code':'usage_limit'})
        self.assertFalse(blocker['automatically_replayed'])

    def test_revoked_token_does_not_replay_prior_intake(self):
        request=self.intake()
        self.store.revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(PermissionDenied):self.call('GET','intake?id='+request['id'])

    def test_current_bearer_cannot_be_stored_as_text(self):
        with self.assertRaises(ValueError):self.call('POST','intake',{'request_key':'r','conversation':'c','text':'ERP '+self.manager})
