"""Scoped fleet metadata and crash-safe provisioning with synthetic hosts."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from manager_bridge.fleet import Fleet,FleetProvider,SessionObservation
from manager_bridge.store import Store,Conflict,PermissionDenied,NotFound,token_hash,digest
from manager_bridge.maintenance_registry import Registry
from manager_bridge.coordination import Coordination


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.now=1000
        self.store=Store(self.root/'fleet.sqlite',clock=lambda:self.now);self.registry=Registry(self.store);self.coord=Coordination(self.store)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.config={'company':'fixture','name':'Fixture','source_kind':'github','source_ref':'fixture/project','branch':'main','services':[],'maintenance_enabled':True}
        for project in ('project','other'):self.registry.register(self.owner,project,{**self.config,'source_ref':'fixture/'+project})
        self.manager=self.store.add_token('manager','manager',['project','other']);self.limited=self.store.add_token('limited','manager',['project']);self.worker=self.store.add_token('worker','worker',['project'])
        self.items=(self.observation(),);self.discover_calls=0;self.create_calls=0;self.discovery=None;self.creator=None;self.count=0
        self.providers={('project','host'):FleetProvider(self.discover,self.create)}
        self.fleet=Fleet(self.store,self.registry,self.providers)
        self.fleet.register_host(self.owner,'project','host')

    def observation(self,**changes):
        return replace(SessionObservation('project','thread-one','thread-one','codex_cli','fixture/project','a'*40,True,'fixture/session',self.now),**changes)

    def discover(self,project):
        self.discover_calls+=1
        return self.discovery(project) if self.discovery else self.items

    def create(self,project,task,key):
        self.create_calls+=1
        if self.creator:return self.creator(project,task,key)
        return self.observation(external_ref='new-'+key[:8],session_id='new-'+key[:8],source_sha=task['manifest']['base_sha'])

    def template(self,cap=3):
        return self.fleet.register_template(self.owner,'project','template','host',max_sessions=cap,expires_at=self.now+600)

    def task(self,approved=True,policy=True,project='project'):
        self.count+=1
        task=self.store.submit(self.owner,'request-'+str(self.count),{'project':project,'repository':'fixture/'+project,'base_sha':'a'*40,'description':'Synthetic already approved development task.','checks':['unit'],'kind':'development'})
        p=None
        if policy:p=self.coord.set_policy(self.owner,task['id'],task['version'],task['manifest_digest'],allowed_paths=['src/'],dependencies=[])
        if approved:self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'],policy_digest=digest(p) if p else None)
        return task

    def create_task(self,task=None,key='create'):
        task=task or self.task()
        return self.fleet.create_for_task(self.manager,task['id'],'template',key)

    def test_explicit_registered_host_inventory_is_not_account_inventory(self):
        before=self.fleet.inventory(self.manager,['project']);self.assertEqual(before['projects'][0]['hosts'][0]['state'],'registered')
        self.fleet.discover(self.manager,'project','host');result=self.fleet.enroll(self.manager,'project','host','thread-one')
        self.assertEqual(result['state'],'ready');self.assertTrue(result['host_transport_observed'])
        self.assertFalse(result['instruction_delivery_verified']);self.assertFalse(result['existing_ui_chat_control_verified'])
        inventory=self.fleet.inventory(self.manager,['project']);self.assertFalse(inventory['account_wide_inventory_verified'])
        self.assertEqual(inventory['projects'][0]['total_session_count'],1)

    def test_owner_only_registers_hosts_and_templates(self):
        with self.assertRaises(PermissionDenied):self.fleet.register_host(self.manager,'project','host')
        with self.assertRaises(PermissionDenied):self.fleet.register_template(self.manager,'project','template','host',max_sessions=1,expires_at=1500)
        with self.assertRaises(NotFound):self.fleet.register_host(self.owner,'project','unconfigured')

    def test_explicit_project_scopes_checked_before_read(self):
        self.fleet.discover(self.manager,'project','host')
        with self.assertRaises((PermissionDenied,NotFound)):self.fleet.inventory(self.limited,['project','other'])
        with self.assertRaises(PermissionDenied):self.fleet.inventory(self.worker,['project'])
        self.assertEqual(self.discover_calls,1)

    def test_no_broad_or_duplicate_project_list(self):
        for value in ([],['project','project'],'project'):
            with self.subTest(value=value),self.assertRaises(ValueError):self.fleet.inventory(self.manager,value)

    def test_existing_ui_is_visible_unsupported_not_enrollable(self):
        self.items=(self.observation(kind='existing_ui',can_receive=False),)
        self.fleet.discover(self.manager,'project','host')
        row=self.fleet.inventory(self.manager,['project'])['projects'][0]['sessions'][0]
        self.assertEqual(row['state'],'unsupported')
        with self.assertRaises(Conflict):self.fleet.enroll(self.manager,'project','host','thread-one')
        with self.assertRaises(ValueError):self.observation(kind='existing_ui',can_receive=True)

    def test_enrollment_requires_observed_supported_receiving_session(self):
        with self.assertRaises(NotFound):self.fleet.enroll(self.manager,'project','host','not-present')
        self.items=(self.observation(can_receive=False),)
        with self.assertRaises(Conflict):self.fleet.enroll(self.manager,'project','host','thread-one')

    def test_discovery_is_bounded_typed_fresh_and_exact_scope(self):
        for items in ([self.observation()],(self.observation(),self.observation()),(self.observation(project='other'),),
                      (self.observation(repository='another/source'),),(self.observation(observed_at=1),),
                      tuple(self.observation(external_ref='thread-'+str(i)) for i in range(201))):
            self.items=items
            with self.subTest(kind=type(items)),self.assertRaises(Conflict):self.fleet.discover(self.manager,'project','host')
        self.assertFalse(self.fleet.inventory(self.manager,['project'])['projects'][0]['sessions'])

    def test_discovery_outage_is_not_empty_or_ready(self):
        self.fleet.enroll(self.manager,'project','host','thread-one')
        self.discovery=lambda _:(_ for _ in ()).throw(ConnectionError('private provider details'))
        with self.assertRaises(Conflict) as e:self.fleet.discover(self.manager,'project','host')
        self.assertNotIn('private',str(e.exception))
        result=self.fleet.inventory(self.manager,['project'])['projects'][0]
        self.assertEqual(result['total_session_count'],1);self.assertEqual(result['hosts'][0]['state'],'unavailable')
        self.assertEqual(result['sessions'][0]['state'],'unavailable');self.assertFalse(result['sessions'][0]['host_transport_observed'])

    def test_missing_from_fresh_discovery_is_stale(self):
        self.fleet.enroll(self.manager,'project','host','thread-one');self.items=()
        self.fleet.discover(self.manager,'project','host')
        self.assertEqual(self.fleet.inventory(self.manager,['project'])['projects'][0]['sessions'][0]['state'],'stale')

    def test_changed_session_identity_is_not_silently_rebound(self):
        self.fleet.discover(self.manager,'project','host');self.items=(self.observation(session_id='replacement'),)
        with self.assertRaises(Conflict):self.fleet.discover(self.manager,'project','host')

    def test_revocation_or_configuration_change_during_discovery_records_nothing(self):
        def changed(_):
            self.registry.register(self.owner,'project',{**self.config,'name':'Changed'},expected_revision=1)
            return self.items
        self.discovery=changed
        with self.assertRaises(Conflict):self.fleet.discover(self.manager,'project','host')
        self.assertFalse(self.fleet.inventory(self.manager,['project'])['projects'][0]['sessions'])

    def test_bearers_and_secret_shaped_metadata_are_rejected(self):
        self.items=(self.observation(session_id=self.manager),)
        with self.assertRaises(ValueError):self.fleet.discover(self.manager,'project','host')
        for field,value in [('session_id','sk-proj-secret'),('external_ref','github_pat_secret'),('reference','/private/path')]:
            with self.subTest(field=field),self.assertRaises(ValueError):self.observation(**{field:value})

    def test_future_session_creation_requires_prior_exact_approval_and_policy(self):
        self.template()
        for task in (self.task(approved=False),self.task(policy=False)):
            with self.assertRaises(Conflict):self.create_task(task)
        self.assertEqual(self.create_calls,0)

    def test_actual_create_is_metadata_only_and_idempotent(self):
        self.template();task=self.task();result=self.create_task(task)
        self.assertEqual(result['state'],'created');self.assertFalse(result['execution_started'])
        again=self.create_task(task);self.assertEqual(result,again);self.assertEqual(self.create_calls,1)
        row=self.fleet.inventory(self.manager,['project'])['projects'][0]['sessions'][0]
        self.assertEqual(row['state'],'ready');self.assertFalse(row['instruction_delivery_verified'])
        self.assertEqual(self.fleet.get_creation(self.manager,result['id']),result)

    def test_stable_provisioning_id_is_actor_scoped_not_raw_request(self):
        self.template();seen=[]
        def create(project,task,key):
            seen.append(key);return self.observation(external_ref='new-'+key[:8],session_id='new-'+key[:8])
        self.creator=create;self.create_task(self.task(),key='same')
        other=self.store.add_token('other-manager','manager',['project'])
        self.fleet.create_for_task(other,self.task()['id'],'template','same')
        self.assertEqual(len(set(seen)),2);self.assertTrue(all(len(key)==64 for key in seen));self.assertNotIn('same',seen)

    def test_changed_request_binding_conflicts_instead_of_new_session(self):
        self.template();self.create_task(self.task())
        with self.assertRaises(Conflict):self.create_task(self.task())
        self.assertEqual(self.create_calls,1)

    def test_creation_cap_counts_uncertain_reservations(self):
        self.template(cap=1);self.creator=lambda *args:(_ for _ in ()).throw(TimeoutError('secret raw failure'))
        task=self.task()
        with self.assertRaises(Conflict) as error:self.create_task(task)
        self.assertNotIn('secret',str(error.exception))
        repeated=self.create_task(task);self.assertEqual(repeated['state'],'uncertain');self.assertEqual(self.create_calls,1)
        with self.assertRaises(Conflict):self.create_task(self.task(),key='different')
        self.assertEqual(self.create_calls,1)

    def test_crash_intent_survives_restart_without_replay(self):
        self.template();task=self.task();self.creator=lambda *args:(_ for _ in ()).throw(SystemExit('simulated crash'))
        with self.assertRaises(SystemExit):self.create_task(task)
        reopened=Fleet(self.store,self.registry,self.providers)
        result=reopened.create_for_task(self.manager,task['id'],'template','create')
        self.assertEqual(result['state'],'uncertain');self.assertEqual(self.create_calls,1)

    def test_intent_exists_before_provider_call(self):
        self.template()
        def inspect(project,task,key):
            with self.store._transaction() as c:
                row=c.execute('SELECT state,id FROM fleet_creates').fetchone()
                self.assertEqual(row['state'],'creating');self.assertEqual(row['id'],key)
            return self.observation(external_ref='new-thread',session_id='new-thread')
        self.creator=inspect;self.assertEqual(self.create_task()['state'],'created')

    def test_template_expiry_owner_revocation_and_maintenance_disable_block(self):
        self.template();task=self.task();self.now+=601
        with self.assertRaises(PermissionDenied):self.create_task(task)
        self.assertEqual(self.create_calls,0)

    def test_owner_revocation_blocks_existing_grant(self):
        self.template();task=self.task()
        with self.store._transaction() as c:c.execute('UPDATE bridge_tokens SET revoked=? WHERE token_hash=?',(self.now,token_hash(self.owner)))
        with self.assertRaises(PermissionDenied):self.create_task(task)
        self.assertEqual(self.create_calls,0)

    def test_callback_revocation_marks_creation_uncertain(self):
        self.template();task=self.task()
        def revoke(*args):
            self.store.revoke_token(self.owner,token_hash(self.manager));return self.observation(external_ref='new-thread',session_id='new-thread')
        self.creator=revoke
        with self.assertRaises(PermissionDenied):self.create_task(task)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT state FROM fleet_creates').fetchone()[0],'uncertain')

    def test_wrong_source_or_unreceivable_creation_never_enrolls(self):
        self.template();task=self.task()
        self.creator=lambda *args:self.observation(source_sha='b'*40)
        with self.assertRaises(Conflict):self.create_task(task)
        self.assertFalse(self.fleet.inventory(self.manager,['project'])['projects'][0]['sessions'])

    def test_already_observed_session_is_not_new_creation(self):
        self.template();self.fleet.discover(self.manager,'project','host');self.creator=lambda *args:self.observation()
        with self.assertRaises(Conflict):self.create_task()
        self.assertEqual(self.fleet.inventory(self.manager,['project'])['projects'][0]['total_session_count'],1)

    def test_template_changes_need_new_reference_and_explicit_bounds(self):
        self.template()
        with self.assertRaises(Conflict):self.fleet.register_template(self.owner,'project','template','host',max_sessions=4,expires_at=1600)
        for count in (0,True,101):
            with self.assertRaises(ValueError):self.fleet.register_template(self.owner,'project','new','host',max_sessions=count,expires_at=1600)
        for expires in (self.now,self.now+8*86400,float('inf')):
            with self.assertRaises(ValueError):self.fleet.register_template(self.owner,'project','new','host',max_sessions=1,expires_at=expires)

    def test_aliases_cannot_duplicate_one_session_across_creation_or_discovery(self):
        self.items=(self.observation(),self.observation(external_ref='alias'))
        with self.assertRaises(Conflict):self.fleet.discover(self.manager,'project','host')
        self.items=(self.observation(),);self.fleet.discover(self.manager,'project','host');self.template()
        self.creator=lambda *args:self.observation(external_ref='new-alias')
        with self.assertRaises(Conflict):self.create_task()
        self.assertEqual(self.fleet.inventory(self.manager,['project'])['projects'][0]['total_session_count'],1)

    def test_inventory_is_bounded_without_executing_host_callbacks(self):
        self.items=tuple(self.observation(external_ref='thread-'+str(i),session_id='thread-'+str(i)) for i in range(200))
        self.fleet.discover(self.manager,'project','host')
        self.discovery=lambda _:(_ for _ in ()).throw(AssertionError('inventory must not call host'))
        result=self.fleet.inventory(self.manager,['project'])['projects'][0]
        self.assertEqual(result['displayed_session_count'],200);self.assertEqual(result['total_session_count'],200)
        self.assertFalse(result['sessions_truncated']);self.assertEqual(self.discover_calls,1)

    def test_reapproved_revision_before_callback_never_uses_old_intent(self):
        self.template();task=self.task();original=self.fleet._approved_task;calls=[0]
        def changed(c,token,task_id):
            calls[0]+=1
            if calls[0]==2:
                row=c.execute('SELECT * FROM bridge_tasks WHERE id=?',(task_id,)).fetchone()
                approval=json.loads(row['approval']);approval['version']=row['version']+1
                c.execute('UPDATE bridge_tasks SET version=version+1,approval=? WHERE id=?',(json.dumps(approval),task_id))
            return original(c,token,task_id)
        self.fleet._approved_task=changed
        with self.assertRaises(Conflict):self.create_task(task)
        self.assertEqual(self.create_calls,0)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT state FROM fleet_creates').fetchone()[0],'uncertain')


class DeliveryTests(unittest.TestCase):
    setUp=FleetTests.setUp
    observation=FleetTests.observation
    discover=FleetTests.discover
    create=FleetTests.create
    task=FleetTests.task
    def prepare_delivery(self):
        from manager_bridge.fleet import DeliveryObservation
        self.fleet.enroll(self.manager,'project','host','thread-one')
        task=self.task();self.coord.readiness(self.manager,token_hash(self.worker),ready=True,reason='ready',ttl=60);self.coord.route(self.manager,task['id'],worker_hash=token_hash(self.worker));leased=self.store.claim(self.worker,'project',task_id=task['id'])
        self.evidence=DeliveryObservation('project','thread-one',task['id'],leased['version'],leased['manifest_digest'],
            'codex-execution/'+'a'*64,'commands_observed',1,0,True,'codex-receipt/'+'b'*64,self.now,
            digest(leased['worker']),token_hash(leased['lease_token']))
        self.callback=lambda *args:self.evidence
        self.fleet.providers[('project','host')]=FleetProvider(self.discover,self.create,True,lambda *args:self.callback(*args))
        return task

    def verify(self,task):
        return self.fleet.verify_delivery(self.manager,'project','host','thread-one',task['id'])

    def session(self):
        return self.fleet.inventory(self.manager,['project'])['projects'][0]['sessions'][0]

    def test_actual_commands_verified_without_claiming_tests_or_acceptance(self):
        task=self.prepare_delivery();result=self.verify(task)
        self.assertTrue(result['instruction_delivery_verified']);self.assertFalse(result['tests_verified']);self.assertFalse(result['technical_accepted'])
        self.assertTrue(self.session()['instruction_delivery_verified'])
        self.assertNotIn(self.evidence.lease_digest,json.dumps(self.session()))

    def test_turn_failed_uncertain_never_promoted(self):
        task=self.prepare_delivery()
        for state,commands,failed,turn in [('turn_completed',0,0,True),('execution_failed',0,1,False),('uncertain',0,0,False)]:
            self.evidence=replace(self.evidence,state=state,completed_commands=commands,failed_commands=failed,turn_completed=turn)
            self.assertFalse(self.verify(task)['instruction_delivery_verified']);self.assertFalse(self.session()['instruction_delivery_verified'])

    def test_receipt_exact_bindings_required(self):
        task=self.prepare_delivery();valid=self.evidence
        for field,value in [('project','other'),('session_id','another'),('task_id','f'*32),('task_version',2),('manifest_digest','c'*64),('worker_subject_digest','d'*64),('lease_digest','e'*64),('observed_at',1)]:
            self.evidence=replace(valid,**{field:value})
            with self.subTest(field=field),self.assertRaises(Conflict):self.verify(task)
        self.assertFalse(self.session()['instruction_delivery_verified'])

    def test_revocation_during_observer_prevents_persistence(self):
        task=self.prepare_delivery()
        def revoke(*args):
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return self.evidence
        self.callback=revoke
        with self.assertRaises(PermissionDenied):self.verify(task)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM fleet_deliveries').fetchone()[0],0)

    def test_receipt_does_not_survive_changed_lease_or_worker_revocation(self):
        task=self.prepare_delivery();self.verify(task)
        with self.store._transaction() as c:c.execute('UPDATE bridge_tasks SET lease_hash=? WHERE id=?',('f'*64,task['id']))
        self.assertFalse(self.session()['instruction_delivery_verified'])

    def test_freshness_and_host_outage_invalidate_delivery(self):
        task=self.prepare_delivery();self.verify(task)
        self.now+=301;self.assertFalse(self.session()['instruction_delivery_verified'])
        self.now-=301
        self.discovery=lambda *args:(_ for _ in ()).throw(RuntimeError('secret provider output'))
        with self.assertRaises(Conflict):self.fleet.discover(self.manager,'project','host')
        self.assertFalse(self.session()['instruction_delivery_verified'])

    def test_boolean_or_untyped_success_is_not_receipt(self):
        task=self.prepare_delivery();self.callback=lambda *args:True
        with self.assertRaises(Conflict):self.verify(task)
        self.assertFalse(self.session()['instruction_delivery_verified'])

    def test_invalid_promoted_receipt_rejected(self):
        self.prepare_delivery()
        for change in ({'completed_commands':0},{'failed_commands':1},{'turn_completed':False},{'completed_commands':True}):
            with self.assertRaises(ValueError):replace(self.evidence,**change)

from manager_bridge.tests import test_three_tier as tier_fixtures

class ReworkDeliveryTests(unittest.TestCase):
    setUp=tier_fixtures.ThreeTierTests.setUp
    task=tier_fixtures.ThreeTierTests.task
    start=tier_fixtures.ThreeTierTests.start
    reported=tier_fixtures.ThreeTierTests.reported
    reviewed=tier_fixtures.ThreeTierTests.reviewed
    observe=tier_fixtures.ThreeTierTests.observe
    verify=tier_fixtures.ThreeTierTests.verify

    def prepare(self):
        from manager_bridge.fleet import DeliveryObservation
        task,review=self.reviewed()
        self.core.decide(self.manager,task['id'],review['candidate_digest'],decision='rework')
        assignment=self.core.claim_rework(self.dev,task['id'])
        self.evidence=DeliveryObservation('project','session',task['id'],task['version'],task['manifest_digest'],
            'codex-execution/'+'a'*64,'commands_observed',1,0,True,'fixture/actual',self.now,digest(assignment['worker']),token_hash(assignment['lease_token']))
        self.items=(SessionObservation('project','session','session','codex_cli','fixture/project','b'*40,True,'fixture/session',self.now),)
        self.callback=lambda *_:self.evidence
        self.fleet=Fleet(self.store,self.registry,{('project','host'):FleetProvider(lambda _:self.items,None,True,lambda *args:self.callback(*args))})
        self.fleet.register_host(self.owner,'project','host');self.fleet.enroll(self.manager,'project','host','session')
        return task,assignment

    def check(self,task):return self.fleet.verify_delivery(self.manager,'project','host','session',task['id'])

    def test_exact_core_frozen_rework_checkpoint_is_verified(self):
        task,assignment=self.prepare()
        with self.store._transaction() as c:
            row=c.execute('SELECT * FROM hierarchy_rework_checkpoints WHERE task_id=?',(task['id'],)).fetchone()
            self.assertEqual(row['lease_hash'],token_hash(assignment['lease_token']))
            self.assertEqual(json.loads(row['candidate'])['source']['head_sha'],'b'*40)
            self.assertIsNone(c.execute('SELECT candidate FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone()[0])
        self.assertTrue(self.check(task)['instruction_delivery_verified'])

    def test_ordinary_running_wrong_checkpoint_not_allowed(self):
        task,_=self.prepare()
        with self.store._transaction() as c:c.execute("UPDATE hierarchy_tasks SET state='developing' WHERE task_id=?",(task['id'],))
        with self.assertRaises(Conflict):self.check(task)

    def test_wrong_checkpoint_and_changed_candidate_rejected(self):
        task,_=self.prepare();self.items=(replace(self.items[0],source_sha='c'*40),)
        self.fleet.discover(self.manager,'project','host')
        with self.assertRaises(Conflict):self.check(task)
        self.items=(replace(self.items[0],source_sha='b'*40),);self.fleet.discover(self.manager,'project','host')
        with self.store._transaction() as c:
            row=c.execute('SELECT candidate FROM hierarchy_rework_checkpoints WHERE task_id=?',(task['id'],)).fetchone()
            candidate=json.loads(row[0]);candidate['identity']['head_sha']='c'*40
            c.execute('UPDATE hierarchy_rework_checkpoints SET candidate=? WHERE task_id=?',(json.dumps(candidate),task['id']))
        with self.assertRaises(Conflict):self.check(task)

    def test_stale_lease_and_changed_config_rejected(self):
        task,_=self.prepare();self.now+=301
        with self.assertRaises(Conflict):self.check(task)
        self.now-=301
        self.registry.register(self.owner,'project',{**self.config,'name':'Changed configuration'},expected_revision=1)
        with self.assertRaises(Conflict):self.check(task)

    def test_checkpoint_changed_during_observer_never_persists(self):
        task,_=self.prepare()
        def mutate(*_):
            with self.store._transaction() as c:c.execute('UPDATE hierarchy_rework_checkpoints SET rework_count=2 WHERE task_id=?',(task['id'],))
            return self.evidence
        self.callback=mutate
        with self.assertRaises(Conflict):self.check(task)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM fleet_deliveries').fetchone()[0],0)
