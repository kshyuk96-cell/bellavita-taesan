"""Enrollment fixtures only; synthetic credentials, no native calls."""
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_subscription_broker as fixtures
from manager_bridge.subscription_broker import SubscriptionTarget
from manager_bridge.subscription_enrollment import SubscriptionEnrollment
from manager_bridge.store import Conflict,PermissionDenied,token_hash

class EnrollmentTests(unittest.TestCase):
    setUp=fixtures.SubscriptionBrokerTests.setUp
    auth=fixtures.SubscriptionBrokerTests.auth
    broker=fixtures.SubscriptionBrokerTests.broker
    observe=fixtures.SubscriptionBrokerTests.observe
    verify=fixtures.SubscriptionBrokerTests.verify
    def ready(self):
        b=self.broker();b.refresh_and_distribute(self.owner,0,'fixture-ready')
        return b,SubscriptionEnrollment(b)
    def additions(self):
        result=[]
        for member,role in [('dev','developer'),('reviewer','reviewer')]:
            home=self.root/('other-'+member);home.mkdir(mode=0o700)
            result.append(SubscriptionTarget('other',member,role,home))
        return result
    def test_exact_add_only_generation_and_old_instance_fail_closed(self):
        b,e=self.ready();old=(self.devhome/'auth.json').read_bytes();targets=self.additions()
        plan=e.plan(self.owner,targets,1);new=e.apply(self.owner,plan)
        self.assertEqual(new.status(self.owner)['generation'],2)
        self.assertEqual(new.status(self.owner)['target_count'],4)
        self.assertEqual((self.devhome/'auth.json').read_bytes(),old)
        for target in targets:
            self.assertEqual((target.home/'auth.json').read_bytes(),old)
            self.assertEqual((target.home/'auth.json').stat().st_mode&0o777,0o600)
        with self.assertRaises(Conflict):b.status(self.owner)
        with new.profile_guard(self.dev,'other','dev'):pass
        with self.assertRaises(Conflict):e.apply(self.owner,plan)
        self.assertEqual(len(self.calls),1)
    def test_owner_only_stale_generation_and_nonempty_profiles(self):
        b,e=self.ready();targets=self.additions()
        with self.assertRaises(PermissionDenied):e.plan(self.manager,targets,1)
        with self.assertRaises(Conflict):e.plan(self.owner,targets,0)
        (targets[0].home/'existing').write_text('preserve')
        with self.assertRaises(Conflict):e.plan(self.owner,targets,1)
    def test_guard_busy_and_revocation_after_plan(self):
        b,e=self.ready();targets=self.additions();plan=e.plan(self.owner,targets,1)
        with b.profile_guard(self.dev,'project','dev'):
            with self.assertRaises(Conflict):e.apply(self.owner,plan)
        self.store.revoke_token(self.owner,token_hash(self.dev))
        with self.assertRaises(PermissionDenied):e.apply(self.owner,plan)
        self.assertFalse((targets[0].home/'auth.json').exists())
    def test_partial_copy_crash_uncertain_no_replay_and_old_bytes_preserved(self):
        from manager_bridge.subscription_enrollment import _atomic
        b,e=self.ready();targets=self.additions();plan=e.plan(self.owner,targets,1);calls=[]
        def crash(home,data,expected):
            calls.append(home)
            if len(calls)==2:raise RuntimeError('fixture crash')
            return _atomic(home,data,expected)
        with patch('manager_bridge.subscription_enrollment._atomic',side_effect=crash):
            with self.assertRaises(RuntimeError):e.apply(self.owner,plan)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT state FROM subscription_broker_state').fetchone()[0],'uncertain')
        self.assertTrue((targets[0].home/'auth.json').exists());self.assertFalse((targets[1].home/'auth.json').exists())
        with self.assertRaises(Conflict):e.apply(self.owner,plan)
    def test_physical_alias_and_config_change_rejected(self):
        b,e=self.ready();targets=self.additions()
        alias=SubscriptionTarget('other','dev','developer',self.devhome)
        with self.assertRaises(Conflict):e.plan(self.owner,[alias],1)
        plan=e.plan(self.owner,targets,1)
        config=self.registry.get(self.owner,'other')
        self.registry.register(self.owner,'other',{**config['config'],'name':'new config'},expected_revision=config['revision'])
        with self.assertRaises((Conflict,PermissionDenied)):e.apply(self.owner,plan)
    def test_directory_mode_and_symlink_are_not_adopted(self):
        b,e=self.ready();targets=self.additions();targets[0].home.chmod(0o755)
        with self.assertRaises((Conflict,ValueError)):e.plan(self.owner,targets,1)
        targets[0].home.chmod(0o700)
        link=self.root/'profile-alias';link.symlink_to(targets[0].home,target_is_directory=True)
        with self.assertRaises(ValueError):SubscriptionTarget('other','dev','developer',link)
    def test_changed_plan_authority_never_creates_credentials(self):
        from dataclasses import replace
        b,e=self.ready();targets=self.additions();plan=e.plan(self.owner,targets,1)
        with self.assertRaises(Conflict):e.apply(self.owner,replace(plan,authority_digest='0'*64))
        self.assertEqual(b.status(self.owner)['state'],'ready')
        self.assertFalse(any((t.home/'auth.json').exists() for t in targets))
