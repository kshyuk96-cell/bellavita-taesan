import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_subscription_broker as fixtures
from manager_bridge.managed_subscription_runtime import ManagedSubscriptionRuntime
from manager_bridge.governance_supervision import SupervisionSettings
from manager_bridge.governance_service import load_service
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.store import Conflict


class ManagedSubscriptionRuntimeTests(unittest.TestCase):
    setUp=fixtures.SubscriptionBrokerTests.setUp
    observe=fixtures.SubscriptionBrokerTests.observe
    verify=fixtures.SubscriptionBrokerTests.verify
    auth=fixtures.SubscriptionBrokerTests.auth

    def setup_runtime(self):
        old=fixtures.SubscriptionBrokerTests.broker(self)
        self.ownerpath=self.root/'owner.token';self.ownerpath.write_text(self.owner);self.ownerpath.chmod(0o600)
        self.paths={}
        for member,token in (('dev',self.dev),('reviewer',self.reviewer)):
            p=self.root/(member+'.token');p.write_text(token);p.chmod(0o600);self.paths[member]=p
        self.runtime=ManagedSubscriptionRuntime(broker_ref=old.ref,canonical_home=self.canonical,
            targets=self.targets,owner_token_file=self.ownerpath,refresh_callback=self.refresh)
        self.host=SimpleNamespace(core=self.core,store=self.store,registry=self.registry,fleet=None,manager_token=lambda:self.manager)
        self.settings={'projects':('project',),'instance_id':'managed-service','poll_seconds':1}
        self.schedule=SupervisionSettings(subscription_interval=300)

    def guards(self):
        return [self.runtime.guard_factory(t.role,t.project,t.member_id,t.home,self.paths[t.member_id]) for t in self.targets]

    def test_unbound_then_ready_after_explicit_refresh(self):
        self.setup_runtime();guards=self.guards()
        with self.assertRaises(Conflict):
            with guards[0]('project'):pass
        broker=self.runtime.bind(self.host,self.settings,self.schedule)
        with self.assertRaises(Conflict):
            with guards[0]('project'):pass
        broker.refresh_and_distribute(self.owner,0,'runtime-first')
        with guards[0]('project') as view:self.assertEqual(1,view['generation'])
        with guards[1]('project') as view:self.assertEqual('reviewer',view['role'])
        self.assertEqual(1,len(self.calls))

    def test_scope_and_schedule_missing_fail(self):
        self.setup_runtime();self.guards()
        for settings,schedule in ((self.settings,None),({**self.settings,'projects':('other',)},self.schedule)):
            with self.assertRaises(ValueError):self.runtime.bind(self.host,settings,schedule)

    def test_wrong_home_and_duplicate_guard(self):
        self.setup_runtime()
        with self.assertRaises(ValueError):self.runtime.guard_factory('developer','project','dev',self.reviewhome,self.paths['dev'])
        self.guards()
        with self.assertRaises(ValueError):self.runtime.guard_factory('developer','project','dev',self.devhome,self.paths['dev'])

    def test_wrong_worker_token_rejected_before_binding(self):
        self.setup_runtime();self.paths['dev'].write_text(self.manager);guards=self.guards()
        with self.assertRaises(ValueError):self.runtime.bind(self.host,self.settings,self.schedule)
        with self.assertRaises(Conflict):
            with guards[0]('project'):pass

    def test_rebind_and_changed_auth_denied(self):
        self.setup_runtime();guards=self.guards();broker=self.runtime.bind(self.host,self.settings,self.schedule)
        broker.refresh_and_distribute(self.owner,0,'runtime-ready')
        with self.assertRaises(Conflict):self.runtime.bind(self.host,self.settings,self.schedule)
        self.auth(self.devhome,'external-change')
        with self.assertRaises(Conflict):
            with guards[0]('project'):pass

    def test_native_loader_installs_same_broker_in_supervision(self):
        self.setup_runtime()
        hc=self.root/'host.json';hc.write_text('{}');hc.chmod(0o600)
        config=self.root/'service.json';config.write_text(json.dumps({'version':1,'host_config_file':str(hc),'listen':{'port':0},'supervision':{'subscription_interval':300}}));config.chmod(0o600)
        controller=ContinuousManager(self.core);guards=[]
        def loader(path,*,profile_guard_factory):
            for t in self.targets:guards.append(profile_guard_factory(t.role,t.project,t.member_id,t.home,self.paths[t.member_id]))
            return self.host,controller,self.settings
        with patch('manager_bridge.governance_service.load_host',side_effect=loader):
            service=load_service(config,subscription_runtime=self.runtime)
        self.addCleanup(service.close)
        self.assertIs(service.supervision.subscription_broker,self.runtime._broker)
        with patch.object(controller,'tick',return_value={}):service.run(max_ticks=1)
        self.assertEqual(1,len(self.calls))
        with guards[0]('project') as value:self.assertEqual(1,value['generation'])
        # The same retained due time prevents refresh storms after a loop poll.
        service.supervision.poll(self.manager)
        self.assertEqual(1,len(self.calls))

    def test_revoked_owner_never_calls_refresh(self):
        self.setup_runtime();self.guards()
        self.ownerpath.write_text(self.manager)
        from manager_bridge.store import PermissionDenied
        with self.assertRaises(PermissionDenied):self.runtime.bind(self.host,self.settings,self.schedule)
        self.assertEqual([],self.calls)

    def test_worker_file_changed_to_manager_after_binding_denied(self):
        self.setup_runtime();guards=self.guards();broker=self.runtime.bind(self.host,self.settings,self.schedule)
        broker.refresh_and_distribute(self.owner,0,'ready-then-change')
        self.paths['dev'].write_text(self.manager)
        with self.assertRaises(ValueError):
            with guards[0]('project'):pass

    def test_native_guard_callback_contract_and_cross_project_denial(self):
        self.setup_runtime();guards=self.guards();broker=self.runtime.bind(self.host,self.settings,self.schedule)
        broker.refresh_and_distribute(self.owner,0,'callback-contract')
        with self.assertRaises(ValueError):
            with guards[0]('other'):pass
        from manager_bridge.fleet_codex import CodexHost
        host=object.__new__(CodexHost);host.profile_guard=guards[0];host._binding=lambda project:None
        with host._profile_scope('project') as value:self.assertEqual('developer',value['role'])
