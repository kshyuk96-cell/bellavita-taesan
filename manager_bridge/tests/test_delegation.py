from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from manager_bridge.coordination import Coordination
from manager_bridge.delegation import Delegation, SourceProof, validate_execution
from manager_bridge.maintenance_registry import Registry
from manager_bridge.store import Store, Conflict, PermissionDenied, token_hash, digest

BASE='a'*40
CHECKS=['authorization_regression','approval_regression','unit']


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory())
        self.now=1000.0
        self.store=Store(Path(self.tmp)/'private.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        self.config={'company':'bellavita','name':'Fixture ERP','source_kind':'github','source_ref':'fixture/erp','branch':'main','services':[],'maintenance_enabled':True}
        self.registry.register(self.owner,'erp',self.config)
        self.registry.register(self.owner,'hope',{**self.config,'company':'hope','name':'Hope','source_ref':'fixture/hope'})
        self.manager=self.store.add_token('taesan','manager',['erp'])
        self.manager2=self.store.add_token('other-manager','manager',['erp'])
        self.worker=self.store.add_token('developer','worker',['erp'])
        self.proof=SourceProof('erp','fixture/erp','main',BASE,'fixture/source',self.now)
        self.calls=[]
        self.callback=self.observe
        self.delegation=Delegation(self.store,self.registry,source_observers={'erp':lambda p:self.callback(p)})
        self.sequence=0

    def observe(self,project):
        self.calls.append(project)
        return self.proof

    def grant(self,**changes):
        args={'allowed_paths':['src/','tests/'],'allowed_checks':CHECKS,'kinds':['development','test'],'max_tasks':5,'expires_at':self.now+1000}
        args.update(changes)
        value=self.delegation.propose(self.owner,'erp',token_hash(self.manager),**args)
        return self.delegation.activate(self.owner,value['id'],value['fingerprint'])

    def task(self,**changes):
        self.sequence+=1
        args={'project':'erp','repository':'fixture/erp','base_sha':BASE,'description':'Fixture source change','checks':CHECKS,'kind':'development'}
        args.update(changes)
        return self.store.submit(self.owner,f'task-{self.sequence}',args)

    def authorize(self,grant=None,task=None,**changes):
        grant=grant or self.grant();task=task or self.task()
        kwargs={'requested_paths':['src/a.py'],'dependencies':[]};kwargs.update(changes)
        return self.delegation.authorize(self.manager,grant['id'],task['id'],task['version'],task['manifest_digest'],**kwargs)

    def validate(self,task,**kwargs):
        with self.store._transaction() as c:
            row=c.execute('SELECT * FROM bridge_tasks WHERE id=?',(task['id'],)).fetchone()
            return validate_execution(c,self.store,row,**kwargs)

    def route_claim(self,task):
        coordination=Coordination(self.store)
        coordination.readiness(self.manager,token_hash(self.worker),ready=True,reason='ready')
        coordination.route(self.manager,task['id'])
        return self.store.claim(self.worker,'erp',task_id=task['id'])

    def test_owner_exact_activation_required(self):
        proposal=self.delegation.propose(self.owner,'erp',token_hash(self.manager),allowed_paths=['src/'],allowed_checks=CHECKS,kinds=['test'],max_tasks=1,expires_at=2000)
        task=self.task(kind='test')
        with self.assertRaises(PermissionDenied):self.authorize(proposal,task)
        with self.assertRaises(Conflict):self.delegation.activate(self.owner,proposal['id'],'f'*64)
        self.assertEqual(self.delegation.activate(self.owner,proposal['id'],proposal['fingerprint'])['state'],'active')

    def test_manager_cannot_create_or_activate_own_grant(self):
        with self.assertRaises(PermissionDenied):self.delegation.propose(self.manager,'erp',token_hash(self.manager),allowed_paths=['src/'],allowed_checks=CHECKS,kinds=['test'],max_tasks=1,expires_at=2000)
        grant=self.grant()
        with self.assertRaises(PermissionDenied):self.delegation.activate(self.manager,grant['id'],grant['fingerprint'])

    def test_routine_task_queues_without_owner_token_in_approval(self):
        grant=self.grant();task=self.authorize(grant)
        self.assertEqual(task['state'],'queued')
        approval=task['approval']
        self.assertEqual(approval['delegation_id'],grant['id'])
        self.assertEqual(approval['delegation_digest'],grant['fingerprint'])
        self.assertEqual(approval['manager_hash'],token_hash(self.manager))
        serialized=json.dumps(task)
        self.assertNotIn(self.owner,serialized);self.assertNotIn(self.manager,serialized)
        self.assertTrue(self.validate(task))
        policy=Coordination(self.store).get_policy(self.manager,task['id'])
        self.assertEqual(policy['allowed_paths'],['src/a.py'])
        self.assertIs(policy['routing_required'],True)

    def test_regular_owner_approval_unaffected(self):
        task=self.task()
        task=self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'])
        self.assertTrue(self.validate(task))

    def test_wrong_manager_hash_cannot_use_delegation(self):
        grant=self.grant();task=self.task()
        with self.assertRaises(PermissionDenied):self.delegation.authorize(self.manager2,grant['id'],task['id'],1,task['manifest_digest'],requested_paths=['src/a.py'])
        self.assertEqual(self.calls,[])

    def test_other_company_task_cannot_use_delegation(self):
        grant=self.grant();task=self.task(project='hope',repository='fixture/hope')
        with self.assertRaises(LookupError):self.authorize(grant,task)
        self.assertEqual(self.calls,[])

    def test_operation_kinds_not_supported(self):
        for kind in ('deploy','merge','payment','message','production_write'):
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.grant(kinds=[kind])

    def test_disallowed_path_and_path_traversal_blocked(self):
        for paths in (['config/prod.yaml'],['src/../prod.py'],['/etc/passwd'],['src/*']):
            with self.subTest(paths=paths),self.assertRaises((PermissionDenied,ValueError)):
                self.authorize(requested_paths=paths)

    def test_dropping_or_escalating_checks_rejected(self):
        for checks in (['unit'],CHECKS+['deploy']):
            with self.subTest(checks=checks),self.assertRaises(PermissionDenied):self.authorize(task=self.task(checks=checks))

    def test_mandatory_check_policy_cannot_be_omitted(self):
        with self.assertRaises(ValueError):self.grant(allowed_checks=['unit'])

    def test_exact_version_and_digest(self):
        grant=self.grant();task=self.task()
        for version,fingerprint in ((2,task['manifest_digest']),(1,'f'*64)):
            with self.subTest(version=version),self.assertRaises(Conflict):self.delegation.authorize(self.manager,grant['id'],task['id'],version,fingerprint,requested_paths=['src/a.py'])

    def test_disconnected_source_provider_does_not_queue(self):
        grant=self.grant();task=self.task()
        disconnected=Delegation(self.store,self.registry)
        with self.assertRaises(Conflict):disconnected.authorize(self.manager,grant['id'],task['id'],1,task['manifest_digest'],requested_paths=['src/a.py'])
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'pending_approval')

    def test_source_scope_branch_commit_mismatch_blocked(self):
        for changes in ({'project':'hope'},{'source_ref':'fixture/other'},{'branch':'other'},{'sha':'b'*40}):
            self.proof=replace(self.proof,**changes)
            with self.subTest(changes=changes),self.assertRaises(Conflict):self.authorize()
            self.proof=SourceProof('erp','fixture/erp','main',BASE,'fixture/source',self.now)

    def test_stale_or_future_source_proof_blocked(self):
        for time in (1,1001):
            self.proof=replace(self.proof,observed_at=time)
            with self.subTest(time=time),self.assertRaises(Conflict):self.authorize()

    def test_callback_failure_redacted(self):
        def failed(_):raise RuntimeError('private-provider-key')
        self.callback=failed
        with self.assertRaises(Conflict) as error:self.authorize()
        self.assertNotIn('private',str(error.exception))

    def test_callback_cannot_return_arbitrary_dict(self):
        self.callback=lambda _: {'sha':BASE}
        with self.assertRaises(Conflict):self.authorize()

    def test_revoked_manager_during_source_read_cannot_queue(self):
        grant=self.grant();task=self.task()
        def revoke(_):
            self.store.revoke_token(self.owner,token_hash(self.manager));return self.proof
        self.callback=revoke
        with self.assertRaises(PermissionDenied):self.authorize(grant,task)
        self.assertEqual(self.store.get_task(self.owner,task['id'])['state'],'pending_approval')

    def test_configuration_change_during_source_read_blocks(self):
        grant=self.grant();task=self.task()
        def change(_):
            self.registry.register(self.owner,'erp',{**self.config,'name':'changed'},expected_revision=1);return self.proof
        self.callback=change
        with self.assertRaises(Conflict):self.authorize(grant,task)

    def test_idempotency_does_not_consume_budget_twice(self):
        grant=self.grant(max_tasks=1);task=self.task()
        first=self.authorize(grant,task);second=self.authorize(grant,task)
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(self.delegation.get(self.manager,grant['id'])['used'],1)
        with self.assertRaises(PermissionDenied):self.authorize(grant,self.task())

    def test_concurrent_budget_has_one_winner(self):
        grant=self.grant(max_tasks=1);tasks=[self.task(),self.task()]
        def attempt(task):
            try:self.authorize(grant,task);return True
            except PermissionDenied:return False
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(attempt,tasks))
        self.assertEqual(sum(result),1)
        self.assertEqual(self.delegation.get(self.manager,grant['id'])['used'],1)

    def test_approved_path_cannot_be_widened_on_replay(self):
        grant=self.grant();task=self.task();self.authorize(grant,task)
        with self.assertRaises(Conflict):self.authorize(grant,task,requested_paths=['src/'])

    def test_expired_grant_blocks_authorize_and_execution(self):
        grant=self.grant();task=self.authorize(grant)
        self.now=2001
        with self.assertRaises(PermissionDenied):self.validate(task,check_source_age=False)
        with self.assertRaises(PermissionDenied):self.authorize(grant,self.task())

    def test_source_age_enforced_on_claim_but_not_running_duration(self):
        task=self.authorize()
        self.now+=301
        with self.assertRaises(Conflict):self.validate(task)
        self.assertTrue(self.validate(task,check_source_age=False))

    def test_refresh_same_task_source_proof_without_budget(self):
        grant=self.grant(max_tasks=1);task=self.task();self.authorize(grant,task)
        self.now+=301;self.proof=replace(self.proof,observed_at=self.now)
        current=self.authorize(grant,task)
        self.assertTrue(self.validate(current))
        self.assertEqual(self.delegation.get(self.manager,grant['id'])['used'],1)

    def test_project_configuration_change_invalidates_existing_grant(self):
        grant=self.grant();task=self.authorize(grant)
        self.registry.register(self.owner,'erp',{**self.config,'name':'changed'},expected_revision=1)
        with self.assertRaises(Conflict):self.validate(task)

    def test_disabled_project_cannot_delegate_execution(self):
        self.registry.register(self.owner,'erp',{**self.config,'maintenance_enabled':False},expected_revision=1)
        with self.assertRaises(PermissionDenied):self.authorize()

    def test_owner_revocation_blocks_delegation(self):
        grant=self.grant();task=self.authorize(grant)
        second=self.store.add_token('second-owner','owner',['*'])
        self.store.revoke_token(second,token_hash(self.owner))
        with self.assertRaises(PermissionDenied):self.validate(task,check_source_age=False)

    def test_manager_revocation_blocks_delegation(self):
        task=self.authorize()
        self.store.revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(PermissionDenied):self.validate(task,check_source_age=False)

    def test_revoke_cancels_queue_and_does_not_confirm_worker_stop(self):
        grant=self.grant();task=self.authorize(grant)
        result=self.delegation.revoke(self.owner,grant['id'])
        self.assertEqual(result['queued_cancelled'],1)
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'cancelled')
        self.assertFalse(result['execution_stop_confirmed'])
        with self.assertRaises(Conflict):self.delegation.activate(self.owner,grant['id'],grant['fingerprint'])

    def test_revoke_running_preserves_repository_reservation(self):
        grant=self.grant();task=self.authorize(grant);self.route_claim(task)
        result=self.delegation.revoke(self.owner,grant['id'])
        self.assertEqual(result['needs_verification'],1)
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'needs_verification')
        with self.store._transaction() as c:self.assertIn('fixture/erp',self.store._reserved_repositories(c))

    def test_reviewed_candidate_not_rewritten_by_grant_revoke(self):
        grant=self.grant();task=self.authorize(grant)
        with self.store._transaction() as c:c.execute("UPDATE bridge_tasks SET state='reviewed' WHERE id=?",(task['id'],))
        self.delegation.revoke(self.owner,grant['id'])
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'reviewed')

    def test_tampered_approval_or_missing_usage_blocked(self):
        task=self.authorize()
        with self.store._transaction() as c:c.execute('DELETE FROM management_delegation_usage WHERE task=?',(task['id'],))
        with self.assertRaises(Conflict):self.validate(task)

    def test_matching_existing_owner_policy_can_be_reused(self):
        grant=self.grant();task=self.task()
        Coordination(self.store).set_policy(self.owner,task['id'],1,task['manifest_digest'],allowed_paths=['src/a.py'],dependencies=[])
        result=self.authorize(grant,task)
        self.assertEqual(result['state'],'queued')
        self.assertTrue(self.validate(result))

    def test_owner_path_restriction_cannot_be_widened_by_delegate(self):
        grant=self.grant();task=self.task()
        Coordination(self.store).set_policy(self.owner,task['id'],1,task['manifest_digest'],allowed_paths=['src/a.py'],dependencies=[])
        with self.assertRaises(Conflict):self.authorize(grant,task,requested_paths=['src/'])
        self.assertEqual(self.delegation.get(self.manager,grant['id'])['used'],0)
        self.assertEqual(self.store.get_task(self.manager,task['id'])['state'],'pending_approval')

    def test_expired_grant_is_blocked_by_store_claim(self):
        grant=self.grant(expires_at=1005);task=self.authorize(grant)
        coordination=Coordination(self.store)
        coordination.readiness(self.manager,token_hash(self.worker),ready=True,reason='ready')
        coordination.route(self.manager,task['id'])
        self.now=1006
        self.assertIsNone(self.store.claim(self.worker,'erp',task_id=task['id']))

    def test_stale_source_claim_waits_for_manager_refresh(self):
        grant=self.grant();task=self.authorize(grant)
        self.now+=301
        coordination=Coordination(self.store)
        coordination.readiness(self.manager,token_hash(self.worker),ready=True,reason='ready')
        coordination.route(self.manager,task['id'])
        self.assertIsNone(self.store.claim(self.worker,'erp',task_id=task['id']))
        self.proof=replace(self.proof,observed_at=self.now)
        self.authorize(grant,task)
        self.assertIsNotNone(self.store.claim(self.worker,'erp',task_id=task['id']))

    def test_revoked_grant_prevents_late_heartbeat_and_report(self):
        grant=self.grant();task=self.authorize(grant);assignment=self.route_claim(task)
        self.delegation.revoke(self.owner,grant['id'])
        with self.assertRaises(PermissionDenied):self.store.heartbeat(self.worker,task['id'],assignment['lease_token'])
        with self.assertRaises(PermissionDenied):self.store.report(self.worker,task['id'],assignment['lease_token'],{'head_sha':BASE,'summary':'late claim','checks':[{'name':'unit','passed':True}]})

    def test_project_scope_change_blocks_worker_heartbeat(self):
        task=self.authorize();assignment=self.route_claim(task)
        self.registry.register(self.owner,'erp',{**self.config,'name':'changed'},expected_revision=1)
        with self.assertRaises(Conflict):self.store.heartbeat(self.worker,task['id'],assignment['lease_token'])

    def test_running_worker_can_refresh_lease_after_source_proof_age(self):
        task=self.authorize();assignment=self.route_claim(task)
        self.store.heartbeat(self.worker,task['id'],assignment['lease_token'],lease_seconds=1000)
        self.now+=301
        result=self.store.heartbeat(self.worker,task['id'],assignment['lease_token'])
        self.assertFalse(result['cancel_requested'])

    def test_no_delegated_lease_check_without_transaction(self):
        task=self.authorize();assignment=self.route_claim(task)
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,self.worker)
            row=c.execute('SELECT * FROM bridge_tasks WHERE id=?',(task['id'],)).fetchone()
            with self.assertRaises(Conflict):self.store._lease(row,actor,assignment['lease_token'])

    def test_budget_receipt_corruption_cannot_execute(self):
        grant=self.grant();task=self.authorize(grant)
        with self.store._transaction() as c:c.execute('UPDATE management_delegations SET used=0 WHERE id=?',(grant['id'],))
        with self.assertRaises(Conflict):self.validate(task)

    def test_reopen_store_retains_grant_budget_and_approval(self):
        grant=self.grant();task=self.authorize(grant)
        reopened=Store(self.store.path,clock=lambda:self.now)
        delegated=Delegation(reopened,Registry(reopened),source_observers={'erp':self.observe})
        self.assertEqual(delegated.get(self.manager,grant['id'])['used'],1)
        self.assertEqual(delegated.authorize(self.manager,grant['id'],task['id'],1,task['manifest_digest'],requested_paths=['src/a.py'])['state'],'queued')
