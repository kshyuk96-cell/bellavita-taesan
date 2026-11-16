"""Executor boundary tests; fixture engines do not prove real host isolation."""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from manager_bridge.store import Conflict, PermissionDenied, digest
from manager_bridge.three_tier_runner import (ReviewProfile,FixedReviewRunner,ReviewUnavailable,
    BubblewrapReviewEngine,_archive,_extract,_tree_digest,_runtime_digest,_run_bounded,MAX_ARCHIVE,MAX_RUNTIME_BYTES)


class FixtureSandboxEngine:
    """Protocol fixture only. Its isolation flag is simulated, never live proof."""
    def __init__(self,callback=None,available=True):
        self.callback=callback;self.available=available;self.calls=[]
    def probe(self):
        return {'available':self.available,'isolation_verified':self.available}
    def execute_snapshot(self,profile,work,outputs,checks,heartbeat):
        self.calls.append((work,outputs,checks));heartbeat()
        if self.callback:return self.callback(profile,work,outputs,checks,heartbeat)
        return [{'name':name,'argv_digest':digest(list(argv)),'exit_code':0,'elapsed_seconds':0.001} for name,argv in checks]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source=self.root/'source';self.source.mkdir()
        self.runtime=self.root/'runtime';self.runtime.mkdir()
        (self.source/'fixture.txt').write_text('frozen synthetic input\n')
        (self.source/'.gitignore').write_text('private/\n')
        self.git('init');self.git('add','.');self.git('commit','-m','Synthetic review fixture')
        self.sha=self.git('rev-parse','HEAD');self.now=1000.0
        self.profile=ReviewProfile('fixture-project','fixture/repo',self.sha,self.sha,self.source,self.runtime,
            {'checks':('/usr/bin/python3','-c','assert True')},timeout=2)
        self.task={'id':'a'*32,'manifest':{'project':'fixture-project','repository':'fixture/repo',
            'base_sha':self.sha,'checks':['checks']}}
        self.engine=FixtureSandboxEngine();self.runner=FixedReviewRunner(self.profile,engine=self.engine,clock=lambda:self.now)
        self.reviewer={'subject':'reviewer','project':'fixture-project','role':'reviewer'}

    def git(self,*args):
        return subprocess.check_output(['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','user.name=Fixture',
            '-c','user.email=fixture@example.invalid','-C',str(self.source),*args],stderr=subprocess.DEVNULL,text=True).strip()

    def run_review(self,candidate,reviewer):
        return self.runner(candidate,reviewer,heartbeat=lambda:None)

    def candidate(self):
        return {'digest':'b'*64,'source':asdict(self.runner.observe(self.task)),
                'task':self.task,'developer_subject':'developer'}

    def test_observer_exact_archive_and_registered_argv_digest(self):
        observation=self.runner.observe(self.task)
        self.assertEqual(observation.artifact_digest,hashlib.sha256(_archive(self.profile)).hexdigest())
        self.assertEqual(observation.checks_digest,digest({'checks':digest(list(self.profile.checks['checks']))}))
        self.assertEqual(observation.head_sha,self.sha)

    def test_independent_snapshot_excludes_ignored_credentials_and_git(self):
        private=self.source/'private';private.mkdir();(private/'fixture-secret').write_text('never-copy-this')
        def inspect(profile,work,outputs,checks,heartbeat):
            self.assertNotEqual(work,profile.source)
            self.assertEqual((work/'fixture.txt').read_text(),'frozen synthetic input\n')
            self.assertFalse((work/'.git').exists());self.assertFalse((work/'private').exists())
            return [{'name':n,'argv_digest':digest(list(a)),'exit_code':0,'elapsed_seconds':0} for n,a in checks]
        self.engine.callback=inspect
        proof=self.run_review(self.candidate(),self.reviewer)
        self.assertEqual(proof.artifact_digest,self.candidate()['source']['artifact_digest'])
        self.assertEqual(len(proof.checks),1)
        self.assertNotIn('never-copy-this',repr(proof))

    def test_unregistered_requested_command_never_executes(self):
        self.task['manifest']['checks']=['curl arbitrary']
        with self.assertRaises(PermissionDenied):self.runner.observe(self.task)
        self.assertFalse(self.engine.calls)

    def test_duplicate_checks_rejected(self):
        self.task['manifest']['checks']=['checks','checks']
        with self.assertRaises(PermissionDenied):self.runner.observe(self.task)

    def test_source_project_and_base_are_bound(self):
        for key,value in [('project','other'),('repository','other/repo'),('base_sha','f'*40)]:
            task={'id':self.task['id'],'manifest':dict(self.task['manifest'],**{key:value})}
            with self.subTest(key=key),self.assertRaises(PermissionDenied):self.runner.observe(task)

    def test_missing_isolation_never_executes(self):
        candidate=self.candidate();self.engine.available=False
        with self.assertRaises(ReviewUnavailable):self.run_review(candidate,self.reviewer)
        self.assertFalse(self.engine.calls)

    def test_no_plain_execution_fallback_on_real_engine_failure(self):
        engine=BubblewrapReviewEngine()
        with patch.object(engine,'probe',return_value={'available':False,'isolation_verified':False}),patch('manager_bridge.three_tier_runner._run_bounded') as run:
            with self.assertRaises(ReviewUnavailable):engine.execute_snapshot(self.profile,self.source,self.root,[],lambda:None)
            run.assert_not_called()

    def test_actual_production_probe_is_explicit_without_false_pass(self):
        result=BubblewrapReviewEngine().probe()
        self.assertIs(type(result['isolation_verified']),bool)
        self.assertEqual(result['available'],result['isolation_verified'])
        self.assertIn(result['state'],{'ready','sandbox_unavailable'})

    def test_profile_symlink_and_source_runtime_overlap_rejected(self):
        link=self.root/'linked';link.symlink_to(self.source,target_is_directory=True)
        with self.assertRaises(ValueError):replace(self.profile,source=link)
        with self.assertRaises(ValueError):replace(self.profile,runtime=self.source)

    def test_committed_source_symlink_rejected(self):
        (self.source/'linked-file').symlink_to('fixture.txt')
        self.git('add','.');self.git('commit','-m','Synthetic link')
        runner=FixedReviewRunner(replace(self.profile,head_sha=self.git('rev-parse','HEAD')),engine=self.engine)
        with self.assertRaises(ReviewUnavailable):runner.observe(self.task)

    def test_git_export_ignore_cannot_hide_candidate_inputs(self):
        (self.source/'.gitattributes').write_text('fixture.txt export-ignore\n')
        self.git('add','.');self.git('commit','-m','Synthetic export-ignore')
        runner=FixedReviewRunner(replace(self.profile,head_sha=self.git('rev-parse','HEAD')),engine=self.engine)
        with self.assertRaisesRegex(ReviewUnavailable,'omits tracked'):runner.observe(self.task)

    def test_changed_source_before_execution_blocks(self):
        candidate=self.candidate();(self.source/'fixture.txt').write_text('changed')
        with self.assertRaises(Conflict):self.run_review(candidate,self.reviewer)
        self.assertFalse(self.engine.calls)

    def test_changed_source_during_execution_blocks(self):
        candidate=self.candidate()
        def changed(profile,work,outputs,checks,heartbeat):
            (profile.source/'fixture.txt').write_text('changed during review')
            heartbeat()
            return [{'name':n,'argv_digest':digest(list(a)),'exit_code':0,'elapsed_seconds':0} for n,a in checks]
        self.engine.callback=changed
        with self.assertRaises(Conflict):self.run_review(candidate,self.reviewer)

    def test_changed_independent_input_blocks_even_if_command_claims_success(self):
        def changed(profile,work,outputs,checks,heartbeat):
            (work/'fixture.txt').write_text('tampered fixture')
            return [{'name':n,'argv_digest':digest(list(a)),'exit_code':0,'elapsed_seconds':0} for n,a in checks]
        self.engine.callback=changed
        with self.assertRaises(Conflict):self.run_review(self.candidate(),self.reviewer)

    def test_frozen_artifact_and_checkspec_mismatch_blocks(self):
        for key in ('artifact_digest','checks_digest','head_sha'):
            candidate=self.candidate();candidate['source'][key]='f'*(40 if key=='head_sha' else 64)
            with self.subTest(key=key),self.assertRaises(Conflict):self.run_review(candidate,self.reviewer)
        self.assertFalse(self.engine.calls)

    def test_author_cannot_review_own_work(self):
        with self.assertRaises(PermissionDenied):self.run_review(self.candidate(),dict(self.reviewer,subject='developer'))
        self.assertFalse(self.engine.calls)

    def test_failed_actual_exit_remains_failed_without_fabricated_test_count(self):
        self.engine.callback=lambda p,w,o,c,h:[{'name':n,'argv_digest':digest(list(a)),'exit_code':1,'elapsed_seconds':0.01} for n,a in c]
        proof=self.run_review(self.candidate(),self.reviewer)
        self.assertEqual(proof.checks[0].exit_code,1)
        self.assertFalse(hasattr(proof,'tests_passed'));self.assertNotIn('passed',asdict(proof.checks[0]))

    def test_malformed_missing_or_forged_outcomes_rejected(self):
        candidates=[[],[{'name':'checks','passed':True}],
            [{'name':'checks','argv_digest':'f'*64,'exit_code':0,'elapsed_seconds':0}],
            [{'name':'checks','argv_digest':digest(list(self.profile.checks['checks'])),'exit_code':True,'elapsed_seconds':0}]]
        for results in candidates:
            self.engine.callback=lambda *args:results
            with self.subTest(results=results),self.assertRaises(ReviewUnavailable):self.run_review(self.candidate(),self.reviewer)

    def test_loss_of_isolation_after_checks_blocks_proof(self):
        def changed(p,w,o,c,h):
            self.engine.available=False
            return [{'name':n,'argv_digest':digest(list(a)),'exit_code':0,'elapsed_seconds':0.01} for n,a in c]
        self.engine.callback=changed
        with self.assertRaises(ReviewUnavailable):self.run_review(self.candidate(),self.reviewer)

    def test_real_local_fixture_command_executes_without_claiming_isolation(self):
        # This proves command execution/sanitized env only. It does not run the
        # production sandbox and is NOT submitted to ThreeTier as a passing proof.
        destination=self.root/'actual-fixture-output.txt'
        script='import os,pathlib,sys; assert not os.getenv("OPENAI_API_KEY"); pathlib.Path(sys.argv[1]).write_text("actual-fixture")'
        with patch.dict(os.environ,{'OPENAI_API_KEY':'synthetic-sentinel-not-a-key'}):
            result=_run_bounded(['/usr/bin/python3','-c',script,str(destination)],cwd=self.root,timeout=2,heartbeat=lambda:None)
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(destination.read_text(),'actual-fixture')
        self.assertNotIn('synthetic-sentinel',json.dumps(result))

    def test_real_local_failing_command_and_output_are_not_success(self):
        result=_run_bounded(['/usr/bin/python3','-c','import sys; print("private-output"); sys.exit(7)'],cwd=self.root,timeout=2,heartbeat=lambda:None)
        self.assertEqual(result['exit_code'],7)
        self.assertNotIn('private-output',json.dumps(result))

    def test_real_local_timeout_kills_command(self):
        with self.assertRaises(ReviewUnavailable):
            _run_bounded(['/usr/bin/python3','-c','import time; time.sleep(30)'],cwd=self.root,timeout=0.05,heartbeat=lambda:None)

    def test_host_owned_commands_require_sandbox_absolute_executable(self):
        for argv in (('python','-c','pass'),('/runtime/../host/bin/python','-c','pass'),('/home/user/bin/tool',)):
            with self.subTest(argv=argv),self.assertRaises(ValueError):replace(self.profile,checks={'checks':argv})

    def test_missing_per_request_authorization_heartbeat_is_rejected(self):
        with self.assertRaises(ReviewUnavailable):self.runner(self.candidate(),self.reviewer)
        self.assertFalse(self.engine.calls)

    def test_per_request_revocation_heartbeat_aborts_without_proof(self):
        candidate=self.candidate();calls=[]
        def revoked():
            calls.append(True)
            if len(calls)>1:raise PermissionDenied('fixture authorization revoked')
        with self.assertRaises(PermissionDenied):self.runner(candidate,self.reviewer,heartbeat=revoked)

    def test_real_namespace_engine_runs_frozen_fixture(self):
        """Actual isolation/file/command test; skip when host forbids namespaces."""
        engine=BubblewrapReviewEngine()
        if engine.probe().get('isolation_verified') is not True:
            self.skipTest('real namespace sandbox unavailable on selected host')
        nonce='synthetic-three-tier-review-fixture'
        inode=os.stat('/proc/self/ns/net').st_ino
        script='''import errno,json,os,pathlib,resource,sys
assert resource.getrlimit(resource.RLIMIT_NPROC)==(128,128)
assert resource.getrlimit(resource.RLIMIT_NOFILE)==(256,256)
assert resource.getrlimit(resource.RLIMIT_FSIZE)==(268435456,268435456)
assert resource.getrlimit(resource.RLIMIT_CORE)==(0,0)
assert resource.getrlimit(resource.RLIMIT_AS)==(1610612736,1610612736)
mode,nonce,parent_net=sys.argv[1:]
assert str(pathlib.Path.cwd())=='/work'
assert not pathlib.Path('/workspace').exists()
assert not pathlib.Path('/home/agent').exists()
assert not pathlib.Path('/work/.git').exists()
assert not any(os.getenv(k) for k in ('OPENAI_API_KEY','GOOGLE_APPLICATION_CREDENTIALS','GH_TOKEN'))
assert os.stat('/proc/self/ns/net').st_ino!=int(parent_net)
p=pathlib.Path('/outputs/proof.json')
expected={'nonce':nonce,'independent_source':True,'outside_hidden':True,'credentials_absent':True,'network_namespace_distinct':True,'source_readonly':True}
if mode=='write':
 try:pathlib.Path('/work/fixture.txt').write_text('forbidden')
 except OSError as e:assert e.errno in (errno.EROFS,errno.EACCES)
 else:raise AssertionError('source is writable')
 p.write_text(json.dumps(expected,sort_keys=True))
else:
 assert json.loads(p.read_text())==expected
 assert pathlib.Path('/work/fixture.txt').read_text()=='frozen synthetic input\\n'
'''
        (self.source/'fixture_check.py').write_text(script)
        self.git('add','.');self.git('commit','-m','Actual isolated review fixture')
        head=self.git('rev-parse','HEAD')
        checks={
            'write_fixture':('/usr/bin/python3','/work/fixture_check.py','write',nonce,str(inode)),
            'verify_fixture':('/usr/bin/python3','/work/fixture_check.py','verify',nonce,str(inode)),
        }
        profile=replace(self.profile,head_sha=head,checks=checks,timeout=5)
        runner=FixedReviewRunner(profile,clock=lambda:self.now)
        task={'id':self.task['id'],'manifest':dict(self.task['manifest'],checks=list(checks))}
        observed=runner.observe(task)
        candidate={'digest':'b'*64,'source':asdict(observed),'task':task,'developer_subject':'developer'}
        heartbeat=[]
        proof=runner(candidate,self.reviewer,heartbeat=lambda:heartbeat.append(True))
        self.assertTrue(proof.isolation_verified)
        self.assertEqual([x.exit_code for x in proof.checks],[0,0])
        self.assertEqual(set(observed.changed_paths),{'fixture_check.py'})
        self.assertGreaterEqual(len(heartbeat),5)
        self.assertEqual((self.source/'fixture.txt').read_text(),'frozen synthetic input\n')

    def test_runtime_mutation_before_execution_invalidates_candidate(self):
        candidate=self.candidate();(self.runtime/'fixture-runtime.txt').write_text('changed runtime')
        with self.assertRaises(Conflict):self.run_review(candidate,self.reviewer)
        self.assertFalse(self.engine.calls)

    def test_runtime_mutation_during_execution_blocks_evidence(self):
        def changed(profile,work,outputs,checks,heartbeat):
            (profile.runtime/'fixture-runtime.txt').write_text('changed during review')
            return [{'name':n,'argv_digest':digest(list(a)),'exit_code':0,'elapsed_seconds':0} for n,a in checks]
        self.engine.callback=changed
        with self.assertRaises(Conflict):self.run_review(self.candidate(),self.reviewer)

    def test_runtime_symlink_fails_closed_before_commands(self):
        (self.runtime/'linked').symlink_to(self.source/'fixture.txt')
        with self.assertRaises(Conflict):self.runner.observe(self.task)
        self.assertFalse(self.engine.calls)

    def test_fixed_runtime_budget_accepts_large_binary_without_expanding_source(self):
        binary=self.runtime/'node'
        with binary.open('wb') as stream:stream.truncate(127*1024*1024)
        self.assertEqual(256*1024*1024,MAX_RUNTIME_BYTES)
        self.assertEqual(64*1024*1024,MAX_ARCHIVE)
        runtime_hash=_runtime_digest(self.runtime)
        self.assertEqual(64,len(runtime_hash))
        with self.assertRaisesRegex(Conflict,'too large'):_tree_digest(self.runtime)
        self.assertEqual(runtime_hash,self.runner.observe(self.task).runtime_digest)
        with binary.open('wb') as stream:stream.truncate(MAX_RUNTIME_BYTES+1)
        with self.assertRaisesRegex(Conflict,'too large'):_runtime_digest(self.runtime)

    def test_trusted_dependency_mount_readonly_and_fingerprinted(self):
        modules=self.runtime/'node_modules';modules.mkdir();(modules/'fixture.js').write_text('module.exports=42')
        engine=BubblewrapReviewEngine();seen=[]
        def run(argv,**kwargs):
            seen.append(argv)
            self.assertTrue((self.root/'work'/'node_modules').is_dir())
            return {'exit_code':0,'elapsed_seconds':0.01}
        work=self.root/'work';work.mkdir()
        with patch.object(engine,'probe',return_value={'available':True,'isolation_verified':True}),patch('manager_bridge.three_tier_runner._run_bounded',side_effect=run):
            engine.execute_snapshot(self.profile,work,self.root,[('checks',self.profile.checks['checks'])],lambda:None)
        argv=seen[0];position=argv.index(str(modules))
        self.assertEqual('--ro-bind',argv[position-1]);self.assertEqual('/work/node_modules',argv[position+1])
        candidate=self.candidate();(modules/'fixture.js').write_text('changed')
        with self.assertRaises(Conflict):self.run_review(candidate,self.reviewer)

    def test_dependencies_cannot_shadow_candidate_or_use_links(self):
        modules=self.runtime/'node_modules';modules.mkdir()
        (self.source/'node_modules').mkdir();(self.source/'node_modules'/'bad.js').write_text('tracked')
        self.git('add','.');self.git('commit','-m','Forbidden tracked dependencies')
        profile=replace(self.profile,head_sha=self.git('rev-parse','HEAD'))
        runner=FixedReviewRunner(profile,engine=self.engine,clock=lambda:self.now)
        with self.assertRaisesRegex(ReviewUnavailable,'candidate dependencies'):runner.observe(self.task)
        self.assertFalse(self.engine.calls)
        (modules/'outside').symlink_to(self.source/'fixture.txt')
        with self.assertRaises(Conflict):_runtime_digest(self.runtime)

    def test_nested_tracked_dependencies_rejected_in_archive_and_extract(self):
        import io,tarfile
        modules=self.source/'src'/'node_modules';modules.mkdir(parents=True)
        (modules/'package.js').write_text('candidate dependency shadow')
        self.git('add','.');self.git('commit','-m','Nested forbidden dependency')
        profile=replace(self.profile,head_sha=self.git('rev-parse','HEAD'))
        with self.assertRaisesRegex(ReviewUnavailable,'candidate dependencies'):_archive(profile)
        for name in ('node_modules/x.js','src/node_modules/x.js','src/deep/node_modules/x.js'):
            stream=io.BytesIO()
            with tarfile.open(fileobj=stream,mode='w') as archive:
                item=tarfile.TarInfo(name);item.size=1;archive.addfile(item,io.BytesIO(b'x'))
            with self.assertRaises(ReviewUnavailable):_extract(stream.getvalue(),self.root/'unpacked')

    def test_direct_engine_runtime_mutation_blocks_outcomes(self):
        engine=BubblewrapReviewEngine()
        def run(argv,**kwargs):
            (self.runtime/'changed').write_text('untrusted drift')
            return {'exit_code':0,'elapsed_seconds':0.01}
        with patch.object(engine,'probe',return_value={'available':True,'isolation_verified':True}),patch('manager_bridge.three_tier_runner._run_bounded',side_effect=run):
            with self.assertRaises(Conflict):engine.execute_snapshot(self.profile,self.source,self.root,[('checks',self.profile.checks['checks'])],lambda:None)

    def test_process_cap_is_applied_inside_namespace_before_registered_command(self):
        engine=BubblewrapReviewEngine();seen=[]
        def run(argv,**kwargs):
            seen.append(argv);return {'exit_code':0,'elapsed_seconds':0.01}
        with patch.object(engine,'probe',return_value={'available':True,'isolation_verified':True}),patch('manager_bridge.three_tier_runner._run_bounded',side_effect=run):
            engine.execute_snapshot(self.profile,self.source,self.root,[('checks',self.profile.checks['checks'])],lambda:None)
        argv=seen[0];position=argv.index('/usr/bin/prlimit')
        self.assertLess(argv.index('--unshare-all'),position)
        self.assertIn('--nproc=128',argv[position:])
        self.assertEqual(argv[-3:],list(self.profile.checks['checks']))
