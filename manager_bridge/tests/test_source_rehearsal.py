from pathlib import Path
import subprocess
import tempfile
import unittest
from manager_bridge.source_rehearsal import observe_checkout,snapshot_profile
from manager_bridge.store import Conflict


class SourceRehearsal(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory());self.path=Path(self.temp)/'source';self.path.mkdir()
        self.git('init');(self.path/'file.txt').write_text('synthetic')
        self.git('add','.');self.git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','Fixture')
        self.sha=self.git('rev-parse','HEAD')
        self.record={'pin':{'project':'a','source_kind':'github','source_ref':'fixture/a','branch':'main','sha':self.sha,'revision':'v1'},
                     'observed_at':1000,'fixture_tests':{'passed':5,'failed':0,'skipped':0},'build':'not_applicable'}

    def git(self,*args):
        return subprocess.check_output(['/usr/bin/git','-c','core.hooksPath=/dev/null','-C',str(self.path),*args],stderr=subprocess.DEVNULL,text=True).strip()

    def test_copy_and_tests_cannot_attest_sandbox_or_live_connections(self):
        host=snapshot_profile(self.record,self.path)
        self.assertEqual(host.observe('a','fixture_tests').state,'verified')
        for capability in ('sandbox','receiver','model','host','customer_acceptance'):
            self.assertIsNone(host.observe('a',capability))
        facts=observe_checkout(self.path)
        self.assertFalse(facts['security_sandbox_verified']);self.assertFalse(facts['remote_rechecked'])

    def test_source_changed_after_registration_is_blocked(self):
        host=snapshot_profile(self.record,self.path)
        (self.path/'file.txt').write_text('changed')
        with self.assertRaises(Conflict):host.observe('a','fixture_tests')

    def test_wrong_snapshot_commit_rejected(self):
        self.record['pin']['sha']='a'*40
        with self.assertRaises(Conflict):snapshot_profile(self.record,self.path)

    def test_symlink_or_subdirectory_not_accepted_as_registered_root(self):
        link=Path(self.temp)/'link';link.symlink_to(self.path,target_is_directory=True)
        with self.assertRaises(ValueError):observe_checkout(link)
        (self.path/'sub').mkdir()
        with self.assertRaises(Conflict):observe_checkout(self.path/'sub')

    def test_skips_are_partial_and_observation_time_preserved(self):
        self.record['fixture_tests']={'passed':97,'failed':0,'skipped':6}
        fact=snapshot_profile(self.record,self.path).observe('a','fixture_tests')
        self.assertEqual(fact.state,'partial');self.assertEqual(fact.skipped,6)
        self.assertEqual(fact.observed_at,1000)

    def test_ignored_dependencies_not_security_attested(self):
        (self.path/'.gitignore').write_text('deps/\n');self.git('add','.gitignore');self.git('commit','-m','Ignore fixture deps')
        (self.path/'deps').mkdir();(self.path/'deps'/'package.txt').write_text('synthetic')
        fact=observe_checkout(self.path)
        self.assertTrue(fact['tracked_and_visible_untracked_clean'])
        self.assertFalse(fact['ignored_dependencies_audited'])
