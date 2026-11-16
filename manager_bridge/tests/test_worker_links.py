"""Source and role metadata must never imply actual worker execution."""
from contextlib import nullcontext
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.worker_links import WorkerLink,NativeWorkerLinks
from manager_bridge.connection_readiness import SourcePin
from manager_bridge.fleet_codex import CodexHost,ProjectBinding
from manager_bridge.subscription_observations import SubscriptionObservation,RateWindow
from manager_bridge.store import token_hash,PermissionDenied,Conflict


class WorkerLinkTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify

    def links(self):
        source=self.root/'source';source.mkdir()
        def git(*a):return subprocess.check_output(['/usr/bin/git','-c','core.hooksPath=/dev/null','-C',str(source),*a],stderr=subprocess.DEVNULL,text=True).strip()
        git('init');git('remote','add','origin','https://github.com/fixture/project.git')
        (source/'file.txt').write_text('source')
        git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@invalid','commit','-m','fixture')
        sha=git('rev-parse','HEAD');pin=SourcePin('project','github','fixture/project','main',sha,'fixture-head')
        output=[]
        for role,member in (('developer','dev'),('reviewer','reviewer')):
            home=self.root/role;home.mkdir(mode=0o700)
            host=CodexHost('/bin/true',home,{'project':ProjectBinding('project','fixture/project',source)},profile_guard=lambda p:nullcontext())
            output.append(WorkerLink(pin,role,member,host))
        return NativeWorkerLinks(self.core,output),source

    def observation(self,allowed=True):
        return SubscriptionObservation('project',self.registry.get(self.owner,'project')['fingerprint'],'observed',self.now,self.now+30,allowed,(RateWindow('primary',14,10080,self.now+1000),))

    def test_native_metadata_separate_from_execution_missing_roles_and_usage_permission(self):
        service,_=self.links()
        with patch('manager_bridge.worker_links.NativeSubscriptionObserver') as factory:
            factory.return_value.read.return_value=self.observation()
            result=service.inspect(self.manager,['project','other'])
            self.assertEqual(factory.return_value.read.call_count,2)
        items=result['items']
        self.assertEqual([r['state'] for r in items],['metadata_connected','metadata_connected','not_configured','not_configured'])
        self.assertTrue(all(not r['worker_execution_verified'] for r in items));self.assertEqual(result['models_started'],0)
        with patch('manager_bridge.worker_links.NativeSubscriptionObserver') as factory:
            factory.return_value.read.return_value=self.observation(False)
            denied=service.inspect(self.manager,['project'])
            self.assertTrue(all(r['state']=='usage_denied' and not r['metadata_connection_verified'] for r in denied['items']))

    def test_dirty_or_changed_source_blocks_before_native_read(self):
        service,source=self.links();(source/'file.txt').write_text('unreviewed')
        with patch('manager_bridge.worker_links.NativeSubscriptionObserver') as factory:
            result=service.inspect(self.manager,['project'])
            factory.assert_not_called()
        self.assertTrue(all(not r['metadata_connection_verified'] and not r['source_verified'] for r in result['items']))

    def test_later_native_callback_invalidates_earlier_source_observation(self):
        service,source=self.links();calls=[]
        def later(*_):
            calls.append(1)
            if len(calls)==2:(source/'file.txt').write_text('changed during second role read')
            return self.observation()
        with patch('manager_bridge.worker_links.NativeSubscriptionObserver') as factory:
            factory.return_value.read.side_effect=later
            result=service.inspect(self.manager,['project'])
        self.assertTrue(all(not r['metadata_connection_verified'] and not r['source_verified'] for r in result['items']))

    def test_worker_cannot_request_aggregate_and_mid_read_revoke_returns_no_result(self):
        service,_=self.links()
        with self.assertRaises(PermissionDenied):service.inspect(self.dev,['project'])
        def revoke(*_):
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return self.observation()
        with patch('manager_bridge.worker_links.NativeSubscriptionObserver') as factory:
            factory.return_value.read.side_effect=revoke
            with self.assertRaises(PermissionDenied):service.inspect(self.manager,['project'])

    def test_wrong_repository_origin_policy_duplicate_and_stale_observation(self):
        service,_=self.links();link=service.links[0]
        with self.assertRaises(ValueError):NativeWorkerLinks(self.core,[link,link])
        link.host.bindings['project']=ProjectBinding('project','fixture/project',link.host.bindings['project'].cwd,require_origin=False)
        with self.assertRaises(ValueError):WorkerLink(link.pin,link.role,link.member_id,link.host)
