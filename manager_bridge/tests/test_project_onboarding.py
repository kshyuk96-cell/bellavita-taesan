from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from manager_bridge.project_onboarding import Onboarding
from manager_bridge.maintenance_registry import Registry
from manager_bridge.connection_readiness import SourcePin
from manager_bridge.source_watch import SourceHead
from manager_bridge.store import Store, Conflict, PermissionDenied


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory())
        self.store=Store(Path(self.temp)/'onboard.sqlite3',clock=lambda:1000)
        self.registry=Registry(self.store)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.board=Onboarding(self.store,self.registry)
        self.projects=list(self.board.catalog())

    def register(self,projects=None,**changes):
        projects=projects or self.projects
        return self.board.register_catalog(changes.get('owner',self.owner),projects,changes.get('expected',{p:0 for p in projects}))

    def test_four_customer_configs_atomic_enrollment_and_restart(self):
        result=self.register()
        self.assertEqual(set(self.projects),{'bellavita-shop','bellavita-erp','hope-sunrise','funeral-association'})
        self.assertEqual(len(result['registrations']),4)
        self.assertFalse(result['execution_ready']);self.assertFalse(result['deployment_authorized'])
        restarted=Store(self.store.path,clock=lambda:1000)
        registry=Registry(restarted);board=Onboarding(restarted,registry)
        self.assertEqual(board.register_catalog(self.owner,self.projects,{p:0 for p in self.projects}),result)
        with restarted._transaction() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM governance_enrolled_projects').fetchone()[0],4)
        for project in self.projects:
            cfg=registry.get(self.owner,project)
            self.assertEqual(cfg['revision'],1);self.assertFalse(cfg['config']['maintenance_enabled'])
            self.assertTrue(all(s['purpose']=='review' for s in cfg['config']['services']))

    def test_owner_role_and_all_project_scopes_required_before_write(self):
        limited=self.store.add_token('limited','owner',[self.projects[0]])
        with self.assertRaises(PermissionDenied):self.register(owner=limited)
        self.assertEqual(self.registry.list(self.owner),[])
        self.register([self.projects[0]])
        manager=self.store.add_token('manager','manager',[self.projects[0]])
        with self.assertRaises(PermissionDenied):self.register([self.projects[0]],owner=manager)

    def test_existing_different_config_never_overwritten_or_partial_registered(self):
        project=self.projects[-1]
        cfg=self.board.catalog()[project];cfg['branch']='preserved-branch'
        self.registry.register(self.owner,project,cfg)
        with self.assertRaises(Conflict):self.register()
        self.assertEqual([r['project'] for r in self.registry.list(self.owner)],[project])
        self.assertEqual(self.registry.get(self.owner,project)['config']['branch'],'preserved-branch')
        with self.store._transaction() as c:
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name='governance_enrolled_projects'").fetchone())

    def test_revision_mismatch_rolls_back_every_selected_project(self):
        expected={p:0 for p in self.projects};expected[self.projects[-1]]=1
        with self.assertRaises(Conflict):self.register(expected=expected)
        self.assertEqual(self.registry.list(self.owner),[])
        self.register()
        with self.assertRaises(Conflict):self.register(expected={p:2 for p in self.projects})
        self.assertTrue(all(x['revision']==1 for x in self.registry.list(self.owner)))

    def test_sql_failure_rolls_back_config_and_enrollment(self):
        project=self.projects[-1]
        with self.store._transaction() as c:
            c.execute("CREATE TRIGGER fail_catalog BEFORE INSERT ON maintenance_projects WHEN NEW.project='"+project+"' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):self.register()
        self.assertEqual(self.registry.list(self.owner),[])
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM bridge_projects').fetchone()[0],0)

    def test_selection_and_revision_contract_is_exact(self):
        for projects,expected in (([],{}),([self.projects[0]]*2,{self.projects[0]:0}),(['unknown'],{'unknown':0}),([self.projects[0]],{}),([self.projects[0]],{self.projects[0]:True})):
            with self.assertRaises(ValueError):self.board.register_catalog(self.owner,projects,expected)

    def test_handoff_planning_reuses_typed_read_only_inventory_and_preserves_source(self):
        project='bellavita-shop';self.register([project]);cfg=self.board.catalog()[project]
        pin=SourcePin(project,cfg['source_kind'],cfg['source_ref'],cfg['branch'],'a'*40,'v1')
        upstream=SourceHead(project,cfg['source_kind'],cfg['source_ref'],cfg['branch'],'b'*40,'v2','fixture/remote',1000)
        with patch('manager_bridge.handoff_inventory._guard_git_filters'),patch('manager_bridge.handoff_inventory.observe_checkout',return_value={'sha':'a'*40,'tracked_and_visible_untracked_clean':True}):
            result=self.board.plan_source_handoff(self.owner,project,self.temp,pin,upstream)
        self.assertEqual(result['state'],'upstream_changed')
        self.assertEqual(result['migration_plan']['strategy'],'separate_worktree')
        self.assertTrue(result['migration_plan']['preserve_original'])
        self.assertFalse(result['source_modified']);self.assertFalse(result['migration_plan']['fetch_performed'])
        with self.assertRaises(ValueError):self.board.plan_source_handoff(self.owner,project,self.temp,{'sha':'a'*40})

    def test_handoff_configuration_drift_during_inspection_never_returns_current_plan(self):
        project='bellavita-shop';self.register([project]);cfg=self.board.catalog()[project]
        pin=SourcePin(project,cfg['source_kind'],cfg['source_ref'],cfg['branch'],'a'*40,'v1')
        def drift(*args,**kwargs):
            self.registry.register(self.owner,project,{**cfg,'branch':'separate-review'},expected_revision=1)
            return {'state':'source_aligned'}
        with patch('manager_bridge.project_onboarding.inspect_handoff',side_effect=drift):
            with self.assertRaises(Conflict):self.board.plan_source_handoff(self.owner,project,self.temp,pin)

    def test_catalog_copy_cannot_enable_maintenance_or_change_future_registration(self):
        catalog=self.board.catalog();catalog['hope-sunrise']['maintenance_enabled']=True
        self.register(['hope-sunrise'])
        self.assertFalse(self.registry.get(self.owner,'hope-sunrise')['config']['maintenance_enabled'])
