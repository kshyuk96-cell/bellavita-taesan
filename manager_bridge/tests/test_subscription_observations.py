"""Synthetic native metadata only: no models/network or credential files."""
import json
import unittest
from unittest.mock import patch,MagicMock
from manager_bridge.subscription_observations import sanitize,RateWindow,NativeSubscriptionObserver
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.fleet_codex import CodexHost
from manager_bridge.store import PermissionDenied,Conflict,token_hash

class SanitizationTests(unittest.TestCase):
    def observe(self,response,account=None):
        return sanitize('project','a'*64,account or {'account':{'type':'chatgpt','email':'secret@example.test'}},response,1000)
    def test_allowlist_explicit_backend_denial_overrides_zero_percent(self):
        result=self.observe({'ordinaryUsageAllowed':False,'accountId':'secret-account','rateLimits':{'primary':{'usedPercent':0,'windowDurationMins':300,'resetsAt':2000},'credits':{'balance':'secret-money'}}}).public()
        self.assertFalse(result['ordinary_usage_allowed']);self.assertFalse(result['execution_authorized'])
        self.assertNotIn('secret',json.dumps(result));self.assertIsNone(result['api_cost'])
    def test_missing_is_unknown_not_zero_and_permission_null_stays_null(self):
        self.assertEqual(self.observe({'rateLimits':{}}).state,'unavailable')
        observation=self.observe({'rateLimits':{'secondary':{'usedPercent':23}}})
        self.assertIsNone(observation.ordinary_usage_allowed);self.assertEqual(observation.windows[0].used_percent,23)
    def test_malformed_windows_do_not_recover_any_values(self):
        for value in (-1,101,True,1.5,'0'):
            observation=self.observe({'ordinaryUsageAllowed':True,'rateLimits':{'primary':{'usedPercent':value}}})
            self.assertEqual(observation.state,'malformed');self.assertEqual(observation.windows,());self.assertIsNone(observation.ordinary_usage_allowed)
    def test_wrong_account_and_ttl_types(self):
        self.assertEqual(self.observe({'rateLimits':{}},{'account':{'type':'apiKey'}}).state,'account_unavailable')
        with self.assertRaises(ValueError):sanitize('project','a'*64,{}, {},1000,True)
        with self.assertRaises(ValueError):RateWindow('primary',1,None,True)

class GuardedObserverTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    def observer(self):
        host=MagicMock(spec=CodexHost);host.profile_guard=lambda project:None
        return NativeSubscriptionObserver(self.core,host,'dev')
    def test_only_readonly_protocol_requests_and_no_raw_identity_output(self):
        service=self.observer()
        with patch('manager_bridge.subscription_observations._Rpc') as rpc:
            request=rpc.return_value.__enter__.return_value.request
            request.side_effect=[{'account':{'type':'chatgpt','email':'private'}},{'ordinaryUsageAllowed':True,'rateLimits':{'primary':{'usedPercent':12}}}]
            result=service.read(self.dev,'project')
            self.assertEqual(request.call_args_list[0].args,('account/read',{'refreshToken':False}))
            self.assertEqual(request.call_args_list[1].args,('account/rateLimits/read',{'excludeResetCreditDetails':True,'supportsLunaReserve':False}))
            self.assertNotIn('private',json.dumps(result.public()))
    def test_revocation_during_rpc_never_returns_observation(self):
        service=self.observer()
        with patch('manager_bridge.subscription_observations._Rpc') as rpc:
            def read(method,params):
                self.store.revoke_token(self.owner,token_hash(self.dev))
                return {}
            rpc.return_value.__enter__.return_value.request.side_effect=read
            with self.assertRaises(PermissionDenied):service.read(self.dev,'project')
    def test_expired_or_unavailable_never_looks_like_empty_usage(self):
        service=self.observer()
        with patch('manager_bridge.subscription_observations._Rpc') as rpc:
            rpc.return_value.__enter__.side_effect=RuntimeError('private raw sentinel')
            result=service.read(self.dev,'project')
            self.assertEqual(result.state,'unavailable');self.assertNotIn('sentinel',json.dumps(result.public()))
        with patch('manager_bridge.subscription_observations._Rpc') as rpc:
            def read(method,params):
                self.now+=31
                return {}
            rpc.return_value.__enter__.return_value.request.side_effect=read
            with self.assertRaises(Conflict):service.read(self.dev,'project')
    def test_other_worker_cannot_observe_assigned_profile(self):
        with self.assertRaises(PermissionDenied):self.observer().read(self.outsider,'project')
