import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from manager_bridge.tests import test_subscription_broker as fixtures
from manager_bridge.subscription_host import profile_guard_factory,attach_profile_guards
from manager_bridge.fleet_codex import CodexHost,ProjectBinding
from manager_bridge.control import write_secret
from manager_bridge.store import PermissionDenied


class SubscriptionHostTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.SubscriptionBrokerTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.broker=self.fx.broker();self.broker.refresh_and_distribute(self.fx.owner,0,'initial')
        self.path=self.fx.root/'developer.token';write_secret(self.path,self.fx.dev)
        self.bind=profile_guard_factory(self.broker)

    def test_exact_host_role_file_guards_and_rereads_revocation(self):
        guard=self.bind('developer','project','dev',self.fx.devhome,self.path)
        with guard('project') as value:self.assertEqual(value['generation'],1)
        self.path.write_text(self.fx.reviewer)
        with self.assertRaises(PermissionDenied):
            with guard('project'):pass

    def test_other_profile_role_project_or_owner_file_cannot_be_bound(self):
        for role,project,member,home in [('reviewer','project','dev',self.fx.devhome),
                    ('developer','other','dev',self.fx.devhome),('developer','project','dev',self.fx.reviewhome)]:
            with self.assertRaises(PermissionDenied):self.bind(role,project,member,home,self.path)
        self.path.write_text(self.fx.owner)
        with self.assertRaises(PermissionDenied):self.bind('developer','project','dev',self.fx.devhome,self.path)

    def test_guard_refuses_project_change_without_touching_other_profile(self):
        guard=self.bind('developer','project','dev',self.fx.devhome,self.path)
        with self.assertRaises(PermissionDenied):guard('other')

    def test_host_attachment_guards_both_native_roles_before_use(self):
        binary=self.fx.root/'native-stub';binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o700)
        source=self.fx.root/'source';source.mkdir()
        bindings={'project':ProjectBinding('project','fixture/project',source,require_origin=False)}
        developer=CodexHost(binary,self.fx.devhome,bindings)
        reviewer=CodexHost(binary,self.fx.reviewhome,bindings)
        reviewfile=self.fx.root/'reviewer.token';write_secret(reviewfile,self.fx.reviewer)
        @dataclass(frozen=True)
        class Entry:
            developer_transport:object=developer
            developer_id:str='dev'
            reviewer_id:str='reviewer'
            developer_token_file:object=self.path
            reviewer_token_file:object=reviewfile
            reviewer_factory:object=lambda frozen,heartbeat:reviewer
        entry=Entry();host=SimpleNamespace(core=self.fx.core,projects={'project':entry})
        result=attach_profile_guards(host,self.broker)
        self.assertEqual(result['target_profiles'],2);self.assertFalse(result['refresh_performed'])
        with developer._profile_scope('project'):pass
        guarded=host.projects['project'].reviewer_factory(source,heartbeat=lambda:True)
        with guarded._profile_scope('project'):pass
        with self.assertRaises(ValueError):attach_profile_guards(host,self.broker)

    def test_host_attachment_rejects_another_authority_before_mutation(self):
        host=SimpleNamespace(core=object(),projects={})
        with self.assertRaises(ValueError):attach_profile_guards(host,self.broker)
