"""Authenticated transition operations on actual loopback, no model work."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.tests import test_governance_service as service_fixtures
from manager_bridge.control import request
from manager_bridge.operations_control_api import PREFIX
from manager_bridge.encrypted_backup import EncryptedBackup,PrivateLocalBackupBackend
from manager_bridge.governance_durability import GovernanceDurability
from manager_bridge.backup_health import BackupHealth


class TransitionControlTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    compose=service_fixtures.GovernanceServiceTests.compose
    start_http=service_fixtures.GovernanceServiceTests.start_http

    def call(self,base,token,route,body):return request(base,token,'POST',PREFIX+route,body)

    def test_drain_resume_owner_cas_and_real_budget_boundary(self):
        service=self.compose();base=self.start_http(service)
        body={'projects':['project'],'expected_revisions':{'project':0},'request_key':'drain'}
        self.assertEqual(self.call(base,self.manager,'transition/drain',body)['status'],403)
        result=self.call(base,self.owner,'transition/drain',body);self.assertTrue(result['ok'],result)
        self.assertTrue(result['result']['new_admission_blocked'])
        check=self.call(base,self.manager,'budget/report',{'projects':['project']})
        self.assertEqual(check['result']['projects'][0]['reason'],'runtime_transition_blocked')
        status=self.call(base,self.manager,'transition/status',{'projects':['project']})
        self.assertTrue(status['result']['projects'][0]['new_admission_blocked'])
        bad={'projects':['project'],'expected_revisions':{'project':0},'request_key':'resume'}
        self.assertEqual(self.call(base,self.owner,'transition/resume',bad)['status'],409)
        bad['expected_revisions']['project']=1
        self.assertTrue(self.call(base,self.owner,'transition/resume',bad)['ok'])
        self.assertFalse(self.call(base,self.manager,'transition/status',{'projects':['project']})['result']['projects'][0]['new_admission_blocked'])

    def test_link_inventory_owner_only_candidate_refs_and_no_client_paths(self):
        service=self.compose();base=self.start_http(service)
        self.assertEqual(self.call(base,self.manager,'links/inventory',{'projects':['project']})['status'],403)
        result=self.call(base,self.owner,'links/inventory',{'projects':['project']})
        self.assertTrue(result['ok'],result)
        self.assertIn('host_map_digest',result['result'])
        self.assertEqual(self.call(base,self.owner,'links/inventory',{'projects':['project'],'templates':{'source':'/tmp/injected'}})['status'],400)
        self.assertEqual(self.call(base,self.owner,'links/get',{'plan_id':'missing'})['status'],404)

    def test_unconfigured_backup_never_accepts_endpoint_and_owner_scope_required(self):
        service=self.compose();base=self.start_http(service)
        self.assertEqual(self.call(base,self.manager,'backup/report',{})['status'],403)
        self.assertEqual(self.call(base,self.owner,'backup/report',{})['status'],404)
        self.assertEqual(self.call(base,self.owner,'backup/report',{'endpoint':'https://untrusted.example'})['status'],400)

    def test_registered_backup_health_and_rehearsal_do_not_expose_paths_or_keys(self):
        service=self.compose();base=self.start_http(service)
        for p in ('objects','stage','bench'):(self.root/p).mkdir(mode=0o700)
        key=os.urandom(32)
        encrypted=EncryptedBackup(GovernanceDurability(self.core),PrivateLocalBackupBackend(self.root/'objects'),lambda keyid:key,self.root/'stage')
        service.server.operations_control.backup_health=BackupHealth(encrypted,rehearsal_root=self.root/'bench')
        installed=self.call(base,self.owner,'backup/policy',{'policy':{'maximum_completion_age_seconds':30,'retain_latest':1}})
        self.assertTrue(installed['ok'],installed)
        encrypted.backup(self.owner,'fixture-backup','fixture-key')
        result=self.call(base,self.owner,'backup/report',{'verify_latest':True})
        self.assertTrue(result['result']['exact_readback_verified']);self.assertFalse(result['result']['offsite_verified'])
        restored=self.call(base,self.owner,'backup/rehearse',{'backup_id':'fixture-backup','request_key':'fixture-rto'})
        self.assertTrue(restored['result']['quarantined']);self.assertNotIn(str(self.root),str(restored))
        self.assertEqual(self.call(base,self.owner,'backup/rehearse',{'backup_id':'fixture-backup','request_key':'bad','destination':'/tmp/bad'})['status'],400)
        limited=self.store.add_token('limited-owner','owner',['project'])
        self.assertEqual(self.call(base,limited,'backup/report',{})['status'],403)

    def test_fully_drained_service_does_not_start_new_supervision_jobs(self):
        service=self.compose()
        service.runtime_transition.drain(self.owner,['project'],{'project':0},'stop-for-update')
        poll=Mock(side_effect=AssertionError('new background job during full drain'))
        service.supervision=SimpleNamespace(poll=poll)
        with patch.object(self.controller,'tick',return_value={}):service.run(max_ticks=1)
        poll.assert_not_called()
