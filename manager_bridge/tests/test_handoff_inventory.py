from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import importlib.util
import json
from manager_bridge.connection_readiness import SourcePin
from manager_bridge.continuous_host import _git
from manager_bridge.handoff_inventory import inspect_handoff
from manager_bridge.source_watch import SourceHead
from manager_bridge.store import Conflict


class HandoffInventoryTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root/'code.txt').write_text('baseline\n')
        _git(self.root,'init','-q');_git(self.root,'add','code.txt');_git(self.root,'commit','-qm','baseline')
        self.sha=_git(self.root,'rev-parse','HEAD').decode().strip()
        self.config={'company':'fixture','name':'Fixture','source_kind':'github','source_ref':'fixture/project','branch':'main','services':[],'maintenance_enabled':True}
        self.pin=SourcePin('project','github','fixture/project','main',self.sha,'v1')
        self.head=SourceHead('project','github','fixture/project','main',self.sha,'v1','fixture/head',1000)
    def inspect(self,head=True):
        return inspect_handoff(self.config,self.root,self.pin,self.head if head is True else head,clock=lambda:1000)
    def test_aligned_source_is_not_execution_or_qa_approval(self):
        value=self.inspect();self.assertEqual(value['state'],'source_aligned')
        self.assertTrue(value['source_ready_for_handoff'])
        self.assertFalse(value['execution_ready']);self.assertFalse(value['approval_granted'])
        self.assertEqual(value['qa_contract'],'not_verified');self.assertEqual(value['tests_executed'],0)
    def test_remote_code_and_saved_version_drift_are_separate(self):
        self.assertEqual(self.inspect(replace(self.head,sha='b'*40))['state'],'upstream_changed')
        self.assertEqual(self.inspect(replace(self.head,revision='v2'))['state'],'revision_changed')
    def test_missing_stale_and_future_remote_evidence_never_current(self):
        self.assertEqual(self.inspect(None)['state'],'upstream_unmeasured')
        for stamp in (1,1001):self.assertEqual(self.inspect(replace(self.head,observed_at=stamp))['state'],'upstream_stale')
    def test_dirty_legacy_source_preserved_even_when_upstream_changed(self):
        (self.root/'code.txt').write_text('unfinished legacy work\n')
        before=_git(self.root,'diff','--','code.txt')
        result=self.inspect(replace(self.head,sha='b'*40))
        self.assertEqual(result['state'],'uncommitted_changes_preserved')
        self.assertEqual(before,_git(self.root,'diff','--','code.txt'))
        self.assertEqual(_git(self.root,'rev-parse','HEAD').decode().strip(),self.sha)
    def test_wrong_project_or_source_is_rejected(self):
        for head in (replace(self.head,project='other'),replace(self.head,source_ref='other/project')):
            with self.assertRaises(Conflict):self.inspect(head)
        with self.assertRaises(Conflict):inspect_handoff({**self.config,'branch':'other'},self.root,self.pin,self.head)
    def test_clean_local_new_commit_requires_baseline_reconciliation(self):
        (self.root/'code.txt').write_text('new commit\n');_git(self.root,'add','code.txt');_git(self.root,'commit','-qm','later')
        self.assertEqual(self.inspect()['state'],'local_baseline_changed')
    def test_absent_source_does_not_claim_unchanged(self):
        value=inspect_handoff(self.config,self.root/'missing',self.pin,self.head,clock=lambda:1000)
        self.assertEqual(value['state'],'source_unavailable');self.assertIsNone(value['local_sha'])
    def test_cli_rejects_traversal_output_without_changing_source(self):
        from dataclasses import asdict
        script=Path(__file__).resolve().parents[2]/'scripts/inspect_project_handoffs.py'
        spec=importlib.util.spec_from_file_location('handoff_cli',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        outside=self.root.parent/(self.root.name+'-meta')
        outside.mkdir();self.addCleanup(lambda:__import__('shutil').rmtree(outside))
        for filename,data in (('catalog.json',{'project':self.config}),('baselines.json',{'profiles':[{'pin':asdict(self.pin)}]}),('heads.json',[asdict(self.head)])):
            (outside/filename).write_text(json.dumps(data))
        destination=outside/'..'/self.root.name/'code.txt'
        original=(self.root/'code.txt').read_bytes()
        args=['--catalog',str(outside/'catalog.json'),'--baselines',str(outside/'baselines.json'),'--heads',str(outside/'heads.json'),'--source-root',str(self.root),'--output',str(destination)]
        with patch('builtins.print'):self.assertEqual(module.main(args),2)
        self.assertEqual((self.root/'code.txt').read_bytes(),original)


if __name__=='__main__':unittest.main()
