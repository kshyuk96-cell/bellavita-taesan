"""Cooperative profile locks around stub protocol subprocesses, no model calls."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import tempfile
import unittest
from manager_bridge.fleet_codex import CodexHost, ProjectBinding, TransportUnavailable


class SubscriptionTransportGuardTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        profile=self.root/'profile';profile.mkdir(mode=0o700)
        binary=self.root/'stub-codex'
        binary.write_text('''#!/usr/bin/python3
import sys,json
if 'app-server' in sys.argv:
 for line in sys.stdin:
  item=json.loads(line)
  if 'id' in item:
   result={'account':{'type':'chatgpt'}} if item['method']=='account/read' else {}
   print(json.dumps({'id':item['id'],'result':result}),flush=True)
else:
 sys.stdin.read()
 for item in [{'type':'thread.started','thread_id':'00000000-0000-4000-8000-000000000001'},{'type':'turn.started'},{'type':'turn.completed'}]:
  print(json.dumps(item),flush=True)
''')
        binary.chmod(0o700);self.active=False;self.events=[];self.spawns=[]
        @contextmanager
        def guard(project):
            self.assertEqual(project,'fixture');self.assertFalse(self.active)
            self.active=True;self.events.append('enter')
            try:yield
            finally:self.events.append('exit');self.active=False
        self.guard=guard
        def spawn(*args,**kwargs):
            self.assertTrue(self.active);self.spawns.append(args[0])
            return subprocess.Popen(*args,**kwargs)
        self.host=CodexHost(binary,profile,{'fixture':ProjectBinding('fixture','fixture/repo',self.root,require_origin=False)},
                            profile_guard=guard,process_factory=spawn,diagnostic_sink=lambda _:True)
        self.host._source=lambda _: {'sha':'a'*40,'tracked_and_visible_untracked_clean':True}
        self.host._confidentiality['fixture']=((),{'verified':True})

    def test_subscription_metadata_process_is_guarded_until_closed(self):
        self.assertTrue(self.host.ready_for_new('fixture'))
        self.assertEqual(self.events,['enter','exit']);self.assertFalse(self.active)
        self.assertEqual(len(self.spawns),1)

    def test_stub_native_process_remains_guarded_for_input_and_receipt(self):
        def heartbeat():self.assertTrue(self.active)
        events,exit_code=self.host._run('fixture','fixture prompt',session_id=None,read_only=True,heartbeat=heartbeat)
        self.assertEqual(exit_code,0);self.assertEqual(events[-1]['type'],'turn.completed')
        self.assertEqual(self.events,['enter','exit']);self.assertFalse(self.active)

    def test_spawn_failure_releases_profile_and_never_claims_ready(self):
        def failed(*args,**kwargs):
            self.assertTrue(self.active);raise OSError('fixture unavailable')
        self.host.process_factory=failed
        with self.assertRaises(TransportUnavailable):self.host.ready_for_new('fixture')
        self.assertEqual(self.events,['enter','exit']);self.assertFalse(self.active)

    def test_guard_denial_prevents_every_process_spawn(self):
        @contextmanager
        def denied(project):raise PermissionError('fixture denied');yield
        self.host.profile_guard=denied
        with self.assertRaises(TransportUnavailable):self.host.ready_for_new('fixture')
        with self.assertRaises(PermissionError):self.host._run('fixture','fixture',session_id=None,read_only=True,heartbeat=lambda:True)
        self.assertEqual(self.spawns,[])


if __name__=='__main__':unittest.main()
