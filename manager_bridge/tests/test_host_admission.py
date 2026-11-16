"""Actual admission fences with bounded trusted host observations, no models."""
from dataclasses import replace
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.host_admission import HostAdmission,HostObservation,HostRequirement,LocalHostObserver
from manager_bridge.host_preflight import local_host_identity
from manager_bridge.operating_budget import OperatingBudget,BudgetPolicy,BudgetAuthObservation
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.store import Conflict,PermissionDenied


class HostAdmissionTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    task=fixtures.ThreeTierTests.task

    def service(self):
        self.req=HostRequirement(local_host_identity(),'a'*40,30)
        self.obs=HostObservation(self.req.host_identity,'a'*40,True,True,self.now)
        self.gate=HostAdmission(self.core,observer=lambda req:self.obs)
        self.gate.install(self.owner,'project',self.req)
        def auth(p):return BudgetAuthObservation(p,self.registry.get(self.manager,p)['fingerprint'],'ready',self.now,self.now+300,'fixture/auth')
        self.budget=OperatingBudget(self.core,auth_observer=auth,require_policy=True,host_admission=self.gate)
        self.budget.install(self.owner,'project',BudgetPolicy(2,10,60));self.budget.refresh_auth(self.manager,'project')
        return self.gate

    def test_only_owner_installs_and_missing_observation_blocks_actual_dispatch(self):
        gate=self.service()
        with self.assertRaises(PermissionDenied):gate.install(self.manager,'project',self.req,expected_revision=1)
        task=self.task();calls=[]
        controller=ContinuousManager(self.core,handlers={'start':lambda *a:calls.append(a)},admission_budget=self.budget)
        controller.tick(self.manager,['project'],'fixture-host')
        self.assertEqual(calls,[])
        self.assertEqual(self.budget.check(self.manager,'project')['reason'],'host_observation_unavailable')
        gate.refresh(self.manager,'project');self.assertTrue(self.budget.check(self.manager,'project')['allowed'])
        self.now+=30;self.assertFalse(self.budget.check(self.manager,'project')['allowed'])

    def test_negative_measurements_replace_prior_success_and_host_relocation_denies(self):
        gate=self.service();gate.refresh(self.manager,'project')
        cases=[({'namespace_isolation_verified':False},'host_isolation_unavailable'),({'private_storage_verified':False},'host_storage_unavailable'),({'application_commit':'b'*40},'host_application_changed'),({'host_identity':'b'*64},'host_identity_changed'),({'observed_at':self.now+1},'host_observation_expired')]
        original=self.obs
        for changes,reason in cases:
            self.now+=1
            changes=dict(changes)
            if 'observed_at' in changes:changes['observed_at']=self.now+1
            self.obs=replace(original,**({'observed_at':self.now}|changes));gate.refresh(self.manager,'project')
            self.assertEqual(self.budget.check(self.manager,'project')['reason'],reason)
        self.now+=2;self.obs=replace(original,observed_at=self.now);gate.refresh(self.manager,'project')
        with patch('manager_bridge.host_admission.local_host_identity',return_value='c'*64):
            self.assertEqual(self.budget.check(self.manager,'project')['reason'],'host_process_moved')
        gate.observer=lambda req:None;gate.refresh(self.manager,'project')
        self.assertFalse(self.budget.check(self.manager,'project')['allowed'])

    def test_persisted_requirement_cannot_be_bypassed_by_omitting_gate(self):
        gate=self.service();gate.refresh(self.manager,'project')
        bypass=OperatingBudget(self.core)
        self.assertEqual(bypass.check(self.manager,'project')['reason'],'host_gate_unconfigured')
        recreated=HostAdmission(self.core)
        self.assertTrue(recreated.check(self.manager,'project')['allowed'])
        self.now+=31;self.assertFalse(recreated.check(self.manager,'project')['allowed'])

    def test_reentrant_newer_failure_supersedes_older_success(self):
        gate=self.service();old=self.obs
        def read(req):
            gate.observer=lambda req:None
            gate.refresh(self.manager,'project')
            return old
        gate.observer=read
        self.assertEqual(gate.refresh(self.manager,'project')['state'],'superseded')
        self.assertFalse(gate.check(self.manager,'project')['allowed'])

    def test_authority_changes_during_probe_do_not_commit_success(self):
        gate=self.service()
        def read(req):
            gate.install(self.owner,'project',replace(req,application_commit='b'*40),expected_revision=1)
            return self.obs
        gate.observer=read
        with self.assertRaises(Conflict):gate.refresh(self.manager,'project')
        self.assertFalse(gate.check(self.manager,'project')['allowed'])

    def test_host_only_requirement_blocks_core_without_budget_adapter(self):
        gate=HostAdmission(self.core)
        gate.install(self.owner,'project',HostRequirement(local_host_identity(),'a'*40))
        task=self.task()
        with self.assertRaises(PermissionDenied):self.core.start(self.manager,task['id'],'dev','host-only')
        budget=OperatingBudget(self.core)
        self.core.admission_budget=budget
        with self.assertRaises(PermissionDenied):self.core.start(self.manager,task['id'],'dev','host-only-budget')

    def test_local_observer_rejects_application_drift_during_namespace_probe(self):
        self.service()
        observer=LocalHostObserver(application_root=self.root,state_root=self.root)
        facts={'observed_local_host_identity':self.req.host_identity,'namespace_isolation_verified':True,
               'storage':{'private_writable_fsync_verified':True},'observed_at':self.now}
        with patch('manager_bridge.host_admission._git',side_effect=[b'a'*40,b'',b'b'*40]), patch('manager_bridge.host_admission.inspect_preflight',return_value=facts):
            with self.assertRaises(Conflict):observer(self.req)

    def test_poll_reuses_fresh_evidence_and_rechecks_after_expiry(self):
        gate=self.service();calls=[]
        gate.observer=lambda req:(calls.append(req) or self.obs)
        gate.poll(self.manager,['project']);gate.poll(self.manager,['project'])
        self.assertEqual(len(calls),1)
        self.now+=31;self.obs=replace(self.obs,observed_at=self.now)
        gate.poll(self.manager,['project']);self.assertEqual(len(calls),2)

    def test_older_success_cannot_erase_newer_negative_or_transport_failure(self):
        gate=self.service();original=self.obs
        gate.refresh(self.manager,'project')
        self.now+=1;self.obs=replace(original,namespace_isolation_verified=False,observed_at=self.now)
        gate.refresh(self.manager,'project');self.assertFalse(gate.check(self.manager,'project')['allowed'])
        self.now+=1;self.obs=original
        self.assertEqual(gate.refresh(self.manager,'project')['state'],'superseded')
        self.assertFalse(gate.check(self.manager,'project')['allowed'])
        gate.observer=lambda req:None;gate.refresh(self.manager,'project')
        self.obs=replace(original,observed_at=self.now);gate.observer=lambda req:self.obs
        self.assertEqual(gate.refresh(self.manager,'project')['state'],'superseded')
        self.now+=1;self.obs=replace(original,observed_at=self.now)
        self.assertTrue(gate.refresh(self.manager,'project')['allowed'])
