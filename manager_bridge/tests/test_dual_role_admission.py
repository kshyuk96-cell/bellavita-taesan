from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.dual_role_admission import DualRoleSubscriptionAdmission
from manager_bridge.subscription_observations import NativeSubscriptionObserver,SubscriptionObservation
from manager_bridge.fleet_codex import CodexHost,ProjectBinding
from manager_bridge.store import Conflict,PermissionDenied,token_hash


class DualRoleAdmissionTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify

    def observer(self):
        roles={}
        for role,member in (('developer','dev'),('reviewer','reviewer')):
            home=self.root/role;home.mkdir(mode=0o700)
            native=CodexHost('/bin/true',home,{'project':ProjectBinding('project','fixture/project',self.root)},profile_guard=lambda project:nullcontext())
            roles[role]=NativeSubscriptionObserver(self.core,native,member)
        self.roles=roles
        self.fp=self.registry.get(self.manager,'project')['fingerprint']
        return DualRoleSubscriptionAdmission(self.core,{'project':roles},lambda:self.manager)

    def value(self,**changes):
        return replace(SubscriptionObservation('project',self.fp,'observed',self.now,self.now+30,True,()),**changes)

    def test_both_roles_required_and_earliest_expiry_used(self):
        observer=self.observer()
        with patch.object(self.roles['developer'],'read',return_value=self.value(valid_until=self.now+20)),patch.object(self.roles['reviewer'],'read',return_value=self.value()):
            value=observer('project')
        self.assertEqual('ready',value.state);self.assertEqual(self.now+20,value.valid_until)

    def test_developer_usage_denied(self):
        observer=self.observer()
        with patch.object(NativeSubscriptionObserver,'read',side_effect=[self.value(ordinary_usage_allowed=False),self.value()]):
            self.assertEqual('unknown',observer('project').state)

    def test_reviewer_usage_denied(self):
        observer=self.observer()
        with patch.object(NativeSubscriptionObserver,'read',side_effect=[self.value(),self.value(ordinary_usage_allowed=False)]):
            self.assertEqual('unknown',observer('project').state)

    def test_unavailable_or_untyped_metadata_denied(self):
        observer=self.observer()
        for bad in ({'state':'observed'},self.value(state='unavailable',ordinary_usage_allowed=None)):
            with patch.object(NativeSubscriptionObserver,'read',side_effect=[self.value(),bad]):
                self.assertEqual('unknown',observer('project').state)

    def test_expired_or_future_metadata_denied(self):
        observer=self.observer()
        for bad in (self.value(observed_at=self.now-30,valid_until=self.now),self.value(observed_at=self.now+1,valid_until=self.now+31)):
            with patch.object(NativeSubscriptionObserver,'read',side_effect=[self.value(),bad]):
                self.assertEqual('unknown',observer('project').state)

    def test_wrong_project_or_configuration_denied(self):
        observer=self.observer()
        for bad in (self.value(project='other'),self.value(config_fingerprint='0'*64)):
            with patch.object(NativeSubscriptionObserver,'read',side_effect=[self.value(),bad]):
                self.assertEqual('unknown',observer('project').state)

    def test_revocation_during_second_role_invalidates_first(self):
        observer=self.observer();values=[self.value(),self.value()]
        def read(*args):
            if len(values)==1:self.store.revoke_token(self.owner,token_hash(self.dev))
            return values.pop(0)
        with patch.object(NativeSubscriptionObserver,'read',side_effect=read):
            with self.assertRaises((PermissionDenied,Conflict)):observer('project')

    def test_registered_role_mismatch_denied_before_rpc(self):
        observer=self.observer();observer.observers['project']['reviewer']=self.roles['developer']
        with patch.object(NativeSubscriptionObserver,'read',side_effect=AssertionError('RPC called')):
            with self.assertRaises((Conflict,PermissionDenied)):observer('project')

    def test_manager_rotation_during_observation_denied(self):
        observer=self.observer();calls=[self.manager,self.owner];observer.token_loader=lambda:calls.pop(0)
        with patch.object(NativeSubscriptionObserver,'read',return_value=self.value()):
            with self.assertRaises(Conflict):observer('project')

    def test_missing_role_or_outside_scope_denied(self):
        observer=self.observer()
        with self.assertRaises(ValueError):DualRoleSubscriptionAdmission(self.core,{'project':{'developer':self.roles['developer']}},lambda:self.manager)
        with self.assertRaises(Conflict):observer('other')
