"""Project-scoped maintenance facts; no network or operational credentials."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import unittest

from manager_bridge.maintenance_registry import (CredentialObservation, Observation,
    ReadProbe, Registry, configuration)
from manager_bridge.store import Conflict, NotFound, PermissionDenied, Store, token_hash


class MaintenanceRegistryTests(unittest.TestCase):
    def setUp(self):
        folder=self.enterContext(tempfile.TemporaryDirectory())
        self.now=1000.0
        self.store=Store(Path(folder)/'private.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        self.config={'company':'bellavita','name':'벨라비타 ERP','source_kind':'github',
            'source_ref':'fixture/erp','branch':'main','services':[
                {'kind':'render','id':'srv-erp','url':'https://erp.example.test','purpose':'review'}],
            'maintenance_enabled':False}
        self.registry.register(self.owner,'erp',self.config)
        other=copy.deepcopy(self.config);other.update(company='hope',name='희망일출',source_ref='fixture/hope')
        other['services'][0].update(id='srv-hope',url='https://hope.example.test')
        self.registry.register(self.owner,'hope',other)
        self.manager=self.store.add_token('taesan','manager',['erp'])
        self.worker=self.store.add_token('worker','worker',['erp'])
        self.hope_manager=self.store.add_token('hope-manager','manager',['hope'])

    def observation(self,target='fixture/erp',**changes):
        values=dict(state='ready',target=target,reference='fixture:observation',observed_at=self.now)
        values.update(changes)
        return Observation(**values)

    def probes(self,source=None,service=None):
        return Registry(self.store,probes={
            ('erp','source'):ReadProbe('fixture/erp',source or (lambda *_:self.observation())),
            ('erp','service'):ReadProbe('srv-erp',service or (lambda *_:self.observation('srv-erp')))})

    def credential(self,**changes):
        values=dict(company='bellavita',project='erp',alias='source-read',capabilities=('read','test'),
                    expires_at=self.now+60,reference='fixture:credential')
        values.update(changes)
        return CredentialObservation(**values)

    def credential_registry(self,callback):
        return Registry(self.store,credential_observers={('erp','source-read'):callback})

    def incident(self,**changes):
        values=dict(code='source-unavailable',target='fixture/erp',severity='normal',reference='fixture:incident')
        values.update(changes)
        return self.registry.incident(self.manager,'erp',**values)

    def test_inventory_only_owner_can_register(self):
        for token in (self.manager,self.worker):
            with self.subTest(role=token_hash(token)),self.assertRaises(PermissionDenied):
                self.registry.register(token,'erp',self.config,expected_revision=1)

    def test_inventory_scoped_owner_cannot_register_other_project(self):
        owner=self.store.add_token('erp-owner','owner',['erp'])
        with self.assertRaises(PermissionDenied):self.registry.register(owner,'hope',self.config)

    def test_inventory_list_only_authorized_projects(self):
        self.assertEqual([p['project'] for p in self.registry.list(self.manager)],['erp'])
        self.assertEqual([p['project'] for p in self.registry.list(self.hope_manager)],['hope'])
        self.assertEqual(len(self.registry.list(self.owner)),2)
        with self.assertRaises(NotFound):self.registry.get(self.manager,'hope')
        with self.assertRaises(PermissionDenied):self.registry.list(self.worker)

    def test_inventory_revision_fences_stale_edits(self):
        current=self.registry.get(self.manager,'erp')
        updated=copy.deepcopy(self.config);updated['name']='ERP revised'
        result=self.registry.register(self.owner,'erp',updated,expected_revision=current['revision'])
        self.assertEqual(result['revision'],2)
        self.assertNotEqual(result['fingerprint'],current['fingerprint'])
        with self.assertRaises(Conflict):self.registry.register(self.owner,'erp',updated,expected_revision=1)

    def test_inventory_company_and_repository_cannot_silently_change(self):
        for change in ({'company':'hope'},{'source_ref':'other/repository'}):
            config={**self.config,**change}
            with self.subTest(change=change),self.assertRaises(Conflict):
                self.registry.register(self.owner,'erp',config,expected_revision=1)
        self.assertEqual(self.registry.get(self.manager,'erp')['config']['company'],'bellavita')

    def test_inventory_defensive_copy(self):
        original=self.registry.get(self.manager,'erp')
        original['config']['services'][0]['id']='changed'
        self.assertEqual(self.registry.get(self.manager,'erp')['config']['services'][0]['id'],'srv-erp')

    def test_inventory_sites_source_alias_preserves_exact_id(self):
        config={**self.config,'source_kind':'sites','source_ref':'appgprj_fixture','branch':'main'}
        self.registry.register(self.owner,'sites-fixture',config)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT repository FROM bridge_projects WHERE project=?',('sites-fixture',)).fetchone()[0],'sites/appgprj_fixture')

    def test_four_customer_seeds_disabled_and_review_only(self):
        path=Path(__file__).resolve().parents[1]/'maintenance_projects.json'
        values=json.loads(path.read_text())
        self.assertEqual(len(values),4)
        for config in values.values():
            with self.subTest(project=config['name']):
                self.assertFalse(configuration(config)['maintenance_enabled'])
                self.assertTrue(all(s['purpose']=='review' for s in config['services']))

    def test_inventory_rejects_credentials_in_config(self):
        config={**self.config,'name':self.owner}
        with self.assertRaises(ValueError):self.registry.register(self.owner,'erp',config,expected_revision=1)
        self.assertEqual(self.registry.get(self.manager,'erp')['revision'],1)

    def test_inventory_rejects_unsafe_origins_and_branches(self):
        for url in ('http://erp.test','https://user:password@erp.test','https://erp.test/?key=value','https://erp.test/#credential','https://erp.test/private'):
            config=copy.deepcopy(self.config);config['services'][0]['url']=url
            with self.subTest(url=url),self.assertRaises(ValueError):configuration(config)
        for branch in ('../main','main//feature','/main','main/../feature'):
            with self.subTest(branch=branch),self.assertRaises(ValueError):configuration({**self.config,'branch':branch})
        self.assertEqual(configuration({**self.config,'branch':'codex/shop-backend'})['branch'],'codex/shop-backend')

    def test_unconfigured_probes_are_attention_not_ready(self):
        result=self.registry.inspect(self.manager,'erp')
        self.assertEqual(result['state'],'attention')
        self.assertEqual(len(result['observations']),2)
        self.assertEqual({o['state'] for o in result['observations']},{'not_configured'})
        self.assertFalse(result['operational_change'])

    def test_ready_requires_all_registered_targets(self):
        registry=Registry(self.store,probes={('erp','source'):ReadProbe('fixture/erp',lambda *_:self.observation())})
        self.assertEqual(registry.inspect(self.manager,'erp')['state'],'attention')
        self.assertEqual(self.probes().inspect(self.manager,'erp')['state'],'ready')

    def test_stale_and_future_observations_are_not_ready(self):
        for observed in (self.now-301,self.now+1):
            registry=self.probes(source=lambda *_:self.observation(observed_at=observed))
            with self.subTest(observed=observed):
                result=registry.inspect(self.manager,'erp')
                self.assertEqual(result['state'],'attention')
                self.assertIn('stale',[o['state'] for o in result['observations']])

    def test_probe_state_failures_are_distinct(self):
        for state in ('permission_denied','unavailable','unknown','degraded'):
            with self.subTest(state=state):
                result=self.probes(source=lambda *_:self.observation(state=state)).inspect(self.manager,'erp')
                self.assertEqual(result['state'],'attention')
                self.assertIn(state,[o['state'] for o in result['observations']])

    def test_callback_exception_and_malformed_facts_are_redacted(self):
        def failed(*_):raise RuntimeError('private-token customer content')
        for callback in (failed,lambda *_:{'state':'ready','private':'raw'}):
            result=self.probes(source=callback).inspect(self.manager,'erp')
            self.assertEqual(result['state'],'attention')
            self.assertNotIn('private-token',json.dumps(result))
            self.assertIn('unknown',[o['state'] for o in result['observations']])

    def test_observation_target_mismatch_cannot_establish_health(self):
        result=self.probes(source=lambda *_:self.observation('fixture/hope')).inspect(self.manager,'erp')
        self.assertEqual(result['state'],'attention')
        self.assertIn('unknown',[o['state'] for o in result['observations']])

    def test_unregistered_probe_target_is_rejected_before_callback(self):
        calls=[]
        registry=Registry(self.store,probes={('erp','other'):ReadProbe('srv-other',lambda *_:calls.append(True))})
        with self.assertRaises(Conflict):registry.inspect(self.manager,'erp')
        self.assertEqual(calls,[])

    def test_cross_project_probe_not_invoked(self):
        calls=[]
        registry=Registry(self.store,probes={('hope','source'):ReadProbe('fixture/hope',lambda *_:calls.append(True))})
        result=registry.inspect(self.manager,'erp')
        self.assertEqual(calls,[])
        with self.assertRaises(NotFound):registry.inspect(self.manager,'hope')

    def test_auth_revocation_during_probe_prevents_saved_report(self):
        def revoke(*_):
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return self.observation()
        with self.assertRaises(PermissionDenied):self.probes(source=revoke).inspect(self.manager,'erp')
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM maintenance_health').fetchone()[0],0)

    def test_configuration_change_during_probe_prevents_saved_report(self):
        def change(*_):
            self.registry.register(self.owner,'erp',{**self.config,'name':'changed'},expected_revision=1)
            return self.observation()
        with self.assertRaises(Conflict):self.probes(source=change).inspect(self.manager,'erp')
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM maintenance_health').fetchone()[0],0)

    def test_raw_token_reference_is_not_saved(self):
        result=self.probes(source=lambda *_:self.observation(reference=self.manager)).inspect(self.manager,'erp')
        self.assertNotIn(self.manager,json.dumps(result))
        self.assertIn('unknown',[o['state'] for o in result['observations']])

    def test_read_only_registration_cannot_allow_mutating_probe(self):
        with self.assertRaises(ValueError):ReadProbe('fixture/erp',lambda *_:None,read_only=False)

    def test_credential_metadata_success_never_returns_raw_credential(self):
        result=self.credential_registry(lambda *_:self.credential()).credential(self.manager,'erp','source-read')
        self.assertEqual(result,{'alias':'source-read','state':'ready','reference':'fixture:credential'})
        self.assertNotIn('token',result)

    def test_credential_missing_expired_and_insufficient_distinct(self):
        self.assertEqual(self.registry.credential(self.manager,'erp','source-read')['state'],'not_configured')
        for observation,expected in ((self.credential(expires_at=self.now),'expired'),(self.credential(capabilities=('read',)),'permission_denied')):
            registry=self.credential_registry(lambda *_:observation)
            with self.subTest(expected=expected):
                self.assertEqual(registry.credential(self.manager,'erp','source-read',required=('test',))['state'],expected)

    def test_credential_company_project_alias_must_all_match(self):
        for changes in ({'company':'hope'},{'project':'hope'},{'alias':'other'}):
            observation=self.credential(**changes)
            with self.subTest(changes=changes):
                result=self.credential_registry(lambda *_:observation).credential(self.manager,'erp','source-read')
                self.assertEqual(result['state'],'permission_denied')

    def test_credential_exception_malformed_and_token_reference_fail_closed(self):
        def failed(*_):raise RuntimeError('private secret')
        for callback in (failed,lambda *_:{'token':'private secret'},lambda *_:self.credential(reference=self.manager)):
            result=self.credential_registry(callback).credential(self.manager,'erp','source-read')
            self.assertEqual(result['state'],'unknown')
            self.assertNotIn('private secret',json.dumps(result))
            self.assertNotIn(self.manager,json.dumps(result))

    def test_credential_auth_rechecked_after_provider(self):
        def revoked(*_):
            self.store.revoke_token(self.owner,token_hash(self.manager));return self.credential()
        with self.assertRaises(PermissionDenied):self.credential_registry(revoked).credential(self.manager,'erp','source-read')

    def test_credential_config_change_requires_new_check(self):
        def changed(*_):
            self.registry.register(self.owner,'erp',{**self.config,'name':'changed'},expected_revision=1)
            return self.credential()
        with self.assertRaises(Conflict):self.credential_registry(changed).credential(self.manager,'erp','source-read')

    def test_credential_operational_capabilities_forbidden(self):
        with self.assertRaises(ValueError):self.registry.credential(self.manager,'erp','source-read',required=('deploy',))
        with self.assertRaises(ValueError):self.credential(capabilities=('read','deploy'))

    def test_duplicate_incident_keeps_identity_and_does_not_advance_revision(self):
        first=self.incident();self.now+=10
        second=self.incident(reference='fixture:repeat')
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(second['occurrences'],2)
        self.assertEqual(second['revision'],1)
        self.assertEqual(second['first_seen'],1000)
        self.assertEqual(second['last_seen'],1010)

    def test_incident_severity_escalates_without_downgrade(self):
        first=self.incident()
        high=self.incident(severity='critical')
        low=self.incident(severity='normal')
        self.assertEqual(high['revision'],2)
        self.assertEqual(low['revision'],2)
        self.assertEqual(low['severity'],'critical')

    def test_concurrent_incident_occurrences_are_not_lost(self):
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:self.incident(),range(8)))
        values=self.registry.incidents(self.manager,'erp')
        self.assertEqual(len(values),1)
        self.assertEqual(values[0]['occurrences'],8)

    def test_incident_scope_and_registered_target_required(self):
        with self.assertRaises(Conflict):self.incident(target='srv-hope')
        with self.assertRaises(NotFound):self.registry.incidents(self.hope_manager,'erp')
        self.assertEqual(self.registry.incidents(self.manager,'erp'),[])

    def test_incident_raw_input_token_cannot_be_persisted(self):
        with self.assertRaises(ValueError):self.incident(code=self.manager)
        self.assertEqual(self.registry.incidents(self.manager,'erp'),[])

    def test_credential_alias_cannot_echo_caller_token(self):
        with self.assertRaises(ValueError):self.registry.credential(self.manager,'erp',self.manager)

    def test_reproduction_uses_registered_synthetic_case_and_needs_approval(self):
        incident=self.incident()
        fixture={'case-a':{'project':'erp','check':'authorization_regression','source_sha':'a'*40}}
        result=Registry(self.store,fixtures=fixture).reproduction(self.manager,'erp',incident['id'],fixture_id='case-a')
        self.assertEqual(result['state'],'pending_approval')
        self.assertFalse(result['private_payload_copied'])
        self.assertEqual(result['source_sha'],'a'*40)

    def test_reproduction_other_project_or_missing_fixture_rejected(self):
        incident=self.incident()
        with self.assertRaises(NotFound):self.registry.reproduction(self.manager,'erp',incident['id'],fixture_id='missing')
        fixture={'case-a':{'project':'hope','check':'test','source_sha':'a'*40}}
        with self.assertRaises(ValueError):Registry(self.store,fixtures=fixture).reproduction(self.manager,'erp',incident['id'],fixture_id='case-a')
        with self.assertRaises(NotFound):Registry(self.store,fixtures={'case-a':{**fixture['case-a'],'project':'erp'}}).reproduction(self.manager,'erp','0'*64,fixture_id='case-a')

    def test_reproduction_cannot_return_token_as_fixture_name(self):
        incident=self.incident()
        fixtures={self.manager:{'project':'erp','check':'test','source_sha':'a'*40}}
        with self.assertRaises(ValueError):Registry(self.store,fixtures=fixtures).reproduction(self.manager,'erp',incident['id'],fixture_id=self.manager)

    def test_reproduction_command_text_not_accepted(self):
        incident=self.incident()
        fixtures={'case-a':{'project':'erp','check':'test','source_sha':'a'*40,'argv':['arbitrary']}}
        with self.assertRaises(ValueError):Registry(self.store,fixtures=fixtures).reproduction(self.manager,'erp',incident['id'],fixture_id='case-a')

    def test_invalid_or_worker_token_cannot_invoke_read_provider(self):
        calls=[]
        registry=self.probes(source=lambda *_:calls.append(True))
        for token in (self.worker,'not-an-authenticated-token'):
            with self.subTest(token_hash=token_hash(token) if len(token)>=32 else 'invalid'),self.assertRaises(PermissionDenied):
                registry.inspect(token,'erp')
        self.assertEqual(calls,[])

    def test_expired_manager_cannot_invoke_credential_provider(self):
        token=self.store.add_token('expiring','manager',['erp'],expires_at=self.now+1)
        calls=[]
        registry=self.credential_registry(lambda *_:calls.append(True))
        self.now+=2
        with self.assertRaises(PermissionDenied):registry.credential(token,'erp','source-read')
        self.assertEqual(calls,[])

    def test_reproduction_registry_defensive_copy(self):
        incident=self.incident()
        fixtures={'case-a':{'project':'erp','check':'authorization_regression','source_sha':'a'*40}}
        registry=Registry(self.store,fixtures=fixtures)
        fixtures['case-a']['project']='hope'
        result=registry.reproduction(self.manager,'erp',incident['id'],fixture_id='case-a')
        self.assertEqual(result['project'],'erp')
        self.assertEqual(result['check'],'authorization_regression')

    def test_nonfinite_observation_times_are_rejected(self):
        for value in (True,float('nan'),float('inf'),0,-1):
            with self.subTest(value=value),self.assertRaises(ValueError):self.observation(observed_at=value)

    def test_revocation_between_probes_prevents_later_callbacks(self):
        calls=[]
        def first(*_):
            calls.append('first')
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return self.observation('srv-erp')
        def later(*_):
            calls.append('later')
            return self.observation()
        registry=Registry(self.store,probes={
            ('erp','a-first'):ReadProbe('srv-erp',first),
            ('erp','z-later'):ReadProbe('fixture/erp',later)})
        with self.assertRaises(PermissionDenied):registry.inspect(self.manager,'erp')
        self.assertEqual(calls,['first'])
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM maintenance_health').fetchone()[0],0)

    def test_failed_new_registration_does_not_leave_bridge_orphan(self):
        config={**self.config,'source_ref':'fixture/new'}
        with self.assertRaises(Conflict):self.registry.register(self.owner,'new-project',config,expected_revision=1)
        with self.store._transaction() as c:
            self.assertIsNone(c.execute('SELECT 1 FROM bridge_projects WHERE project=?',('new-project',)).fetchone())
            self.assertIsNone(c.execute('SELECT 1 FROM maintenance_projects WHERE project=?',('new-project',)).fetchone())

    def test_one_sites_probe_covers_shared_source_and_service_id(self):
        config={**self.config,'source_kind':'sites','source_ref':'appgprj_fixture','services':[
            {'kind':'sites','id':'appgprj_fixture','url':'https://site.example.test','purpose':'review'}]}
        self.registry.register(self.owner,'sites-single',config)
        registry=Registry(self.store,probes={
            ('sites-single','site'):ReadProbe('appgprj_fixture',lambda *_:self.observation('appgprj_fixture'))})
        result=registry.inspect(self.owner,'sites-single')
        self.assertEqual(result['state'],'ready')
        self.assertEqual(len(result['observations']),1)

    def test_registry_restart_preserves_inventory_and_incident(self):
        first=self.incident()
        reopened=Registry(Store(self.store.path,clock=lambda:self.now))
        self.assertEqual(reopened.get(self.manager,'erp')['config'],self.config)
        self.assertEqual(reopened.incidents(self.manager,'erp')[0]['id'],first['id'])
