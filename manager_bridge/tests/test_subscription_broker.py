"""Managed subscription rotation fixtures; no real auth or network operation."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.subscription_broker import SubscriptionBroker,SubscriptionTarget,CodexSubscriptionRefresher
from manager_bridge.store import Conflict,PermissionDenied,NotFound,token_hash


class SubscriptionBrokerTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    task=fixtures.ThreeTierTests.task
    start=fixtures.ThreeTierTests.start
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify

    def auth(self,home,version='first',account='fixture-account',mode='chatgpt'):
        value={'auth_mode':mode,'OPENAI_API_KEY':None,'tokens':{'access_token':'synthetic-access-'+version,
              'refresh_token':'synthetic-refresh-'+version,'id_token':'synthetic-id-'+version,'account_id':account}}
        if getattr(self,'extra_token_metadata',None) is not None:value['tokens']['metadata']=self.extra_token_metadata
        path=home/'auth.json';path.write_text(json.dumps(value));path.chmod(0o600)
        return path.read_bytes()

    def broker(self,callback=None,ref='fixture-broker'):
        self.canonical=self.root/'canonical';self.devhome=self.root/'devhome';self.reviewhome=self.root/'reviewhome'
        for p in (self.canonical,self.devhome,self.reviewhome):p.mkdir(mode=0o700)
        for p in (self.canonical,self.devhome,self.reviewhome):self.auth(p)
        self.calls=[]
        def refresh(home):
            self.calls.append(home);self.auth(home,'next')
            return {'state':'authenticated','account_type':'chatgpt'}
        self.refresh=refresh if callback is None else callback
        self.targets=[SubscriptionTarget('project','dev','developer',self.devhome),SubscriptionTarget('project','reviewer','reviewer',self.reviewhome)]
        return SubscriptionBroker(self.core,ref,self.canonical,self.targets,refresh_callback=self.refresh)

    def test_rotation_managed_profiles_private_generation_and_idempotency(self):
        broker=self.broker();before=(self.devhome/'auth.json').read_bytes()
        result=broker.refresh_and_distribute(self.owner,0,'first-request')
        self.assertTrue(result['refresh_requested']);self.assertTrue(result['auth_file_changed']);self.assertEqual(result['generation'],1)
        self.assertEqual(result,broker.refresh_and_distribute(self.owner,0,'first-request'))
        self.assertEqual(len(self.calls),1)
        for home in (self.canonical,self.devhome,self.reviewhome):
            self.assertEqual((home/'auth.json').stat().st_mode&0o777,0o600)
            self.assertNotEqual(before,(home/'auth.json').read_bytes())
        self.assertEqual((self.canonical/'auth.json').read_bytes(),(self.devhome/'auth.json').read_bytes())
        raw=json.dumps(result)
        for secret in ('synthetic-access','synthetic-refresh','synthetic-id','fixture-account',self.owner):self.assertNotIn(secret,raw)
        with broker.profile_guard(self.dev,'project','dev') as evidence:self.assertEqual(evidence['generation'],1)
        self.assertEqual(broker.status(self.owner)['state'],'ready')

    def test_refresh_owner_scope_only_and_generation_cas(self):
        broker=self.broker()
        for token in (self.manager,self.dev,self.reviewer):
            with self.assertRaises(PermissionDenied):broker.refresh_and_distribute(token,0,'denied')
        limited=self.store.add_token('other-owner','owner',['other'])
        with self.assertRaises((PermissionDenied,Conflict,NotFound)):broker.refresh_and_distribute(limited,0,'scope')
        with self.assertRaises(Conflict):broker.refresh_and_distribute(self.owner,1,'wrong')
        self.assertEqual(self.calls,[])

    def test_execution_guard_serializes_refresh_and_restricts_worker(self):
        broker=self.broker();broker.refresh_and_distribute(self.owner,0,'initial')
        with broker.profile_guard(self.dev,'project','dev'):
            with self.assertRaisesRegex(Conflict,'busy'):broker.refresh_and_distribute(self.owner,1,'busy')
        with self.assertRaises(PermissionDenied):
            with broker.profile_guard(self.dev,'project','reviewer'):pass
        result=broker.refresh_and_distribute(self.owner,1,'after-guard')
        self.assertEqual(result['generation'],2)

    def test_registered_running_target_prevents_refresh(self):
        broker=self.broker();self.start(self.task())
        with self.assertRaisesRegex(Conflict,'in use'):broker.refresh_and_distribute(self.owner,0,'running')
        self.assertEqual(self.calls,[])

    def test_auth_file_changed_without_broker_prevents_refresh_and_guard(self):
        broker=self.broker();broker.refresh_and_distribute(self.owner,0,'initial');self.auth(self.devhome,'unexpected')
        with self.assertRaisesRegex(Conflict,'outside broker'):broker.refresh_and_distribute(self.owner,1,'changed')
        with self.assertRaisesRegex(Conflict,'outside broker'):
            with broker.profile_guard(self.dev,'project','dev'):pass
        self.assertEqual(len(self.calls),1)

    def test_failed_refresh_is_quarantined_without_old_token_rollback_or_replay(self):
        def failing(home):
            self.auth(home,'rotated');raise ConnectionError('synthetic-refresh-DO-NOT-LEAK')
        broker=self.broker(failing);target=(self.devhome/'auth.json').read_bytes()
        with self.assertRaisesRegex(Conflict,'native subscription refresh unavailable') as error:broker.refresh_and_distribute(self.owner,0,'failed')
        self.assertNotIn('DO-NOT-LEAK',str(error.exception))
        self.assertEqual((self.devhome/'auth.json').read_bytes(),target)
        self.assertNotEqual((self.canonical/'auth.json').read_bytes(),target)
        self.assertEqual(broker.status(self.owner)['state'],'uncertain')
        restarted=SubscriptionBroker(self.core,'fixture-broker',self.canonical,self.targets,refresh_callback=self.refresh)
        with self.assertRaisesRegex(Conflict,'requires reconciliation'):restarted.refresh_and_distribute(self.owner,0,'retry')
        with self.assertRaisesRegex(Conflict,'requires reconciliation'):
            with restarted.profile_guard(self.dev,'project','dev'):pass

    def test_revocation_during_refresh_prevents_distribution(self):
        def revoked(home):
            self.auth(home,'next');self.store.revoke_token(self.owner,token_hash(self.owner))
            return {'state':'authenticated','account_type':'chatgpt'}
        broker=self.broker(revoked);before=(self.devhome/'auth.json').read_bytes()
        with self.assertRaises(PermissionDenied):broker.refresh_and_distribute(self.owner,0,'revoked')
        self.assertEqual((self.devhome/'auth.json').read_bytes(),before)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT state FROM subscription_broker_state').fetchone()[0],'uncertain')

    def test_partial_write_failure_blocks_all_profiles_and_preserves_journal(self):
        import manager_bridge.subscription_broker as module
        broker=self.broker();original=module._atomic;calls=[]
        def fail_second(home,data,expected):
            calls.append(home)
            if len(calls)==2:raise OSError('synthetic-write-failure')
            original(home,data,expected)
        with patch.object(module,'_atomic',side_effect=fail_second):
            with self.assertRaises(OSError):broker.refresh_and_distribute(self.owner,0,'partial')
        self.assertNotEqual((self.devhome/'auth.json').read_bytes(),(self.reviewhome/'auth.json').read_bytes())
        for token,member in ((self.dev,'dev'),(self.reviewer,'reviewer')):
            with self.assertRaisesRegex(Conflict,'requires reconciliation'):
                with broker.profile_guard(token,'project',member):pass
        self.assertEqual(broker.status(self.owner)['state'],'uncertain')

    def test_api_external_mode_account_switch_and_symlink_are_refused(self):
        broker=self.broker();self.auth(self.canonical,mode='apikey')
        with self.assertRaises(ValueError):broker.refresh_and_distribute(self.owner,0,'api')
        self.auth(self.canonical)
        (self.devhome/'auth.json').unlink();(self.devhome/'auth.json').symlink_to(self.reviewhome/'auth.json')
        with self.assertRaises(ValueError):broker.refresh_and_distribute(self.owner,0,'link')

    def test_request_replay_does_not_hide_later_auth_change(self):
        broker=self.broker();broker.refresh_and_distribute(self.owner,0,'request');self.auth(self.devhome,'changed')
        with self.assertRaisesRegex(Conflict,'outside broker'):broker.refresh_and_distribute(self.owner,0,'request')

    def test_same_profile_and_changed_registered_binding_are_refused(self):
        broker=self.broker()
        with self.assertRaises(ValueError):SubscriptionBroker(self.core,'other-broker',self.canonical,[SubscriptionTarget('project','dev','developer',self.canonical)])
        with self.assertRaises(Conflict):SubscriptionBroker(self.core,'fixture-broker',self.canonical,self.targets[:1])
        self.store.revoke_token(self.owner,token_hash(self.reviewer))
        with self.assertRaises(PermissionDenied):broker.refresh_and_distribute(self.owner,0,'revoked-target')

    def test_guard_rechecks_authority_and_never_adopts_independent_refresh(self):
        broker=self.broker();broker.refresh_and_distribute(self.owner,0,'initial')
        with self.assertRaisesRegex(Conflict,'changed during use'):
            with broker.profile_guard(self.dev,'project','dev'):
                self.auth(self.devhome,'independently-refreshed')
        self.assertEqual(broker.status(self.owner)['state'],'uncertain')

    def test_guard_rechecks_caller_revocation(self):
        broker=self.broker();broker.refresh_and_distribute(self.owner,0,'initial')
        with self.assertRaises(PermissionDenied):
            with broker.profile_guard(self.dev,'project','dev'):
                self.store.revoke_token(self.owner,token_hash(self.dev))
        self.assertEqual(broker.status(self.owner)['state'],'uncertain')

    def test_native_callback_only_requests_managed_metadata_refresh(self):
        from manager_bridge.fleet_codex import CodexHost,ProjectBinding
        broker=self.broker()
        host=CodexHost('/bin/true',self.canonical,{'project':ProjectBinding('project','fixture/project',self.root,require_origin=False)})
        refresh=CodexSubscriptionRefresher(host,'project')
        with patch('manager_bridge.fleet_codex._Rpc') as rpc:
            rpc.return_value.__enter__.return_value.request.return_value={'account':{'type':'chatgpt','email':'fixture-only@example.invalid'}}
            self.assertEqual(refresh(self.canonical),{'state':'authenticated','account_type':'chatgpt'})
            rpc.return_value.__enter__.return_value.request.assert_called_once_with('account/read',{'refreshToken':True})

    def test_request_identifiers_never_persist_known_secrets(self):
        broker=self.broker()
        for key in (self.owner,'synthetic-refresh-first'):
            with self.assertRaises(ValueError):broker.refresh_and_distribute(self.owner,0,key)
        self.assertEqual(self.calls,[])
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT count(*) FROM subscription_broker_requests').fetchone()[0],0)

    def test_initial_generation_is_unverified_until_owner_refresh(self):
        broker=self.broker()
        self.assertEqual(broker.status(self.owner)['state'],'unverified')
        with self.assertRaisesRegex(Conflict,'requires reconciliation'):
            with broker.profile_guard(self.dev,'project','dev'):pass
        self.assertEqual(self.calls,[])

    def test_revoked_second_target_is_fenced_before_each_copy(self):
        import manager_bridge.subscription_broker as module
        from contextlib import contextmanager
        broker=self.broker();original_atomic=module._atomic;original_tx=self.store._transaction
        before=(self.reviewhome/'auth.json').read_bytes();state={'copied':False,'revoked':False}
        def atomic(home,data,expected):
            original_atomic(home,data,expected)
            if home==self.devhome:state['copied']=True
        @contextmanager
        def transaction():
            with original_tx() as c:yield c
            if state['copied'] and not state['revoked']:
                state['revoked']=True
                self.store.revoke_token(self.owner,token_hash(self.reviewer))
        with patch.object(module,'_atomic',side_effect=atomic),patch.object(self.store,'_transaction',transaction):
            with self.assertRaises(PermissionDenied):broker.refresh_and_distribute(self.owner,0,'revocation-race')
        self.assertEqual((self.reviewhome/'auth.json').read_bytes(),before)
        self.assertNotEqual((self.devhome/'auth.json').read_bytes(),before)
        self.assertEqual(broker.status(self.owner)['state'],'uncertain')

    def test_restored_authority_cannot_refresh_or_use_profiles(self):
        broker=self.broker();broker.refresh_and_distribute(self.owner,0,'initial')
        with self.store._transaction() as c:
            c.execute('CREATE TABLE governance_restore_marker (reconciliation_required INTEGER)')
            c.execute('INSERT INTO governance_restore_marker VALUES (1)')
        self.assertTrue(broker.status(self.owner)['recovery_quarantined'])
        with self.assertRaisesRegex(Conflict,'restored subscription'):
            broker.refresh_and_distribute(self.owner,1,'restored')
        with self.assertRaisesRegex(Conflict,'restored subscription'):
            with broker.profile_guard(self.dev,'project','dev'):pass
        self.assertEqual(len(self.calls),1)

    def test_profile_ownership_is_unique_across_brokers_and_roles(self):
        broker=self.broker();other=self.root/'other-canonical';other.mkdir(mode=0o700);self.auth(other)
        with self.assertRaisesRegex(Conflict,'another broker'):
            SubscriptionBroker(self.core,'second-broker',other,self.targets,refresh_callback=self.refresh)
        with self.assertRaisesRegex(Conflict,'another broker'):
            SubscriptionBroker(self.core,'second-broker',self.devhome,[SubscriptionTarget('project','dev','developer',other)],refresh_callback=self.refresh)
        # Reverse canonical-as-target overlap is also refused.
        with self.assertRaisesRegex(Conflict,'another broker'):
            SubscriptionBroker(self.core,'second-broker',other,[SubscriptionTarget('project','dev','developer',self.canonical)],refresh_callback=self.refresh)
        restarted=SubscriptionBroker(self.core,'fixture-broker',self.canonical,self.targets,refresh_callback=self.refresh)
        self.assertEqual(restarted.status(self.owner)['generation'],0)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM subscription_broker_state').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM subscription_profile_owners').fetchone()[0],3)

    def test_legacy_ownership_migration_cannot_choose_between_unknown_brokers(self):
        broker=self.broker()
        with self.store._transaction() as c:c.execute('DELETE FROM subscription_profile_owners')
        # Only this exact configured legacy broker is recoverable from its full current binding.
        SubscriptionBroker(self.core,'fixture-broker',self.canonical,self.targets,refresh_callback=self.refresh)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM subscription_profile_owners').fetchone()[0],3)
            c.execute("INSERT INTO subscription_broker_state SELECT 'unmapped-legacy','legacy-home',binding,generation,state,canonical_digest,target_digests,updated FROM subscription_broker_state WHERE ref='fixture-broker'")
        with self.assertRaisesRegex(Conflict,'explicit migration'):
            SubscriptionBroker(self.core,'fixture-broker',self.canonical,self.targets,refresh_callback=self.refresh)

    def test_durable_profile_claim_loss_prevents_guard_or_refresh(self):
        broker=self.broker();broker.refresh_and_distribute(self.owner,0,'initial')
        with self.store._transaction() as c:c.execute("DELETE FROM subscription_profile_owners WHERE profile_key!='canonical'")
        with self.assertRaisesRegex(Conflict,'ownership changed'):
            with broker.profile_guard(self.dev,'project','dev'):pass
        with self.assertRaisesRegex(Conflict,'ownership changed'):
            broker.refresh_and_distribute(self.owner,1,'unowned')

    def test_physical_directory_aliases_cannot_evade_ownership(self):
        broker=self.broker();other=self.root/'alias-canonical';other.mkdir(mode=0o700);self.auth(other)
        aliases=[SubscriptionTarget(t.project,t.member_id,t.role,Path('/'+str(t.home))) for t in self.targets]
        with self.assertRaisesRegex(Conflict,'another broker'):
            SubscriptionBroker(self.core,'alias-broker',other,aliases,refresh_callback=self.refresh)
        with self.assertRaises(ValueError):
            SubscriptionBroker(self.core,'same-dir-broker',self.canonical,[SubscriptionTarget('project','dev','developer',Path('/'+str(self.canonical)))])
        self.assertEqual(broker.status(self.owner)['state'],'unverified')

    def test_nested_sdk_metadata_preserved_without_treating_objects_as_secrets(self):
        self.extra_token_metadata={'kind':'pro','details':{'revision':1,'nullable':None},'flags':['ready',True]}
        broker=self.broker()
        self.assertEqual(broker.status(self.owner)['state'],'unverified')
        receipt=broker.refresh_and_distribute(self.owner,0,'metadata-compatible')
        self.assertEqual(receipt['state'],'ready')
        self.assertEqual(broker.status(self.owner)['state'],'ready')
        for home in (self.canonical,self.devhome,self.reviewhome):
            value=json.loads((home/'auth.json').read_text())
            self.assertEqual(value['tokens']['metadata'],self.extra_token_metadata)
        with self.assertRaises(ValueError):broker.refresh_and_distribute(self.owner,1,'synthetic-refresh-next')
        self.assertEqual(len(self.calls),1)
        raw=json.dumps(receipt)
        for key in ('synthetic-access-next','synthetic-refresh-next','synthetic-id-next','fixture-account'):
            self.assertNotIn(key,raw)

    def test_unknown_callback_never_confers_ready_or_api_fallback(self):
        broker=self.broker(lambda home:{'state':'authenticated','account_type':'apiKey'})
        with self.assertRaisesRegex(Conflict,'not verified'):broker.refresh_and_distribute(self.owner,0,'api-fallback')
        self.assertEqual(broker.status(self.owner)['state'],'uncertain')
