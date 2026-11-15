"""Local host integration fixtures. No Codex, credentials or real isolation."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from manager_bridge.continuous_host import ContinuousHost,ProjectHost,_git,_private_json,load_host,_validate_resources
from manager_bridge.continuous_manager import ContinuousManager,ActionRequest
from manager_bridge.fleet import SessionObservation,Fleet,FleetProvider,DeliveryObservation,ReservedExecutionObservation
from manager_bridge.fleet_codex import ExecutionReceipt
from manager_bridge.store import Store,Conflict,token_hash,digest
from manager_bridge.maintenance_registry import Registry
from manager_bridge.coordination import Coordination
from manager_bridge.three_tier import ThreeTier,ReviewEvidence,CheckOutcome,CodeAssessment
from manager_bridge.three_tier_runner import FixedReviewRunner

SESSION='00000000-0000-4000-8000-000000000001'

class DevelopmentFixture:
    def __init__(self,source,clock):self.source=source;self.clock=clock;self.calls=0;self.fail=False;self.escape=False;self.empty=False;self.saved={};self.native_calls=0
    def discover(self,project):
        return (SessionObservation(project,SESSION,SESSION,'codex_cli','fixture/project',_git(self.source,'rev-parse','HEAD').decode().strip(),True,'fixture/receiver',self.clock()),)
    def execute(self,project,assignment,session,*,heartbeat):
        heartbeat();self.calls+=1
        if self.fail:raise TimeoutError('fixture execution outcome unknown')
        if not self.empty:
            target=self.source/('escape.py' if self.escape else 'src/main.py')
            target.write_text('value = '+str(self.calls)+'\n')
        heartbeat()
        receipt=ExecutionReceipt(project,assignment['id'],session,'execution_observed',0,True,1,1,0,({'command_digest':digest('fixture-command'),'exit_code':0},),digest(['fixture',self.calls]),self.clock())
        self.saved[assignment['id']]=(assignment,receipt)
        return receipt
    def ready_for_new(self,project):return True
    def execute_new(self,project,assignment,reservation_id,*,heartbeat):
        self.native_calls+=1
        return self.execute(project,assignment,SESSION,heartbeat=heartbeat)
    def delivery(self,project,session,task_id):
        task,receipt=self.saved[task_id]
        return DeliveryObservation(project,session,task_id,task['version'],task['manifest_digest'],'fixture/actual-execution','commands_observed',1,0,True,'fixture/actual-delivery',self.clock(),digest(task['worker']),token_hash(task['lease_token']))
    def reserved_observer(self,project,reservation_id,task_id):
        task,receipt=self.saved[task_id]
        session=SessionObservation(project,SESSION,SESSION,'codex_cli','fixture/project',task['manifest']['base_sha'],False,'fixture/native-after-execution',self.clock())
        return ReservedExecutionObservation(reservation_id,session,self.delivery(project,SESSION,task_id))

class HostTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.now=1000
        self.source=self.root/'source';self.source.mkdir();(self.source/'src').mkdir();(self.source/'src/main.py').write_text('value = 0\n')
        _git(self.source,'init','-q');_git(self.source,'add','src/main.py');_git(self.source,'commit','-qm','fixture base')
        self.base=_git(self.source,'rev-parse','HEAD').decode().strip()
        self.candidates=self.root/'candidates';self.candidates.mkdir(mode=0o700);self.runtime=self.root/'runtime';self.runtime.mkdir()
        self.store=Store(self.root/'state.sqlite3',clock=lambda:self.now);self.registry=Registry(self.store);self.coord=Coordination(self.store)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry.register(self.owner,'project',{'company':'fixture','name':'Fixture project','source_kind':'github','source_ref':'fixture/project','branch':'main','services':[],'maintenance_enabled':True})
        self.manager=self.store.add_token('taesan','manager',['project'])
        self.dev=self.store.add_token('developer','worker',['project']);self.reviewer=self.store.add_token('reviewer','worker',['project'])
        self.core=ThreeTier(self.store,self.registry);self.core.set_limits(self.owner,None)
        self.core.set_limits(self.owner,'project')
        for member,token,role in (('taesan',self.manager,'taesan'),('dev',self.dev,'developer'),('reviewer',self.reviewer,'reviewer')):
            self.core.register(self.owner,'project',member,token_hash(token),role,agent_id=None if role=='taesan' else member+'-agent')
        def private(label,token):
            path=self.root/label;path.write_text(token);path.chmod(0o600);return path
        self.managerfile=private('manager.token',self.manager);self.devfile=private('developer.token',self.dev);self.reviewfile=private('reviewer.token',self.reviewer)
        self.transport=DevelopmentFixture(self.source,lambda:self.now);self.reviews=0
        def assess(project,candidate,reviewer,*,heartbeat):
            heartbeat();self.reviews+=1
            return CodeAssessment(candidate['digest'],reviewer['subject'],'pass',digest([]),'fixture/semantic-receipt',self.now)
        self.project=ProjectHost('project','fixture/project',self.source,self.candidates,self.runtime,'dev','reviewer',self.devfile,self.reviewfile,{'fixture-check':['/bin/true']},self.transport,lambda _,**kw:SimpleNamespace(assess=assess),session_id=SESSION)
        self.host=ContinuousHost(self.core,self.managerfile,{'project':self.project});self.scheduler=ContinuousManager(self.core,handlers=self.host.handlers());self.n=0
    def task(self,*,approved=True,base=None,dependencies=()):
        self.n+=1
        task=self.store.submit(self.owner,'fixture-'+str(self.n),{'project':'project','repository':'fixture/project','base_sha':base or self.base,'description':'Implement fixture value.','checks':['fixture-check'],'kind':'development'})
        self.coord.set_policy(self.owner,task['id'],task['version'],task['manifest_digest'],allowed_paths=['src/'],dependencies=list(dependencies))
        if approved:self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'],policy_digest=digest(self.coord.get_policy(self.owner,task['id'])))
        return self.store.get_task(self.owner,task['id'])
    def tick(self):return self.scheduler.tick(self.manager,['project'],'fixture-instance')
    def mechanical(self,runner,candidate,reviewer,*,heartbeat):
        heartbeat()
        return ReviewEvidence(candidate['digest'],reviewer['subject'],(CheckOutcome('fixture-check',digest(['/bin/true']),0,.01),),candidate['source']['artifact_digest'],'fixture/mechanical-only',self.now,True,candidate['source']['runtime_digest'])
    def accepted(self):
        task=self.task()
        with patch.object(FixedReviewRunner,'__call__',lambda runner,candidate,reviewer,**kw:self.mechanical(runner,candidate,reviewer,**kw)):
            for _ in range(8):self.tick()
        return task,self.core.get(self.manager,task['id'])
    def test_real_local_commit_frozen_source_report_review_and_accept(self):
        task,result=self.accepted()
        self.assertEqual(result['state'],'accepted');self.assertEqual(self.transport.calls,1);self.assertEqual(self.reviews,1)
        row=self.host._row(task['id']);self.assertEqual(row['phase'],'frozen');self.assertNotEqual(row['candidate_head'],self.base)
        self.assertEqual((Path(row['candidate_path'])/'src/main.py').read_text(),'value = 1\n')
        self.assertFalse(result['operational_approved']);self.assertFalse(result['connection_verified'])
        for _ in range(4):self.tick()
        self.assertEqual(self.transport.calls,1)
    def test_unapproved_task_never_executes(self):
        self.task(approved=False)
        for _ in range(3):self.tick()
        self.assertEqual(self.transport.calls,0)
    def test_execution_timeout_survives_restart_without_replay(self):
        task=self.task();self.transport.fail=True
        for _ in range(3):self.tick()
        self.assertEqual(self.transport.calls,1);self.assertEqual(self.host._row(task['id'])['phase'],'uncertain')
        self.now+=31;self.scheduler=ContinuousManager(self.core,handlers=self.host.handlers())
        for _ in range(4):self.tick()
        self.assertEqual(self.transport.calls,1)
    def test_unapproved_path_never_committed_or_submitted(self):
        task=self.task();self.transport.escape=True
        for _ in range(5):self.tick()
        self.assertEqual(_git(self.source,'rev-parse','HEAD').decode().strip(),self.base)
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'developing')
        self.assertIsNone(self.store.get_task(self.manager,task['id'])['report'])
    def test_no_source_change_does_not_become_candidate(self):
        task=self.task();self.transport.empty=True
        for _ in range(5):self.tick()
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'developing')
    def test_missing_semantic_assessment_never_passes(self):
        task=self.task()
        object.__setattr__(self.project,'reviewer_factory',lambda _,**kw:SimpleNamespace(assess=lambda *a,**kw:None))
        with patch.object(FixedReviewRunner,'__call__',lambda runner,candidate,reviewer,**kw:self.mechanical(runner,candidate,reviewer,**kw)):
            for _ in range(7):self.tick()
        self.assertNotEqual(self.core.get(self.manager,task['id'])['state'],'accepted')
    def test_private_role_file_required_and_symlinks_rejected(self):
        self.devfile.chmod(0o644)
        with self.assertRaises(ValueError):replace(self.project)
        self.devfile.chmod(0o600);link=self.root/'alias';link.symlink_to(self.devfile)
        with self.assertRaises(ValueError):replace(self.project,developer_token_file=link)
    def test_existing_host_observer_cannot_be_replaced(self):
        with self.assertRaises(ValueError):ContinuousHost(self.core,self.managerfile,{'project':self.project})
    def test_strict_private_configuration_and_duplicate_keys(self):
        path=self.root/'config.json';path.write_text('{"version":1,"version":2}');path.chmod(0o600)
        with self.assertRaises(ValueError):_private_json(path)
        path.write_text('{}');path.chmod(0o644)
        with self.assertRaises(ValueError):load_host(path)
    def test_real_fleet_future_session_created_enrolled_and_executed_once(self):
        task=self.task();created=[]
        provider=FleetProvider(self.transport.discover,lambda project,public,key:(created.append(public['id']) or self.transport.discover(project)[0]))
        fleet=Fleet(self.store,self.registry,providers={('project','fixture-host'):provider})
        fleet.register_host(self.owner,'project','fixture-host')
        fleet.register_template(self.owner,'project','future-workers','fixture-host',max_sessions=2,expires_at=self.now+3600)
        object.__setattr__(self.project,'session_id',None);object.__setattr__(self.project,'fleet_template','future-workers');self.host.fleet=fleet
        with patch.object(FixedReviewRunner,'__call__',lambda runner,candidate,reviewer,**kw:self.mechanical(runner,candidate,reviewer,**kw)):
            for _ in range(8):self.tick()
        self.assertEqual(created,[task['id']]);self.assertEqual(self.transport.calls,1)
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'accepted')
        inventory=fleet.inventory(self.manager,['project'])
        self.assertIsNotNone(inventory)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM fleet_creates').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT session_id FROM continuous_host_sessions').fetchone()[0],SESSION)
    def test_rework_uses_new_execution_checkpoint_and_keeps_prior_receipt(self):
        task=self.task()
        original=self.mechanical
        def checks(runner,candidate,reviewer,**kw):
            proof=original(runner,candidate,reviewer,**kw)
            if self.transport.calls==1:return replace(proof,checks=(CheckOutcome('fixture-check',digest(['/bin/true']),1,.01),))
            return proof
        with patch.object(FixedReviewRunner,'__call__',checks):
            for _ in range(14):self.tick()
        result=self.core.get(self.manager,task['id'])
        self.assertEqual(result['state'],'accepted');self.assertEqual(result['rework_count'],1)
        self.assertEqual(self.transport.calls,2)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT count(*) FROM continuous_host_history').fetchone()[0],1)
    def test_only_exact_preapproved_dependent_task_is_assigned_next(self):
        parent,result=self.accepted();head=self.host._row(parent['id'])['candidate_head']
        dependency=parent['id']
        child=self.task(base=head,dependencies=[dependency]);unapproved=self.task(approved=False,base=head,dependencies=[dependency])
        self.tick()
        self.assertEqual(self.core.get(self.manager,child['id'])['state'],'development_queued')
        self.assertEqual(self.store.get_task(self.manager,unapproved['id'])['state'],'pending_approval')
        with self.store._transaction() as c:
            assignment=c.execute('SELECT next_id FROM hierarchy_next_assignments').fetchone()
            self.assertEqual(assignment[0],child['id'])
    def test_load_host_schema_validates_without_model_or_key_access(self):
        devprofile=self.root/'dev-profile';reviewprofile=self.root/'review-profile'
        devprofile.mkdir(mode=0o700);reviewprofile.mkdir(mode=0o700)
        application=Path(__file__).absolute().parents[2]
        path=self.root/'config.json'
        data=self.root/'control';data.mkdir(mode=0o700)
        database=data/'manager.sqlite3'
        import shutil
        shutil.copyfile(self.store.path,database);database.chmod(0o600)
        payload={'version':1,'application_root':str(application),'expected_source_commit':'a'*40,'data_root':str(data),'database':str(database),'manager_token_file':str(self.managerfile),'instance_id':'fixture-loader','projects':[{
            'project':'project','repository':'fixture/project','source':str(self.source),'candidates':str(self.candidates),'runtime':str(self.runtime),'developer_id':'dev','reviewer_id':'reviewer','developer_token_file':str(self.devfile),'reviewer_token_file':str(self.reviewfile),'checks':{'fixture-check':['/bin/true']},'developer':{'binary':'/bin/true','codex_home':str(devprofile)},'reviewer':{'binary':'/bin/true','codex_home':str(reviewprofile)},'session_id':SESSION}]}
        path.write_text(json.dumps(payload));path.chmod(0o600)
        with patch('manager_bridge.continuous_host.observe_checkout',return_value={'sha':'a'*40,'tracked_and_visible_untracked_clean':True}), patch('manager_bridge.fleet_codex.CodexHost.verify_confidentiality',return_value={'verified':True,'fixture':True}) as probe:
            host,controller,config=load_host(path)
            forbidden=probe.call_args.args[1]
            self.assertIn(devprofile,forbidden);self.assertIn(reviewprofile,forbidden)
        self.assertEqual(config['projects'],('project',));self.assertEqual(self.transport.calls,0)
        # A forbidden control parent cannot be reopened for model work roots.
        payload['data_root']=str(self.root);payload['database']=str(self.store.path)
        path.write_text(json.dumps(payload))
        with patch('manager_bridge.continuous_host.observe_checkout',return_value={'sha':'a'*40,'tracked_and_visible_untracked_clean':True}), patch('manager_bridge.fleet_codex.CodexHost.verify_confidentiality') as probe:
            with self.assertRaisesRegex(ValueError,'control data root must be separate'):load_host(path)
            probe.assert_not_called()
        payload['unknown_command']='/bin/true';path.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):load_host(path)
    def test_cross_project_nested_source_and_trusted_resources_rejected(self):
        other=replace(self.project,project='other',repository='fixture/other')
        with self.assertRaises(ValueError):_validate_resources({'project':self.project,'other':other},self.managerfile,self.store.path)
        nested=self.source/'nested';nested.mkdir()
        other=replace(other,source=nested)
        with self.assertRaises(ValueError):_validate_resources({'project':self.project,'other':other},self.managerfile,self.store.path)
        other_source=self.root/'other-source';other_source.mkdir()
        other=replace(other,source=other_source,runtime=self.source/'src')
        with self.assertRaises(ValueError):_validate_resources({'project':self.project,'other':other},self.managerfile,self.store.path)
    def test_uncertain_process_cannot_clear_quarantine_from_accepted_core_state(self):
        task,result=self.accepted()
        with self.store._transaction() as c:c.execute("UPDATE continuous_host_assignments SET phase='uncertain' WHERE task_id=?",(task['id'],))
        request=ActionRequest(digest('reconcile'),task['id'],'project','reconcile',task['version'],task['manifest_digest'],self.registry.get(self.manager,'project')['fingerprint'],'accepted',result['candidate_digest'])
        receipt=self.host.reconcile(request,lambda:True)
        self.assertEqual(receipt.state,'unavailable');self.assertIn('stop_evidence_required',receipt.reference)
    def test_long_review_renews_supervisor_and_independent_core_leases(self):
        task=self.task()
        def assess(project,candidate,reviewer,*,heartbeat):
            for _ in range(3):
                self.now+=20;heartbeat()
            return CodeAssessment(candidate['digest'],reviewer['subject'],'pass',digest([]),'fixture/long-semantic-review',self.now)
        object.__setattr__(self.project,'reviewer_factory',lambda _,**kw:SimpleNamespace(assess=assess))
        with patch.object(FixedReviewRunner,'__call__',lambda runner,candidate,reviewer,**kw:self.mechanical(runner,candidate,reviewer,**kw)):
            for _ in range(7):self.tick()
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'accepted')
    def test_intermediate_command_failure_does_not_strand_independently_passing_next(self):
        original=self.transport.execute
        def execute(*args,**kw):
            receipt=original(*args,**kw)
            return replace(receipt,state='execution_failed',command_attempts=2,command_successes=1,command_failures=1)
        self.transport.execute=execute
        parent,result=self.accepted()
        report=self.store.get_task(self.manager,parent['id'])['report']
        self.assertEqual(report['checks'],[{'name':'codex_turn_completed','passed':True}])
        self.assertIn('failures: 1',report['summary'])
        self.assertEqual(json.loads(self.host._row(parent['id'])['receipt'])['command_failures'],1)
        child=self.task(base=result['source']['head_sha'],dependencies=[parent['id']]);self.tick()
        self.assertEqual(self.core.get(self.manager,child['id'])['state'],'development_queued')
    def test_malicious_local_clean_filter_is_rejected_before_observe_or_collect(self):
        task=self.task();self.tick();self.tick()
        sentinel=self.root/'FILTER_MUST_NOT_RUN'
        # Configure only after the real synthetic turn: collect must reject it
        # before status/diff/add can invoke the configured process on the host.
        (self.source/'.gitattributes').write_text('src/main.py filter=malicious\n')
        subprocess.run(['/usr/bin/git','-C',str(self.source),'config','filter.malicious.clean','touch '+str(sentinel)],check=True,stdout=subprocess.DEVNULL)
        self.tick()
        self.assertFalse(sentinel.exists())
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'developing')
        self.assertEqual(self.host._row(task['id'])['phase'],'executed')
        head=subprocess.run(['/usr/bin/git','-C',str(self.source),'rev-parse','HEAD'],check=True,capture_output=True,text=True).stdout.strip()
        self.assertEqual(head,self.base)
    def test_worktree_specific_filter_is_rejected_without_execution(self):
        task=self.task();self.tick();self.tick();sentinel=self.root/'WORKTREE_FILTER_MUST_NOT_RUN'
        (self.source/'.gitattributes').write_text('src/main.py filter=malicious\n')
        subprocess.run(['/usr/bin/git','-C',str(self.source),'config','extensions.worktreeConfig','true'],check=True,stdout=subprocess.DEVNULL)
        subprocess.run(['/usr/bin/git','-C',str(self.source),'config','--worktree','filter.malicious.clean','touch '+str(sentinel)],check=True,stdout=subprocess.DEVNULL)
        self.tick()
        self.assertFalse(sentinel.exists());self.assertEqual(self.host._row(task['id'])['phase'],'executed')
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'developing')
    def deferred(self):
        provider=FleetProvider(self.transport.discover,execution_provisioning=True,delivery_observer=self.transport.delivery,reserved_execution_observer=self.transport.reserved_observer)
        fleet=Fleet(self.store,self.registry,providers={('project','native-host'):provider})
        fleet.register_host(self.owner,'project','native-host');fleet.register_template(self.owner,'project','native-template','native-host',max_sessions=4,expires_at=self.now+3600)
        for key,value in [('session_id',None),('worker_mode','deferred_native'),('fleet_host_ref','native-host'),('fleet_template','native-template')]:object.__setattr__(self.project,key,value)
        self.host.fleet=fleet
        return fleet
    def test_deferred_reservation_does_not_claim_receiver_until_actual_execution(self):
        fleet=self.deferred();task=self.task();self.tick()
        self.assertEqual(self.transport.native_calls,0)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT state FROM fleet_creates').fetchone()[0],'reserved')
            self.assertEqual(c.execute('SELECT count(*) FROM fleet_sessions').fetchone()[0],0)
        with patch.object(FixedReviewRunner,'__call__',lambda runner,candidate,reviewer,**kw:self.mechanical(runner,candidate,reviewer,**kw)):
            for _ in range(7):self.tick()
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'accepted');self.assertEqual(self.transport.native_calls,1)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT session_id FROM continuous_host_sessions').fetchone()[0],SESSION)
        child=self.task(base=self.host._row(task['id'])['candidate_head'],dependencies=[task['id']])
        with patch.object(FixedReviewRunner,'__call__',lambda runner,candidate,reviewer,**kw:self.mechanical(runner,candidate,reviewer,**kw)):
            for _ in range(8):self.tick()
        self.assertEqual(self.core.get(self.manager,child['id'])['state'],'accepted')
        self.assertEqual(self.transport.native_calls,1);self.assertEqual(self.transport.calls,2)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT count(*) FROM fleet_creates').fetchone()[0],1)
    def test_unknown_deferred_execution_survives_restart_without_second_native_turn(self):
        self.deferred();task=self.task();self.transport.fail=True
        self.tick();self.tick();self.now+=31;self.scheduler=ContinuousManager(self.core,handlers=self.host.handlers())
        for _ in range(4):self.tick()
        self.assertEqual(self.transport.native_calls,1)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT state FROM fleet_creates').fetchone()[0],'executing')
        self.assertEqual(self.host._row(task['id'])['phase'],'uncertain')
    def test_explicit_metadata_only_partial_start_recovers_with_new_template_no_native_replay(self):
        task=self.task();created=[]
        provider=FleetProvider(lambda _:(),lambda project,public,key:(created.append(key) or self.transport.discover(project)[0]))
        fleet=Fleet(self.store,self.registry,providers={('project','old-host'):provider})
        fleet.register_host(self.owner,'project','old-host');fleet.register_template(self.owner,'project','old-template','old-host',max_sessions=1,expires_at=self.now+3600)
        for key,value in [('session_id',None),('fleet_host_ref','old-host'),('fleet_template','old-template')]:object.__setattr__(self.project,key,value)
        self.host.fleet=fleet
        # Metadata-only create succeeds but the process-scoped receiver vanishes.
        with patch.object(self.transport,'discover',side_effect=[(SessionObservation('project',SESSION,SESSION,'codex_cli','fixture/project',self.base,True,'fixture/ephemeral',self.now),),()]):self.tick()
        with self.store._transaction() as c:action=c.execute("SELECT action_key FROM continuous_actions WHERE phase='start'").fetchone()[0]
        self.assertEqual(len(created),1);self.assertEqual(self.transport.calls,0)
        newfleet=self.deferred();self.now+=31
        result=self.scheduler.reconcile(self.manager,['project'],'fixture-instance',action)
        self.assertEqual(result['actions'][0]['state'],'confirmed')
        self.assertEqual(self.core.get(self.manager,task['id'])['state'],'development_queued');self.assertEqual(self.transport.calls,0)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM fleet_creates').fetchone()[0],2)
            self.assertEqual(c.execute("SELECT count(*) FROM fleet_creates WHERE state='created'").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT state FROM continuous_actions WHERE action_key=?",(action,)).fetchone()[0],'reconciled')
    def test_start_wrong_source_base_has_no_execution(self):
        task=self.task(base='a'*40)
        for _ in range(2):self.tick()
        self.assertEqual(self.transport.calls,0)

if __name__=='__main__':unittest.main()
