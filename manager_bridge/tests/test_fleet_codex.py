"""Fixture tests; these do not claim live Codex inference or technical acceptance."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from manager_bridge.fleet_codex import CodexHost,ProjectBinding,TransportUncertain,TransportUnavailable,_environment,_origin_repository

SESSION='00000000-0000-4000-8000-000000000001'
class TransportTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.profile=self.root/'profile';self.profile.mkdir(mode=0o700)
  self.binary=self.root/'codex';self.binary.write_text('#!/bin/sh\nexit 0\n');self.binary.chmod(0o700)
  self.host=CodexHost(self.binary,self.profile,{'test':ProjectBinding('test','example/test',self.root,require_origin=False)})
  # Fixture-only trusted proof injection; real host requires actual subprocess probe.
  self.host._confidentiality['test']=((),{'verified':True})
 def receipt(self,items=(),code=0,session=SESSION):
  events=[{'type':'thread.started','thread_id':session},{'type':'turn.started'},*items,{'type':'turn.completed'}]
  return self.host._receipt('test','a'*32,events,code,SESSION)[0]
 def command(self,exit_code=0):
  item={'id':'cmd1','type':'command_execution','command':'python -m unittest'}
  return [{'type':'item.started','item':item},{'type':'item.completed','item':dict(item,exit_code=exit_code,status='completed' if exit_code==0 else 'failed')}]
 def test_actual_command_receipt(self):
  r=self.receipt(self.command());self.assertEqual(r.state,'execution_observed');self.assertEqual(r.command_successes,1);self.assertFalse(r.independently_verified);self.assertIsNone(r.testcase_count)
 def test_exit_zero_does_not_hide_failed_command(self):
  r=self.receipt(self.command(1));self.assertEqual(r.state,'execution_failed');self.assertEqual(r.command_failures,1)
 def test_prose_is_not_command_or_test_evidence(self):
  r=self.receipt([{'type':'item.completed','item':{'id':'a','type':'agent_message','text':'100 tests passed'}}]);self.assertEqual(r.state,'dialogue_completed');self.assertEqual(r.command_attempts,0);self.assertIsNone(r.testcase_count)
 def test_wrong_session_rejected(self):
  with self.assertRaises(TransportUncertain):self.receipt(session='00000000-0000-4000-8000-000000000002')
 def test_process_failure_rejected(self):
  with self.assertRaises(TransportUncertain):self.receipt(code=1)
 def test_command_missing_start_rejected(self):
  with self.assertRaises(TransportUncertain):self.receipt(self.command()[1:])
 def test_unfinished_command_rejected(self):
  with self.assertRaises(TransportUncertain):self.receipt(self.command()[:1])
 def test_contradictory_command_rejected(self):
  events=self.command();events[1]['item']['status']='failed'
  with self.assertRaises(TransportUncertain):self.receipt(events)
 def test_unknown_event_rejected(self):
  with self.assertRaises(TransportUncertain):self.receipt([{'type':'invented.success'}])
 def test_command_body_never_returned(self):
  r=self.receipt(self.command());self.assertNotIn('python',repr(r));self.assertIn('command_digest',r.command_receipts[0])
 def test_environment_excludes_credentials(self):
  with patch.dict(os.environ,{'OPENAI_API_KEY':'secret','GH_TOKEN':'secret','GOOGLE_APPLICATION_CREDENTIALS':'secret','DATABASE_URL':'secret'}):
   env=_environment(self.profile)
  for key in ('OPENAI_API_KEY','GH_TOKEN','GOOGLE_APPLICATION_CREDENTIALS','DATABASE_URL'):self.assertNotIn(key,env)
  self.assertEqual(env['CODEX_HOME'],str(self.profile))
 def test_origin_formats(self):
  for value in ('https://github.com/owner/repo.git','git@github.com:owner/repo.git','ssh://git@github.com/owner/repo.git'):self.assertEqual(_origin_repository(value),'owner/repo')
 def test_untrusted_origin_rejected(self):
  with self.assertRaises(TransportUnavailable):_origin_repository('https://evil.test/owner/repo')
 def test_shared_profile_rejected(self):
  self.profile.chmod(0o755)
  with self.assertRaises(ValueError):CodexHost(self.binary,self.profile,self.host.bindings)
 def test_unregistered_project_rejected(self):
  with self.assertRaises(PermissionError):self.host._binding('unknown')
 def test_probe_is_metadata_only(self):
  calls=[]
  class Rpc:
   def __init__(self,*a):pass
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def request(inner,method,params):
    calls.append((method,params))
    if method=='account/read':return {'account':{'type':'chatgpt'}}
    return {'thread':{'id':params['threadId'],'cwd':str(self.root)}}
  with patch('manager_bridge.fleet_codex._Rpc',Rpc):result=self.host.probe_owned_refs('test',[SESSION])
  self.assertEqual(result['resolved_metadata'],1);self.assertFalse(result['existing_ui_chat_control_verified']);self.assertEqual(calls[1],('thread/read',{'threadId':SESSION,'includeTurns':False}));self.assertEqual(result['model_turns_started'],0)

class MetadataFixtureTests(TransportTests):
 def source(self):return {'sha':'a'*40,'tracked_and_visible_untracked_clean':True}
 def rpc(self,pages,ready=True):
  calls=[];iterator=iter(pages)
  class Rpc:
   def __init__(self,*a):pass
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def request(inner,method,params):
    calls.append((method,params))
    if method=='account/read':return {'account':{'type':'chatgpt' if ready else 'apiKey'}}
    if method=='thread/start':return {'thread':{'id':SESSION,'cwd':str(self.root)}}
    return next(iterator)
  return Rpc,calls
 def test_discover_scoped_pagination(self):
  thread={'id':SESSION,'cwd':str(self.root),'preview':'private title'}
  Rpc,calls=self.rpc([{'data':[thread],'nextCursor':'next'},{'data':[],'nextCursor':None}])
  with patch.object(self.host,'_source',return_value=self.source()),patch('manager_bridge.fleet_codex._Rpc',Rpc):result=self.host.discover('test')
  self.assertEqual(len(result),1);self.assertTrue(result[0].can_receive);self.assertNotIn('private title',repr(result));self.assertTrue(calls[1][1]['useStateDbOnly']);self.assertEqual(calls[1][1]['cwd'],str(self.root))
 def test_no_api_key_fallback(self):
  Rpc,_=self.rpc([{'data':[{'id':SESSION,'cwd':str(self.root)}]}],ready=False)
  with patch.object(self.host,'_source',return_value=self.source()),patch('manager_bridge.fleet_codex._Rpc',Rpc):result=self.host.discover('test')
  self.assertFalse(result[0].can_receive)
 def test_repeated_cursor_fails(self):
  Rpc,_=self.rpc([{'data':[],'nextCursor':'same'},{'data':[],'nextCursor':'same'}])
  with patch.object(self.host,'_source',return_value=self.source()),patch('manager_bridge.fleet_codex._Rpc',Rpc),self.assertRaises(TransportUnavailable):self.host.discover('test')
 def test_discovery_source_change_fails(self):
  Rpc,_=self.rpc([{'data':[]}]);other=dict(self.source(),sha='b'*40)
  with patch.object(self.host,'_source',side_effect=[self.source(),other]),patch('manager_bridge.fleet_codex._Rpc',Rpc),self.assertRaises(TransportUnavailable):self.host.discover('test')
 def test_create_is_metadata_only(self):
  from manager_bridge.store import digest
  manifest={'project':'test','repository':'example/test','base_sha':'a'*40,'description':'fixture','checks':['unit'],'kind':'development'}
  task={'state':'queued','version':1,'manifest':manifest,'manifest_digest':digest(manifest),'approval':{'version':1,'manifest_digest':digest(manifest)}}
  Rpc,calls=self.rpc([])
  with patch.object(self.host,'_source',return_value=self.source()),patch('manager_bridge.fleet_codex._Rpc',Rpc):result=self.host.create('test',task,'b'*64)
  self.assertEqual(result.session_id,SESSION);self.assertEqual([v[0] for v in calls],['account/read','thread/start']);self.assertEqual(calls[-1][1],{'cwd':str(self.root)})
 def test_create_running_task_rejected(self):
  with patch.object(self.host,'_source',return_value=self.source()),self.assertRaises(PermissionError):self.host.create('test',{'state':'running'},'b'*64)
 def test_run_real_fixture_jsonl_subprocess(self):
  # This executable is a local fixture, not Codex and not an AI test.
  self.binary.write_text('#!/usr/bin/python3\nimport sys,json\nprompt=sys.stdin.read()\nfor e in '+repr([{'type':'thread.started','thread_id':SESSION},{'type':'turn.started'},{'type':'turn.completed'}])+': print(json.dumps(e))\n')
  events,code=self.host._run('test','approved fixture',session_id=SESSION,read_only=False,heartbeat=lambda:True)
  self.assertEqual(code,0);self.assertEqual(len(events),3)
 def test_run_bad_json_is_uncertain(self):
  self.binary.write_text('#!/usr/bin/python3\nimport sys\nsys.stdin.read()\nprint("not json")\n')
  with self.assertRaises(TransportUncertain):self.host._run('test','fixture',session_id=SESSION,read_only=False,heartbeat=lambda:True)
 def test_bounded_timeout_even_if_child_never_reads_prompt(self):
  self.binary.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(30)\n')
  self.host.execution_timeout=1
  with self.assertRaises(TransportUncertain):self.host._run('test','x'*32000,session_id=SESSION,read_only=False,heartbeat=lambda:True)
 def test_authorization_denied_before_launch(self):
  calls=[]
  def denied():calls.append(True);raise PermissionError('revoked')
  with self.assertRaises(PermissionError):self.host._run('test','fixture',session_id=SESSION,read_only=False,heartbeat=denied)
  self.assertEqual(len(calls),1)
 def approved_task(self):
  from manager_bridge.store import digest
  manifest={'project':'test','repository':'example/test','base_sha':'a'*40,'description':'fixture','checks':['unit'],'kind':'development'}
  return {'id':'a'*32,'state':'running','version':1,'worker':'developer','manifest':manifest,'manifest_digest':digest(manifest),'approval':{'version':1,'manifest_digest':digest(manifest)},'lease_token':'c'*64,'lease_expires_at':9999999999}
 def test_execute_prompt_excludes_lease(self):
  task=self.approved_task();self.host.authorize=lambda *a:True
  events=[{'type':'thread.started','thread_id':SESSION},{'type':'turn.started'},*self.command(),{'type':'turn.completed'}]
  with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run',return_value=(events,0)) as run:
   receipt=self.host.execute('test',task,SESSION,heartbeat=lambda:True)
  self.assertNotIn(task['lease_token'],run.call_args.args[1]);self.assertEqual(receipt.command_successes,1);self.assertIn(('test',SESSION,task['id']),self.host._deliveries)
 def test_execute_requires_server_authorizer(self):
  with patch.object(self.host,'_source',return_value=self.source()),self.assertRaises(PermissionError):self.host.execute('test',self.approved_task(),SESSION,heartbeat=lambda:True)
 def test_code_assessment_requires_exact_json(self):
  task=self.approved_task();candidate={'digest':'b'*64,'source':{'head_sha':'a'*40},'task':task,'developer_subject':'developer'};reviewer={'subject':'reviewer','role':'reviewer','project':'test'};self.host.authorize_review=lambda *a:True
  events=[{'type':'thread.started','thread_id':SESSION},{'type':'turn.started'},{'type':'item.completed','item':{'id':'answer','type':'agent_message','text':'Everything passes'}},{'type':'turn.completed'}]
  with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run',return_value=(events,0)),self.assertRaises(TransportUncertain):self.host.assess('test',candidate,reviewer,heartbeat=lambda:True)
 def test_code_assessment_is_advisory_and_readonly(self):
  import json
  task=self.approved_task();candidate={'digest':'b'*64,'source':{'head_sha':'a'*40},'task':task,'developer_subject':'developer'};reviewer={'subject':'reviewer','role':'reviewer','project':'test'};self.host.authorize_review=lambda *a:True
  answer=json.dumps({'candidate_digest':'b'*64,'recommendation':'rework','findings':['missing case']})
  events=[{'type':'thread.started','thread_id':SESSION},{'type':'turn.started'},{'type':'item.completed','item':{'id':'answer','type':'agent_message','text':answer}},{'type':'turn.completed'}]
  with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run',return_value=(events,0)) as run:result=self.host.assess('test',candidate,reviewer,heartbeat=lambda:True)
  self.assertEqual(result.recommendation,'rework');self.assertTrue(run.call_args.kwargs['read_only']);self.assertIsNone(run.call_args.kwargs['session_id']);self.assertNotIn('missing case',repr(result))
 def test_inventory_overflow_fails_closed(self):
  from uuid import UUID
  items=[{'id':str(UUID(int=i+1)),'cwd':str(self.root)} for i in range(201)]
  Rpc,_=self.rpc([{'data':items}])
  with patch.object(self.host,'_source',return_value=self.source()),patch('manager_bridge.fleet_codex._Rpc',Rpc),self.assertRaises(TransportUnavailable):self.host.discover('test')
 def test_other_project_inventory_not_enrolled(self):
  Rpc,_=self.rpc([{'data':[{'id':SESSION,'cwd':'/other/project','preview':'private'}]}])
  with patch.object(self.host,'_source',return_value=self.source()),patch('manager_bridge.fleet_codex._Rpc',Rpc):result=self.host.discover('test')
  self.assertEqual(result,())
 def test_create_lost_reply_is_uncertain_without_retry(self):
  task=self.approved_task();task['state']='queued';Rpc,calls=self.rpc([])
  original=Rpc.request
  def request(instance,method,params):
   if method=='thread/start':calls.append((method,params));raise TransportUnavailable('fixture lost reply')
   return original(instance,method,params)
  Rpc.request=request
  with patch.object(self.host,'_source',return_value=self.source()),patch('manager_bridge.fleet_codex._Rpc',Rpc),self.assertRaises(TransportUncertain):self.host.create('test',task,'b'*64)
  self.assertEqual(sum(method=='thread/start' for method,_ in calls),1)
 def test_provider_and_delivery_observer_integration(self):
  import hashlib
  from manager_bridge.store import digest
  task=self.approved_task();self.host.authorize=lambda *a:True
  events=[{'type':'thread.started','thread_id':SESSION},{'type':'turn.started'},*self.command(),{'type':'turn.completed'}]
  with patch.object(self.host,'_source',return_value=self.source()),patch.object(self.host,'_run',return_value=(events,0)):self.host.execute('test',task,SESSION,heartbeat=lambda:True)
  provider=self.host.provider('test');evidence=provider.delivery_observer('test',SESSION,task['id'])
  self.assertEqual(evidence.state,'commands_observed');self.assertEqual(evidence.task_version,1);self.assertEqual(evidence.manifest_digest,task['manifest_digest']);self.assertEqual(evidence.worker_subject_digest,digest(task['worker']));self.assertEqual(evidence.lease_digest,hashlib.sha256(task['lease_token'].encode()).hexdigest());self.assertNotIn(task['lease_token'],repr(evidence))
 def test_no_delivery_receipt_without_actual_execution(self):
  with self.assertRaises(TransportUnavailable):self.host.delivery_observer('test',SESSION,'a'*32)
 def test_named_permissions_have_no_legacy_sandbox_override(self):
  args=self.host._permission_args('test',False,self.root/'job',('/private/control',))
  text=' '.join(args)
  self.assertIn('default_permissions="taesan-worker"',text);self.assertIn('":root"="deny"',text);self.assertIn(str(self.profile)+'"="deny"',text);self.assertIn('network={enabled=false}',text);self.assertNotIn('sandbox_mode',text);self.assertNotIn('-s',args)
 def test_no_model_without_actual_confidentiality_probe(self):
  self.host._confidentiality.clear()
  with self.assertRaises(PermissionError):self.host._run('test','fixture',session_id=SESSION,read_only=False,heartbeat=lambda:True)
 def test_repository_config_cannot_restore_broad_sandbox(self):
  d=self.root/'.codex';d.mkdir();(d/'config.toml').write_text('sandbox_mode="danger-full-access"\n')
  with self.assertRaises(TransportUnavailable):self.host._permission_args('test',False,self.root/'job')
 def test_probe_failure_never_enables_model(self):
  import subprocess
  self.host._confidentiality.clear()
  with patch('manager_bridge.fleet_codex.subprocess.run',return_value=subprocess.CompletedProcess([],1,b'',b'')),self.assertRaises(TransportUnavailable):self.host.verify_confidentiality('test')
  self.assertNotIn('test',self.host._confidentiality)
 def test_redundant_nested_denies_canonicalized_without_erasing_carveouts(self):
  import tomllib
  args=self.host._permission_args('test',False,self.root/'job',(self.root,self.root/'private.token',self.root/'profile'/'key'))
  value=next(args[i+1] for i,x in enumerate(args[:-1]) if x=='-c' and args[i+1].startswith('permissions.taesan-worker='))
  fs=tomllib.loads('policy='+value.split('=',1)[1])['policy']['filesystem']
  # The exact cwd reopens self.root, so nested private denies remain required.
  self.assertEqual(fs[str(self.root/'private.token')],'deny');self.assertEqual(fs[str(self.profile)],'deny');self.assertNotIn(str(self.profile/'key'),fs)
 def test_denied_parent_hides_private_sibling_without_nested_mount(self):
  import tomllib
  parent=self.root.parent
  args=self.host._permission_args('test',False,self.root/'job',(parent,parent/'private.token'))
  value=next(args[i+1] for i,x in enumerate(args[:-1]) if x=='-c' and args[i+1].startswith('permissions.taesan-worker='))
  fs=tomllib.loads('policy='+value.split('=',1)[1])['policy']['filesystem']
  self.assertNotIn(str(parent),fs);self.assertEqual(fs[str(parent/'private.token')],'deny');self.assertEqual(fs[str(self.root)],'write');self.assertEqual(fs[str(self.profile)],'deny');self.assertEqual(fs[':root'],'deny')
