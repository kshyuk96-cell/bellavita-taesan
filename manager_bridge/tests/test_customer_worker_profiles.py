import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from manager_bridge.customer_worker_profiles import CustomerWorkerProfiles
from manager_bridge.project_onboarding import Onboarding
from manager_bridge.maintenance_registry import Registry
from manager_bridge.store import Store, Conflict, PermissionDenied, NotFound, token_hash
from manager_bridge.three_tier import ThreeTier


class WorkerProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory());self.root=Path(self.temp)
        self.store=Store(self.root/'control'/'store.db',clock=lambda:1000);self.registry=Registry(self.store)
        self.owner=self.store.add_token('owner','owner',['*'])
        onboard=Onboarding(self.store,self.registry);self.projects=list(onboard.catalog())
        onboard.register_catalog(self.owner,self.projects,{p:0 for p in self.projects})
        self.manager=self.store.add_token('manager','manager',self.projects)
        self.dev=self.store.add_token('developer','worker',self.projects)
        self.reviewer=self.store.add_token('reviewer','worker',self.projects)
        self.core=ThreeTier(self.store,self.registry)
        self.profiles=CustomerWorkerProfiles(self.store,self.registry,self.core)
        self.source=self.root/'source';self.source.mkdir();(self.source/'tests').mkdir()
        (self.source/'README.md').write_text('Synthetic requirements: preserve existing state.\n')
        (self.source/'tests'/'test_fixture.py').write_text('def test_fixture(): pass\n')
        def git(*args):return subprocess.check_output(['/usr/bin/git','-C',str(self.source),*args],stderr=subprocess.DEVNULL).decode().strip()
        git('init');git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@invalid','commit','-m','fixture')
        self.sha=git('rev-parse','HEAD')
        self.paths={'candidates':str(self.root/'candidates'),'runtime':str(self.root/'runtime'),
                    'developer_token_file':str(self.root/'control'/'dev.token'),'reviewer_token_file':str(self.root/'control'/'review.token')}
        self.settings={'developer':{'binary':'/usr/bin/codex','codex_home':str(self.root/'dev-profile')},
                       'reviewer':{'binary':'/usr/bin/codex','codex_home':str(self.root/'review-profile')}}

    def prepare(self,project='bellavita-shop',**changes):
        values=dict(base_sha=self.sha,checks={'fixture':['/usr/bin/python3','-m','unittest']},evidence_files=['README.md','tests/test_fixture.py'],paths=self.paths,profiles=self.settings)
        values.update(changes)
        return self.profiles.prepare(self.manager,project,str(self.source),**values)

    def test_four_catalog_profiles_pin_actual_files_without_reading_credentials(self):
        for project in self.projects:
            result=self.prepare(project)
            manifest=result['requirements_manifest'];entry=result['continuous_host_project']
            self.assertEqual(manifest['base_sha'],self.sha);self.assertEqual(len(manifest['inspected_files']),2)
            self.assertFalse(manifest['all_requirements_known']);self.assertEqual(manifest['tests_executed'],0)
            self.assertFalse(result['execution_ready']);self.assertFalse(result['authentication_verified'])
            self.assertIn('registered_session_or_approved_fleet_template',result['pending_prerequisites'])
            self.assertNotIn(self.manager,json.dumps(result))
            self.assertNotEqual(entry['developer']['codex_home'],entry['reviewer']['codex_home'])
            self.assertFalse(self.registry.get(self.owner,project)['config']['maintenance_enabled'])

    def test_source_head_dirty_state_and_unbounded_evidence_fail_closed(self):
        with self.assertRaises(Conflict):self.prepare(base_sha='a'*40)
        with self.assertRaises(ValueError):self.prepare(evidence_files=['.env'])
        with self.assertRaises(ValueError):self.prepare(evidence_files=['tests/../../README.md'])
        (self.source/'README.md').write_text('Changed source\n')
        with self.assertRaises(Conflict):self.prepare()

    def test_isolation_and_profile_distinction_required(self):
        paths={**self.paths,'developer_token_file':str(self.source/'secret')}
        with self.assertRaises(ValueError):self.prepare(paths=paths)
        settings={**self.settings,'reviewer':self.settings['developer']}
        with self.assertRaises(ValueError):self.prepare(profiles=settings)
        with self.assertRaises(ValueError):self.prepare(checks={'fixture':'echo pass'})
        with self.assertRaises(ValueError):self.prepare(paths={**self.paths,'reviewer_token_file':self.paths['developer_token_file']})

    def bind(self):
        return self.profiles.bind_roles(self.owner,'bellavita-shop',token_hash(self.dev),token_hash(self.reviewer),token_hash(self.manager))

    def test_owner_binding_atomic_idempotent_and_persistent(self):
        result=self.bind();self.assertEqual(result,self.bind());self.assertFalse(result['connection_verified'])
        self.assertEqual({x['role'] for x in result['members']},{'developer','reviewer','taesan'})
        restarted=Store(self.store.path)
        with restarted._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM hierarchy_members').fetchone()[0],3)
        with self.assertRaises(PermissionDenied):self.profiles.bind_roles(self.manager,'bellavita-shop',token_hash(self.dev),token_hash(self.reviewer),token_hash(self.manager))

    def test_partial_role_failure_does_not_register_any_member(self):
        self.store.revoke_token(self.owner,token_hash(self.reviewer))
        with self.assertRaises(PermissionDenied):self.bind()
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM hierarchy_members').fetchone()[0],0)

    def test_same_subject_cannot_supply_independent_roles(self):
        duplicate=self.store.add_token('developer','worker',self.projects)
        with self.assertRaises(Conflict):self.profiles.bind_roles(self.owner,'bellavita-shop',token_hash(self.dev),token_hash(duplicate),token_hash(self.manager))
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM hierarchy_members').fetchone()[0],0)

    def test_scope_and_current_token_cannot_enter_packet(self):
        limited=self.store.add_token('limited','manager',['hope-sunrise'])
        with self.assertRaises(NotFound):self.profiles.prepare(limited,'bellavita-shop',str(self.source),base_sha=self.sha,checks={'fixture':['/usr/bin/true']},evidence_files=['README.md'],paths=self.paths,profiles=self.settings)
        with self.assertRaises(ValueError):self.prepare(checks={'fixture':['/usr/bin/echo',self.manager]})

    def test_sql_failure_on_second_role_rolls_back_first_role(self):
        with self.store._transaction() as c:
            c.execute("CREATE TRIGGER reject_reviewer BEFORE INSERT ON hierarchy_members WHEN NEW.role='reviewer' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):self.bind()
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM hierarchy_members').fetchone()[0],0)

    def test_packet_rejects_current_and_other_known_credentials_in_all_fields(self):
        with self.assertRaises(ValueError):self.prepare(session_id=self.manager)
        with self.assertRaises(ValueError):self.prepare(checks={'fixture':['/usr/bin/echo',self.dev]})
        with self.assertRaises(ValueError):self.prepare(paths={**self.paths,'runtime':str(self.root/self.reviewer)})
        with self.assertRaises(ValueError):self.prepare(profiles={**self.settings,'developer':{**self.settings['developer'],'binary':'/usr/bin/'+self.dev}})

    def test_session_reference_uses_existing_canonical_uuid_contract(self):
        session='12345678-1234-1234-1234-123456789abc'
        self.assertEqual(self.prepare(session_id=session)['continuous_host_project']['session_id'],session)
        for invalid in ('unregistered-human-label','12345678-1234-1234-1234-123456789ABC'):
            with self.assertRaises(ValueError):self.prepare(session_id=invalid)
