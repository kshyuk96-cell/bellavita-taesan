import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from manager_bridge.control_onboarding import ControlOnboarding
from manager_bridge.project_onboarding import Onboarding
from manager_bridge.connection_readiness import Connections,HostProfile,SourcePin,ConnectionFact,CAPABILITIES
from manager_bridge.maintenance_registry import Registry
from manager_bridge.store import Store,Conflict,NotFound,PermissionDenied,token_hash
from manager_bridge.three_tier import ThreeTier


class ControlOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory());self.now=1000
        self.store=Store(Path(self.temp)/'control.db',clock=lambda:self.now);self.registry=Registry(self.store)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.onboarding=Onboarding(self.store,self.registry)
        self.projects=['bellavita-shop','hope-sunrise']
        self.onboarding.register_catalog(self.owner,self.projects,{p:0 for p in self.projects})
        self.manager=self.store.add_token('manager','manager',self.projects)
        self.limited=self.store.add_token('limited','manager',['bellavita-shop'])
        self.dev=self.store.add_token('developer','worker',self.projects)
        self.reviewer=self.store.add_token('reviewer','worker',self.projects)
        self.connections=Connections(self.store,self.registry)
        self.control=ControlOnboarding(self.store,self.registry,self.connections,self.onboarding)
        self.core=ThreeTier(self.store,self.registry)

    def bindings(self,projects=None):
        return {p:{'revision':v['revision'],'fingerprint':v['fingerprint']} for p in (projects or self.projects) for v in [self.registry.get(self.owner,p)]}

    def plan(self,key='request',projects=None):
        selected=projects or self.projects
        return self.control.plan(self.manager,key,selected,self.bindings(selected))

    def register_roles(self):
        for member,credential,role in [('manager',self.manager,'taesan'),('developer',self.dev,'developer'),('reviewer',self.reviewer,'reviewer')]:
            self.core.register(self.owner,'bellavita-shop',member,token_hash(credential),role)

    def test_inventory_roles_missing_and_permission_classifications_are_not_authority(self):
        self.register_roles();self.store.revoke_token(self.owner,token_hash(self.reviewer))
        result=self.control.inventory(self.manager,self.projects)
        shop=next(p for p in result['projects'] if p['project']=='bellavita-shop')
        roles={r['role']:r for r in shop['roles']}
        self.assertEqual(roles['developer']['classification'],'registered')
        self.assertEqual(roles['reviewer']['classification'],'permission_denied')
        self.assertTrue(all(c['classification']=='missing' for c in shop['connections']))
        self.assertFalse(result['execution_ready']);self.assertFalse(result['approval_granted'])
        self.assertNotIn(token_hash(self.dev),json.dumps(result));self.assertNotIn(self.manager,json.dumps(result))

    def test_cached_reports_never_call_provider_and_distinguish_denied_stale_and_missing(self):
        project='bellavita-shop';cfg=self.registry.get(self.owner,project)['config']
        pin=SourcePin(project,cfg['source_kind'],cfg['source_ref'],cfg['branch'],'a'*40,'v1')
        calls=[]
        def observe(p,cap):
            calls.append(cap)
            if cap=='model':return ConnectionFact(p,cap,'a'*40,'permission_denied','fixture/denied',self.now)
            return ConnectionFact(p,cap,'a'*40,'verified','fixture/observed',self.now,passed=1 if cap=='fixture_tests' else 0)
        self.connections.profiles[project]=HostProfile(pin,observe)
        self.connections.inspect(self.manager,project);calls.clear()
        result=self.control.inventory(self.manager,[project]);self.assertEqual(calls,[])
        facts={f['capability']:f['classification'] for f in result['projects'][0]['connections']}
        self.assertEqual(facts['source_snapshot'],'observed_verified');self.assertEqual(facts['model'],'permission_denied')
        self.now+=301
        stale=self.control.inventory(self.manager,[project])
        self.assertEqual(stale['projects'][0]['connections'][0]['classification'],'unverified')

    def test_plan_exact_snapshot_idempotency_and_configuration_drift(self):
        plan=self.plan();self.assertEqual(plan,self.plan());self.assertEqual(self.control.get_plan(self.manager,plan['id'])['state'],'current')
        cfg=self.registry.get(self.owner,'bellavita-shop')
        self.registry.register(self.owner,'bellavita-shop',cfg['config'],expected_revision=cfg['revision'])
        self.assertEqual(self.control.get_plan(self.manager,plan['id'])['state'],'invalidated')
        with self.assertRaises(Conflict):self.plan()
        self.assertNotEqual(self.plan('new')['id'],plan['id'])

    def test_role_change_invalidates_plan_without_configuration_change(self):
        plan=self.plan();self.register_roles()
        self.assertEqual(self.control.get_plan(self.manager,plan['id'])['state'],'invalidated')
        with self.assertRaises(Conflict):self.plan()

    def test_missing_catalog_project_is_owner_registration_plan_only(self):
        project='bellavita-erp'
        result=self.control.plan(self.owner,'new-catalog',[project],{project:{'revision':0,'fingerprint':None}})
        action=result['plan']['registration_action']
        self.assertEqual(action['projects'],[project]);self.assertTrue(action['owner_required']);self.assertFalse(action['performed'])
        self.assertEqual(self.registry.list(self.owner)[0]['revision'],1)
        with self.store._transaction() as c:self.assertIsNone(c.execute('SELECT * FROM maintenance_projects WHERE project=?',(project,)).fetchone())

    def test_hostile_project_scopes_actors_and_request_keys_fail_closed(self):
        with self.assertRaises(NotFound):self.control.inventory(self.limited,self.projects)
        with self.assertRaises(PermissionDenied):self.control.inventory(self.dev,['bellavita-shop'])
        plan=self.plan()
        with self.assertRaises(NotFound):self.control.get_plan(self.owner,plan['id'])
        self.store.revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(PermissionDenied):self.control.get_plan(self.manager,plan['id'])
        with self.assertRaises(PermissionDenied):self.plan()

    def test_exact_expected_bindings_and_bounded_selection(self):
        bindings=self.bindings();bindings['bellavita-shop']['fingerprint']='b'*64
        with self.assertRaises(Conflict):self.control.plan(self.manager,'wrong',self.projects,bindings)
        for selection in ([],self.projects*2,['x'+str(i) for i in range(21)]):
            with self.assertRaises(ValueError):self.control.inventory(self.owner,selection)
        with self.assertRaises(ValueError):self.control.plan(self.manager,'bad',self.projects,{})

    def test_restart_replays_same_plan_and_known_credential_cannot_be_request_key(self):
        plan=self.plan()
        restarted=Store(self.store.path,clock=lambda:self.now);registry=Registry(restarted)
        service=ControlOnboarding(restarted,registry)
        self.assertEqual(service.get_plan(self.manager,plan['id']),plan)
        with self.assertRaises(ValueError):self.control.plan(self.manager,self.dev,self.projects,self.bindings())

    def test_scope_change_invalidates_access_to_saved_plan(self):
        plan=self.plan()
        with self.store._transaction() as c:
            c.execute('UPDATE bridge_tokens SET projects=? WHERE token_hash=?',(json.dumps(['bellavita-shop']),token_hash(self.manager)))
        with self.assertRaises(NotFound):self.control.get_plan(self.manager,plan['id'])
        with self.assertRaises(NotFound):self.plan()

    def test_connection_change_and_other_known_bearer_never_echoed(self):
        plan=self.plan()
        with self.store._transaction() as c:
            cfg=self.registry._get(c,self.owner,'bellavita-shop')
            report={'project':'bellavita-shop','config_fingerprint':cfg['fingerprint'],'facts':[{'capability':'receiver','state':'permission_denied','reference':'fixture/denied','observed_at':self.now}]}
            c.execute('INSERT INTO manager_connection_reports VALUES (?,?,?)',('bellavita-shop',json.dumps(report),self.now))
        self.assertEqual(self.control.get_plan(self.manager,plan['id'])['state'],'invalidated')
        with self.assertRaises(Conflict):self.plan()
        with self.store._transaction() as c:
            report['facts'][0]['reference']=self.reviewer
            c.execute('UPDATE manager_connection_reports SET report=? WHERE project=?',(json.dumps(report),'bellavita-shop'))
        with self.assertRaises(ValueError):self.control.inventory(self.manager,self.projects)

    def test_role_inventory_is_bounded_and_truncation_never_means_missing(self):
        config=self.registry.get(self.owner,'bellavita-shop')
        with self.store._transaction() as c:
            for i in range(101):
                c.execute('INSERT INTO hierarchy_members VALUES (?,?,?,?,?,?,?)',('bellavita-shop','member-'+str(i).zfill(3),token_hash(self.dev),'developer','developer',None,config['fingerprint']))
        roles=self.control.inventory(self.manager,['bellavita-shop'])['projects'][0]['roles']
        self.assertEqual(sum(len(role['members']) for role in roles),100)
        self.assertTrue(all(role['inventory_truncated'] for role in roles))
        self.assertTrue(all(role['classification']=='unverified' for role in roles))

    def test_revision_change_inside_inventory_prevents_mixed_snapshot(self):
        original=self.control._roles
        def drift(c,project,config):
            result=original(c,project,config)
            c.execute('UPDATE maintenance_projects SET revision=revision+1,fingerprint=? WHERE project=?',('b'*64,project))
            return result
        with patch.object(self.control,'_roles',side_effect=drift):
            with self.assertRaisesRegex(Conflict,'during control inventory'):self.plan()
        self.assertTrue(all(v['revision']==1 for v in self.registry.list(self.owner)))
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM control_onboarding_plans').fetchone()[0],0)
