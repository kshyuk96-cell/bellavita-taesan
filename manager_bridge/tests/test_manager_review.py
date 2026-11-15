from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from manager_bridge.store import Store, Conflict, NotFound, PermissionDenied, token_hash, digest
from manager_bridge.maintenance_registry import Registry
from manager_bridge.coordination import Coordination
from manager_bridge.manager_review import (VerificationHost, ChangeObservation, ChangeProbe,
                                          ManagerReview, DecisionPackets)
from manager_bridge.supervisor import VerificationBlocked


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory())
        self.now=1000
        self.store=Store(Path(self.tmp)/'state.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        self.config={'company':'company-a','name':'Project A','source_kind':'github','source_ref':'fixture/a',
                     'branch':'main','services':[],'maintenance_enabled':False}
        self.registry.register(self.owner,'a',self.config)
        self.registry.register(self.owner,'b',{**self.config,'company':'company-b','name':'Project B','source_ref':'fixture/b'})
        self.manager=self.store.add_token('manager','manager',['a'])
        self.global_manager=self.store.add_token('main-manager','manager',['a','b'])
        self.worker=self.store.add_token('worker','worker',['a'])
        self.packets=DecisionPackets(self.store,self.registry)
        self.count=0

    def task(self,project='a',approve=False):
        self.count+=1
        result=self.store.submit(self.owner,f'request-{self.count}',{'project':project,'repository':'fixture/'+project,
            'base_sha':'a'*40,'description':'synthetic development','checks':['authorization'],'kind':'test'})
        if approve:result=self.store.approve(self.owner,result['id'],result['version'],result['manifest_digest'])
        return result

    def observation(self,project='a',**changes):
        value=ChangeObservation(project,'fixture/'+project,'a'*40,'b'*40,('src/app.py',),False,'fixture/source',self.now)
        return replace(value,**changes)

    def test_two_project_observation_scopes_and_no_merge(self):
        review=ManagerReview(self.store,self.registry,change_observers={p:ChangeProbe(lambda p:self.observation(p)) for p in ('a','b')})
        result=review.changes(self.global_manager,['a','b'])
        self.assertTrue(result['all_observed'])
        self.assertFalse(result['production_merge_performed'])
        self.assertFalse(result['deployment_approved'])

    def test_missing_or_unknown_conflict_is_not_measured(self):
        for observers in ({},{'a':ChangeProbe(lambda _:self.observation(conflict=None))}):
            result=ManagerReview(self.store,self.registry,change_observers=observers).changes(self.manager,['a'])
            self.assertFalse(result['all_observed'])
            self.assertEqual(result['projects'][0]['state'],'not_measured')

    def test_conflict_is_visible(self):
        result=ManagerReview(self.store,self.registry,change_observers={'a':ChangeProbe(lambda _:self.observation(conflict=True))}).changes(self.manager,['a'])
        self.assertEqual(result['projects'][0]['state'],'conflict')
        self.assertFalse(result['all_observed'])

    def test_wrong_project_repository_stale_future_or_bad_provider_not_measured(self):
        for observed in (self.observation('b'),self.observation(repository='other/repo'),
                         self.observation(observed_at=1),self.observation(observed_at=1001),{'conflict':False}):
            with self.subTest(observed=type(observed)):
                result=ManagerReview(self.store,self.registry,change_observers={'a':ChangeProbe(lambda _:observed)}).changes(self.manager,['a'])
                self.assertEqual(result['projects'][0]['state'],'not_measured')
                self.assertNotIn('other/repo',json.dumps(result))

    def test_all_project_permissions_checked_before_any_observer(self):
        calls=[]
        review=ManagerReview(self.store,self.registry,change_observers={'a':ChangeProbe(lambda _:calls.append('called'))})
        with self.assertRaises(NotFound):review.changes(self.manager,['a','b'])
        self.assertEqual(calls,[])

    def test_revocation_before_next_observer_stops_collection(self):
        calls=[]
        def first(_):
            self.store.revoke_token(self.owner,token_hash(self.global_manager));return self.observation()
        review=ManagerReview(self.store,self.registry,change_observers={'a':ChangeProbe(first),'b':ChangeProbe(lambda _:calls.append('called'))})
        with self.assertRaises(PermissionDenied):review.changes(self.global_manager,['a','b'])
        self.assertEqual(calls,[])

    def test_config_change_invalidates_observation(self):
        def observe(_):
            self.registry.register(self.owner,'a',{**self.config,'name':'changed'},expected_revision=1)
            return self.observation()
        with self.assertRaises(Conflict):
            ManagerReview(self.store,self.registry,change_observers={'a':ChangeProbe(observe)}).changes(self.manager,['a'])

    def test_provider_error_does_not_expose_private_body(self):
        def failed(_):raise RuntimeError('private-key-value')
        result=ManagerReview(self.store,self.registry,change_observers={'a':ChangeProbe(failed)}).changes(self.manager,['a'])
        self.assertNotIn('private-key-value',json.dumps(result))

    def test_probe_and_path_types_strict(self):
        with self.assertRaises(ValueError):ChangeProbe(lambda _:None,False)
        for fields in ({'conflict':'false'},{'paths':('../outside',)},{'head_sha':'bad'}):
            with self.subTest(fields=fields),self.assertRaises(ValueError):self.observation(**fields)

    def test_blocker_reuses_known_classification(self):
        task=self.task()
        result=ManagerReview(self.store,self.registry).blocker(self.manager,task['id'],'sandbox_unavailable')
        self.assertTrue(result['manager_can_check'])
        self.assertFalse(result['automatically_replayed'])
        self.assertFalse(result['reassignment_allowed'])

    def test_started_cancelled_work_requires_stop_confirmation(self):
        task=self.task(approve=True)
        self.store.claim(self.worker,'a')
        self.store.cancel(self.manager,task['id'])
        result=ManagerReview(self.store,self.registry).blocker(self.manager,task['id'],'sandbox_unavailable')
        self.assertTrue(result['requires_stop_confirmation'])
        self.assertFalse(result['manager_can_check'])

    def test_unknown_failure_does_not_copy_raw_input(self):
        task=self.task()
        result=ManagerReview(self.store,self.registry).blocker(self.manager,task['id'],'private token text')
        self.assertEqual(result['category'],'unknown')
        self.assertNotIn('private token',json.dumps(result))

    def test_packet_approves_two_projects_with_one_exact_owner_decision(self):
        tasks=[self.task(),self.task('b')]
        packet=self.packets.prepare(self.global_manager,'bundle',[x['id'] for x in tasks])
        result=self.packets.decide(self.owner,packet['id'],packet['fingerprint'])
        self.assertEqual(set(result['changed_tasks']),{x['id'] for x in tasks})
        self.assertFalse(result['operational_approval'])
        for task in tasks:
            actual=self.store.get_task(self.owner,task['id'])
            self.assertEqual(actual['state'],'queued')
            self.assertEqual(actual['approval']['decision_packet'],packet['id'])

    def test_manager_or_worker_cannot_make_owner_decision(self):
        task=self.task();packet=self.packets.prepare(self.manager,'bundle',[task['id']])
        for token in (self.manager,self.worker):
            with self.subTest(token_role=token==self.worker),self.assertRaises(PermissionDenied):
                self.packets.decide(token,packet['id'],packet['fingerprint'])
        self.assertEqual(self.store.get_task(self.owner,task['id'])['state'],'pending_approval')

    def test_packet_foreign_scope_cannot_be_prepared_or_read(self):
        a,b=self.task(),self.task('b')
        with self.assertRaises(NotFound):self.packets.prepare(self.manager,'bad',[a['id'],b['id']])
        packet=self.packets.prepare(self.global_manager,'global',[a['id'],b['id']])
        with self.assertRaises(NotFound):self.packets.get(self.manager,packet['id'])

    def test_changed_second_task_blocks_entire_batch(self):
        first,second=self.task(),self.task()
        packet=self.packets.prepare(self.manager,'bundle',[first['id'],second['id']])
        self.store.cancel(self.manager,second['id'])
        with self.assertRaises(Conflict):self.packets.decide(self.owner,packet['id'],packet['fingerprint'])
        self.assertEqual(self.store.get_task(self.owner,first['id'])['state'],'pending_approval')
        self.assertTrue(self.packets.get(self.manager,packet['id'])['stale'])

    def test_configuration_or_file_policy_change_blocks_packet(self):
        for change in ('config','policy'):
            with self.subTest(change=change):
                task=self.task();packet=self.packets.prepare(self.manager,'bundle-'+change,[task['id']])
                if change=='config':
                    current=self.registry.get(self.owner,'a')
                    self.registry.register(self.owner,'a',{**current['config'],'name':'new'},expected_revision=current['revision'])
                else:Coordination(self.store).set_policy(self.owner,task['id'],task['version'],task['manifest_digest'],allowed_paths=['src/'],dependencies=[])
                with self.assertRaises(Conflict):self.packets.decide(self.owner,packet['id'],packet['fingerprint'])
                self.assertEqual(self.store.get_task(self.owner,task['id'])['state'],'pending_approval')

    def test_packet_expiry_and_wrong_digest_do_not_approve(self):
        task=self.task();packet=self.packets.prepare(self.manager,'bundle',[task['id']],ttl=60)
        with self.assertRaises(Conflict):self.packets.decide(self.owner,packet['id'],'f'*64)
        self.now+=61
        with self.assertRaises(Conflict):self.packets.decide(self.owner,packet['id'],packet['fingerprint'])
        self.assertEqual(self.store.get_task(self.owner,task['id'])['state'],'pending_approval')

    def test_duplicate_owner_decision_after_restart_is_receipt_not_reapproval(self):
        task=self.task();packet=self.packets.prepare(self.manager,'bundle',[task['id']])
        result=self.packets.decide(self.owner,packet['id'],packet['fingerprint'])
        reopened_store=Store(self.store.path,clock=lambda:self.now)
        reopened=DecisionPackets(reopened_store,Registry(reopened_store))
        self.assertEqual(reopened.decide(self.owner,packet['id'],packet['fingerprint']),result)
        with self.assertRaises(Conflict):reopened.decide(self.owner,packet['id'],packet['fingerprint'],decision='reject')

    def test_reject_cancels_only_unstarted_pending_requests(self):
        pending,queued=self.task(),self.task(approve=True)
        packet=self.packets.prepare(self.manager,'bundle',[pending['id'],queued['id']])
        result=self.packets.decide(self.owner,packet['id'],packet['fingerprint'],decision='reject')
        self.assertEqual(result['changed_tasks'],[pending['id']])
        self.assertEqual(self.store.get_task(self.owner,pending['id'])['state'],'cancelled')
        self.assertEqual(self.store.get_task(self.owner,queued['id'])['state'],'queued')

    def test_already_queued_routine_tasks_are_informational(self):
        task=self.task(approve=True)
        packet=self.packets.prepare(self.manager,'bundle',[task['id']])
        self.assertEqual(packet['approval_required_tasks'],[])
        self.assertEqual(packet['informational_tasks'],[task['id']])
        result=self.packets.decide(self.owner,packet['id'],packet['fingerprint'])
        self.assertEqual(result['changed_tasks'],[])

    def test_request_id_conflict_and_duplicate_task_input(self):
        first,second=self.task(),self.task()
        packet=self.packets.prepare(self.manager,'same',[first['id']])
        self.assertEqual(self.packets.prepare(self.manager,'same',[first['id']])['id'],packet['id'])
        with self.assertRaises(Conflict):self.packets.prepare(self.manager,'same',[second['id']])
        with self.assertRaises(ValueError):self.packets.prepare(self.manager,'dup',[first['id'],first['id']])

    def test_revoked_owner_cannot_decide_packet(self):
        other_owner=self.store.add_token('other-owner','owner',['*'])
        task=self.task();packet=self.packets.prepare(self.manager,'bundle',[task['id']])
        self.store.revoke_token(other_owner,token_hash(self.owner))
        with self.assertRaises(PermissionDenied):self.packets.decide(self.owner,packet['id'],packet['fingerprint'])

    def test_tokens_not_in_persisted_packet_or_decision(self):
        task=self.task()
        with self.assertRaises(ValueError):self.packets.prepare(self.manager,self.manager,[task['id']])
        packet=self.packets.prepare(self.manager,'bundle',[task['id']])
        result=self.packets.decide(self.owner,packet['id'],packet['fingerprint'])
        with self.store._transaction() as c:body=c.execute('SELECT body,result FROM manager_decision_packets').fetchone()
        for token in (self.owner,self.manager,self.worker):
            self.assertNotIn(token,body['body']+(body['result'] or '')+json.dumps(result))

    def test_concurrent_identical_decision_emits_one_approval_per_task(self):
        first,second=self.task(),self.task()
        packet=self.packets.prepare(self.manager,'bundle',[first['id'],second['id']])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.packets.decide(self.owner,packet['id'],packet['fingerprint']),range(2)))
        self.assertEqual(results[0],results[1])
        with self.store._transaction() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bridge_events WHERE action='packet_approved'").fetchone()[0],2)

    def test_mid_transaction_failure_rolls_back_all_approvals(self):
        from unittest.mock import patch
        first,second=self.task(),self.task()
        packet=self.packets.prepare(self.manager,'bundle',[first['id'],second['id']])
        original=self.store._event
        count=[0]
        def fail_second(*args,**kwargs):
            count[0]+=1
            if count[0]==2:raise RuntimeError('synthetic database event failure')
            return original(*args,**kwargs)
        with patch.object(self.store,'_event',side_effect=fail_second):
            with self.assertRaises(RuntimeError):self.packets.decide(self.owner,packet['id'],packet['fingerprint'])
        for task in (first,second):self.assertEqual(self.store.get_task(self.owner,task['id'])['state'],'pending_approval')
        self.assertEqual(self.packets.get(self.manager,packet['id'])['state'],'pending')

    def test_delegated_task_has_no_extra_owner_approval_and_expiry_stales_packet(self):
        from manager_bridge.delegation import Delegation, SourceProof
        checks=['authorization_regression','approval_regression']
        self.registry.register(self.owner,'a',{**self.config,'maintenance_enabled':True},expected_revision=1)
        delegate=Delegation(self.store,self.registry,source_observers={'a':lambda _:SourceProof('a','fixture/a','main','a'*40,'fixture/current',self.now)})
        grant=delegate.propose(self.owner,'a',token_hash(self.manager),allowed_paths=['src/'],allowed_checks=checks,kinds=['test'],max_tasks=3,expires_at=self.now+1000)
        delegate.activate(self.owner,grant['id'],grant['fingerprint'])
        task=self.store.submit(self.manager,'delegated',{'project':'a','repository':'fixture/a','base_sha':'a'*40,
            'description':'synthetic delegated tests','checks':checks,'kind':'test'})
        delegate.authorize(self.manager,grant['id'],task['id'],task['version'],task['manifest_digest'],requested_paths=['src/a.py'])
        pending=self.task()
        packet=self.packets.prepare(self.manager,'bundle',[task['id'],pending['id']])
        self.assertEqual(packet['approval_required_tasks'],[pending['id']])
        routine=next(x for x in packet['tasks'] if x['id']==task['id'])
        self.assertTrue(routine['delegation']['valid'])
        self.now+=1
        delegate.authorize(self.manager,grant['id'],task['id'],task['version'],task['manifest_digest'],requested_paths=['src/a.py'])
        self.assertFalse(self.packets.get(self.manager,packet['id'])['stale'])
        self.now+=1001
        with self.assertRaises(Conflict):self.packets.decide(self.owner,packet['id'],packet['fingerprint'])
        self.assertEqual(self.store.get_task(self.owner,pending['id'])['state'],'pending_approval')

    def test_only_development_decisions_supported(self):
        task=self.task();packet=self.packets.prepare(self.manager,'bundle',[task['id']])
        with self.assertRaises(ValueError):self.packets.decide(self.owner,packet['id'],packet['fingerprint'],decision='deploy')


class ActualIndependentReview(unittest.TestCase):
    def fixture(self):
        from manager_bridge.tests import test_supervisor
        fixture=test_supervisor.SupervisorTests('test_independent_commands_artifacts_and_exact_report_are_reviewed')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        registry=Registry(fixture.store)
        registry.register(fixture.owner,'a',{'company':'fixture','name':'Fixture review','source_kind':'github',
            'source_ref':'fixture/repo','branch':'main','services':[],'maintenance_enabled':False})
        return fixture,registry

    def test_adapter_runs_actual_independent_commands_and_artifact_validation(self):
        fixture,registry=self.fixture();task,target=fixture.reported()
        host=VerificationHost(fixture.runner,fixture.commands())
        result=ManagerReview(fixture.store,registry,verification_hosts={'a':host}).verify(fixture.manager,task['id'])
        self.assertTrue(result['independently_verified'])
        self.assertEqual(result['state'],'reviewed')
        self.assertFalse(result['deployment_approved'])

    def test_worker_claim_and_missing_host_are_not_verification(self):
        fixture,registry=self.fixture();task,target=fixture.reported()
        result=ManagerReview(fixture.store,registry).verify(fixture.manager,task['id'])
        self.assertFalse(result['independently_verified'])
        self.assertEqual(result['state'],'not_measured')
        self.assertEqual(fixture.store.get_task(fixture.manager,task['id'])['state'],'reported')

    def test_changed_artifact_and_failed_independent_command_block_review(self):
        for mode in ('changed','failed'):
            with self.subTest(mode=mode):
                fixture,registry=self.fixture();task,target=fixture.reported()
                if mode=='changed':(target/'README.md').write_text('changed after worker report')
                commands=fixture.commands('raise RuntimeError("synthetic check failed")' if mode=='failed' else None)
                with self.assertRaises(VerificationBlocked):
                    ManagerReview(fixture.store,registry,verification_hosts={'a':VerificationHost(fixture.runner,commands)}).verify(fixture.manager,task['id'])
                self.assertEqual(fixture.store.get_task(fixture.manager,task['id'])['state'],'reported')

    def test_merge_rehearsal_can_detect_conflict_claim_contradiction_without_real_merge(self):
        fixture,registry=self.fixture()
        from manager_bridge.merge_rehearsal import MergeRehearsal
        base=fixture.base
        (fixture.repo/'new.txt').write_text('new synthetic change')
        fixture.git('add','new.txt');fixture.git('commit','-qm','candidate')
        head=fixture.git('rev-parse','HEAD')
        before=fixture.git('show-ref')
        observation=ChangeObservation('a','fixture/repo',base,head,('new.txt',),True,'fixture/rehearsal',1000)
        review=ManagerReview(fixture.store,registry,change_observers={'a':ChangeProbe(lambda _:observation)},
                             rehearsals={'a':MergeRehearsal({'a':{'repository':'fixture/repo','path':fixture.repo}})})
        result=review.changes(fixture.manager,['a'])
        self.assertEqual(result['projects'][0]['state'],'contradiction')
        self.assertFalse(result['all_observed'])
        self.assertEqual(fixture.git('show-ref'),before)
