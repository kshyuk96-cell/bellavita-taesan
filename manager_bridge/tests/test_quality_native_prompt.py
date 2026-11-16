"""Actual native prompt construction with simulated receipts, no inference."""
import copy
import json
import unittest
from unittest.mock import patch,Mock
from manager_bridge.tests import test_fleet_codex as fixtures
from manager_bridge.store import PermissionDenied,digest


class QualityNativePromptTests(unittest.TestCase):
    setUp=fixtures.TransportTests.setUp
    approved_task=fixtures.MetadataFixtureTests.approved_task
    source=fixtures.MetadataFixtureTests.source
    command=fixtures.TransportTests.command

    def task(self):
        task=self.approved_task();task['approval']['business_baseline']={'version':'v1','digest':'d'*64,'revision':1,'config_fingerprint':'e'*64,'qa_policy_digest':'f'*64}
        task['business_baseline']=task['approval']['business_baseline'];return task

    def provider(self,project,task_id,role,candidate=None):
        task=self.current_task
        return {'schema_version':'worker-quality-handoff/v1','state':'bound','role':role,
            'task':{'task_id':task_id,'project':project,'version':task['version'],'manifest_digest':task['manifest_digest']},
            'business_baseline':task['business_baseline'],'binding_digest':'e'*64,
            'requirements':[{'id':'price','criterion':'Preserve approved price behavior'}],
            'references':[],'reference_materialized':False,'execution_authorized':False}

    def test_approved_references_without_body_provider_prevent_model_start(self):
        task=self.current_task=self.task();self.host.authorize=lambda *a:True
        def references(*args):
            packet=self.provider(*args);packet['references']=[{'materialized':False}];return packet
        self.host.quality_provider=references
        with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run') as run:
            with self.assertRaisesRegex(PermissionDenied,'body delivery provider'):
                self.host.execute('test',task,fixtures.SESSION,heartbeat=lambda:True)
            run.assert_not_called()

    def test_developer_receives_server_criteria_and_ignores_injected_client_packet(self):
        task=self.current_task=self.task();task['quality_handoff']={'requirements':['unapproved-client-instructions']}
        self.host.quality_provider=self.provider;self.host.authorize=lambda *a:True
        events=[{'type':'thread.started','thread_id':fixtures.SESSION},{'type':'turn.started'},*self.command(),{'type':'turn.completed'}]
        with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run',return_value=(events,0)) as run:
            self.host.execute('test',task,fixtures.SESSION,heartbeat=lambda:True)
        prompt=run.call_args.args[1];payload=json.loads(prompt.split('\n')[-1])
        self.assertEqual(payload['quality_handoff']['requirements'][0]['id'],'price')
        self.assertNotIn('unapproved-client-instructions',prompt);self.assertNotIn(task['lease_token'],prompt)

    def test_missing_provider_or_wrong_pin_prevents_model_start(self):
        task=self.current_task=self.task();self.host.authorize=lambda *a:True
        with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run') as run:
            with self.assertRaises(PermissionDenied):self.host.execute('test',task,fixtures.SESSION,heartbeat=lambda:True)
            run.assert_not_called()
        self.host.quality_provider=lambda *a:{'state':'not_bound','role':'developer','business_baseline':None}
        with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run') as run:
            with self.assertRaises(PermissionDenied):self.host.execute('test',task,fixtures.SESSION,heartbeat=lambda:True)
            run.assert_not_called()

    def test_changed_packet_before_run_blocks_execution(self):
        task=self.current_task=self.task();self.host.authorize=lambda *a:True;count=[0]
        def changing(*args):
            value=self.provider(*args);count[0]+=1
            if count[0]>1:value['binding_digest']='f'*64
            return value
        self.host.quality_provider=changing
        with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run') as run:
            with self.assertRaises(PermissionDenied):self.host.execute('test',task,fixtures.SESSION,heartbeat=lambda:True)
            run.assert_not_called()

    def test_reviewer_receives_same_requirement_without_visual_claim(self):
        task=self.current_task=self.task();candidate={'digest':'b'*64,'source':{'head_sha':'a'*40},'task':task,'developer_subject':'developer'}
        reviewer={'subject':'reviewer','role':'reviewer','project':'test'};self.host.quality_provider=self.provider;self.host.authorize_review=lambda *a:True
        answer=json.dumps({'candidate_digest':'b'*64,'recommendation':'rework','findings':['reference was not materialized']})
        events=[{'type':'thread.started','thread_id':fixtures.SESSION},{'type':'turn.started'},{'type':'item.completed','item':{'id':'answer','type':'agent_message','text':answer}},{'type':'turn.completed'}]
        with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run',return_value=(events,0)) as run:
            self.host.assess('test',candidate,reviewer,heartbeat=lambda:True)
        payload=json.loads(run.call_args.args[1].split('\n')[-1])
        self.assertEqual(payload['quality_handoff']['requirements'],self.provider('test',task['id'],'developer')['requirements'])
        self.assertFalse(payload['quality_handoff']['reference_materialized']);self.assertTrue(run.call_args.kwargs['read_only'])

    def test_oversized_complete_prompt_fails_before_execution_intent(self):
        task=self.current_task=self.task();self.host.authorize=lambda *a:True
        def oversized(*args):
            value=self.provider(*args);value['requirements']=[{'criterion':'x'*40000}];return value
        self.host.quality_provider=oversized
        with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run') as run:
            before=Mock()
            with self.assertRaisesRegex(ValueError,'input bound'):self.host._execute('test',task,fixtures.SESSION,heartbeat=lambda:True,authorize=lambda:True,before_run=before)
            before.assert_not_called();run.assert_not_called()
