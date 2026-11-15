import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from manager_bridge.merge_rehearsal import MergeRehearsal, RehearsalError


class MergeRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.git('init','-q','-b','main')
        self.git('config','user.name','Fixture')
        self.git('config','user.email','fixture@example.invalid')
        (self.repo/'shared.txt').write_text('base\n')
        self.git('add','.');self.git('commit','-qm','base')
        self.base = self.git('rev-parse','HEAD')
        self.runner = MergeRehearsal({'fixture':{'repository':'fixture/repo','path':self.repo}})

    def git(self,*args):
        return subprocess.check_output(['/usr/bin/git','-C',str(self.repo),*args],text=True,
            env={'PATH':'/usr/bin:/bin','HOME':str(self.root),'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull}).strip()

    def branch_commit(self,branch,path,text):
        self.git('checkout','-qb',branch,self.base)
        (self.repo/path).write_text(text)
        self.git('add',path);self.git('commit','-qm',branch)
        return self.git('rev-parse','HEAD')

    def snapshot(self):
        index=(self.repo/'.git/index').read_bytes()
        refs=self.git('show-ref')
        head=(self.repo/'.git/HEAD').read_bytes()
        files={str(path.relative_to(self.repo)):path.read_bytes()
               for path in self.repo.iterdir() if path.is_file()}
        objects=sorted(str(path.relative_to(self.repo/'.git/objects')) for path in (self.repo/'.git/objects').rglob('*') if path.is_file())
        return (index,refs,head,files,objects)

    def run_merge(self,left,right):
        return self.runner.run('fixture','fixture/repo',left,right)

    def test_divergent_clean_merge_uses_temporary_objects_without_touching_index_refs_or_files(self):
        left=self.branch_commit('left','left.txt','left\n')
        right=self.branch_commit('right','right.txt','right\n')
        before=self.snapshot()
        result=self.run_merge(left,right)
        self.assertEqual(result['status'],'clean_rehearsal')
        self.assertEqual(result['conflict_paths'],[])
        self.assertEqual(result['object_storage_scope'],'temporary_rehearsal_repository')
        self.assertFalse(result['source_object_database_modified'])
        self.assertFalse(result['production_merge_performed'])
        self.assertEqual(before,self.snapshot())

    def test_content_conflict_reports_paths_only_and_preserves_all_source_state(self):
        left=self.branch_commit('left','shared.txt','private left source\n')
        right=self.branch_commit('right','shared.txt','private right source\n')
        before=self.snapshot()
        result=self.run_merge(left,right)
        self.assertEqual(result['status'],'conflicts')
        self.assertEqual(result['conflict_paths'],['shared.txt'])
        self.assertNotIn('private left source',str(result));self.assertNotIn('private right source',str(result))
        self.assertEqual(before,self.snapshot())

    def test_ancestor_rehearsal_and_dirty_worktree_are_explicit_not_silently_integrated(self):
        right=self.branch_commit('right','right.txt','new\n')
        (self.repo/'shared.txt').write_text('uncommitted\n')
        before=self.snapshot()
        result=self.run_merge(self.base,right)
        self.assertEqual(result['status'],'clean_rehearsal')
        self.assertFalse(result['worktree_clean_before'])
        self.assertEqual(before,self.snapshot())

    def _mutate_during_virtual_merge(self, path, new_content):
        original = self.runner._git
        observed_status = self.git('status', '--porcelain=v1', '--untracked-files=all')
        changed = []
        def injecting(repo, home, *args, **options):
            if args and args[0] == 'merge-tree' and not changed:
                changed.append(True)
                path.write_bytes(new_content)
                self.assertEqual(self.git('status', '--porcelain=v1', '--untracked-files=all'), observed_status)
            return original(repo, home, *args, **options)
        with patch.object(self.runner, '_git', side_effect=injecting):
            with self.assertRaisesRegex(RehearsalError, '상태가 바뀌었어요'):
                self.run_merge(self.base, self.base)
        self.assertTrue(changed)

    def test_same_modified_status_cannot_hide_tracked_byte_changes(self):
        path = self.repo/'shared.txt'
        path.write_bytes(b'first dirty content\n')
        self._mutate_during_virtual_merge(path, b'other dirty content\n')

    def test_same_untracked_status_cannot_hide_byte_changes(self):
        path = self.repo/'new.txt'
        path.write_bytes(b'first untracked bytes\n')
        self._mutate_during_virtual_merge(path, b'other untracked bytes\n')

    def test_ignored_existing_path_and_symlink_target_changes_are_detected(self):
        (self.repo/'.git/info/exclude').write_text('ignored.txt\n')
        path = self.repo/'ignored.txt'
        path.write_bytes(b'first ignored bytes\n')
        self._mutate_during_virtual_merge(path, b'other ignored bytes\n')
        path.unlink()
        link = self.repo/'linked.txt'
        link.symlink_to('/first-target-do-not-follow')
        original = self.runner._git
        def injecting(repo, home, *args, **options):
            if args and args[0] == 'merge-tree':
                link.unlink();link.symlink_to('/other-target-do-not-follow')
            return original(repo, home, *args, **options)
        with patch.object(self.runner, '_git', side_effect=injecting):
            with self.assertRaisesRegex(RehearsalError, '상태가 바뀌었어요'):
                self.run_merge(self.base, self.base)

    def test_assume_unchanged_cannot_hide_raw_tracked_byte_changes(self):
        self.git('update-index','--assume-unchanged','shared.txt')
        path = self.repo/'shared.txt'
        # Git status/diff deliberately omit assume-unchanged worktree changes.
        # Independent raw tracked byte inventory must still detect them.
        path.write_bytes(b'first hidden edit\n')
        self._mutate_during_virtual_merge(path, b'other hidden edit\n')

    def test_file_budget_and_external_hardlink_are_refused_without_raw_output(self):
        path = self.repo/'new.txt'
        path.write_bytes(b'private file data')
        with patch('manager_bridge.merge_rehearsal.MAX_WORKTREE_BYTES', 8):
            with self.assertRaises(RehearsalError) as failure:
                self.run_merge(self.base,self.base)
        self.assertNotIn('private file data',str(failure.exception))
        path.unlink()
        external=self.root/'outside-secret'
        external.write_bytes(b'not to be read through a hardlink')
        os.link(external,path)
        with self.assertRaises(RehearsalError):self.run_merge(self.base,self.base)

    def test_executable_local_config_and_includes_are_refused_without_execution(self):
        marker=self.root/'executed'
        self.git('config','merge.untrusted.driver','touch '+str(marker))
        with self.assertRaises(RehearsalError):self.run_merge(self.base,self.base)
        self.assertFalse(marker.exists())
        self.git('config','--unset','merge.untrusted.driver')
        included=self.root/'included.cfg';included.write_text('[merge "evil"]\n driver = touch '+str(marker)+'\n')
        self.git('config','include.path',str(included))
        with self.assertRaises(RehearsalError):self.run_merge(self.base,self.base)
        self.assertFalse(marker.exists())

    def test_promisor_partial_clone_and_external_helper_cannot_execute_lazy_fetch(self):
        marker=self.root/'unexpected-helper-execution'
        self.git('config','extensions.partialClone','origin')
        self.git('config','remote.origin.promisor','true')
        self.git('config','remote.origin.partialclonefilter','blob:none')
        self.git('config','protocol.ext.allow','always')
        self.git('config','remote.origin.url','ext::/usr/bin/touch '+str(marker))
        with self.assertRaises(RehearsalError):self.run_merge(self.base,'f'*40)
        self.assertFalse(marker.exists())
        for key in ('extensions.partialClone','remote.origin.promisor','remote.origin.partialclonefilter','protocol.ext.allow'):
            self.git('config','--unset',key)
        with self.assertRaises(RehearsalError):self.run_merge(self.base,self.base)
        self.assertFalse(marker.exists())

    def test_transport_is_disabled_even_if_promisor_config_changes_after_preflight(self):
        marker=self.root/'unexpected-late-helper'
        self.git('config','remote.origin.url','https://example.invalid/fixture.git')
        original=self.runner._git
        injected=[]
        def injecting(repo,home,*args,**options):
            if args[:2]==('cat-file','-t') and not injected:
                injected.append(True)
                self.git('config','extensions.partialClone','origin')
                self.git('config','remote.origin.promisor','true')
                self.git('config','remote.origin.partialclonefilter','blob:none')
                self.git('config','protocol.ext.allow','always')
                self.git('config','remote.origin.url','ext::/usr/bin/touch '+str(marker))
            return original(repo,home,*args,**options)
        with patch.object(self.runner,'_git',side_effect=injecting):
            with self.assertRaises(RehearsalError):self.run_merge(self.base,'f'*40)
        self.assertTrue(injected)
        self.assertFalse(marker.exists())

    def test_normal_origin_is_allowed_without_fetch_or_source_mutation(self):
        self.git('remote','add','origin','https://example.invalid/fixture.git')
        before=self.snapshot()
        result=self.run_merge(self.base,self.base)
        self.assertEqual(result['status'],'clean_rehearsal')
        self.assertFalse(result['network_fetch_allowed'])
        self.assertEqual(before,self.snapshot())

    def test_ambient_git_settings_are_not_inherited(self):
        right=self.branch_commit('right','right.txt','new\n')
        with patch.dict(os.environ,{'GIT_DIR':'/nonexistent','GIT_CONFIG_GLOBAL':'/nonexistent',
                                   'OPENAI_API_KEY':'fixture-private-key'}):
            result=self.run_merge(self.base,right)
        self.assertEqual(result['status'],'clean_rehearsal')
        self.assertNotIn('fixture-private-key',str(result))

    def test_linked_detached_worktree_without_extension_is_supported_without_config_changes(self):
        linked=self.root/'linked'
        self.git('worktree','add','--detach','-q',str(linked),self.base)
        common_config=(self.repo/'.git/config').read_bytes()
        linked_runner=MergeRehearsal({'linked':{'repository':'fixture/repo','path':linked}})
        index_path=subprocess.check_output(['/usr/bin/git','-C',str(linked),'rev-parse',
            '--path-format=absolute','--git-path','index'],text=True).strip()
        before_index=Path(index_path).read_bytes()
        result=linked_runner.run('linked','fixture/repo',self.base,self.base)
        self.assertEqual(result['status'],'clean_rehearsal')
        self.assertEqual(common_config,(self.repo/'.git/config').read_bytes())
        self.assertEqual(before_index,Path(index_path).read_bytes())
        self.assertEqual((linked/'shared.txt').read_bytes(),b'base\n')
        worktree_config=Path(subprocess.check_output(['/usr/bin/git','-C',str(linked),'rev-parse',
            '--path-format=absolute','--git-path','config.worktree'],text=True).strip())
        self.assertFalse(worktree_config.exists())

    def test_existing_physical_worktree_config_is_checked_even_when_extension_is_disabled(self):
        linked=self.root/'linked'
        self.git('worktree','add','--detach','-q',str(linked),self.base)
        worktree_config=Path(subprocess.check_output(['/usr/bin/git','-C',str(linked),'rev-parse',
            '--path-format=absolute','--git-path','config.worktree'],text=True).strip())
        marker=self.root/'unsafe-driver'
        worktree_config.write_text('[merge "unsafe"]\n driver = touch '+str(marker)+'\n')
        runner=MergeRehearsal({'linked':{'repository':'fixture/repo','path':linked}})
        with self.assertRaises(RehearsalError):runner.run('linked','fixture/repo',self.base,self.base)
        self.assertFalse(marker.exists())

    def test_symlink_and_nonregular_physical_worktree_configs_are_rejected(self):
        linked=self.root/'linked'
        self.git('worktree','add','--detach','-q',str(linked),self.base)
        worktree_config=Path(subprocess.check_output(['/usr/bin/git','-C',str(linked),'rev-parse',
            '--path-format=absolute','--git-path','config.worktree'],text=True).strip())
        external=self.root/'external-config';external.write_text('[core]\n bare = false\n')
        runner=MergeRehearsal({'linked':{'repository':'fixture/repo','path':linked}})
        worktree_config.symlink_to(external)
        with self.assertRaises(RehearsalError):runner.run('linked','fixture/repo',self.base,self.base)
        worktree_config.unlink();os.mkfifo(worktree_config)
        with self.assertRaises(RehearsalError):runner.run('linked','fixture/repo',self.base,self.base)

    def test_full_commit_ids_registration_and_commit_object_type_are_required(self):
        for left,right in (('main',self.base),(self.base[:7],self.base),('f'*40,self.base)):
            with self.assertRaises(RehearsalError):self.run_merge(left,right)
        with self.assertRaises(RehearsalError):self.runner.run('fixture','other/repo',self.base,self.base)
        tree=self.git('rev-parse','HEAD^{tree}')
        with self.assertRaises(RehearsalError):self.run_merge(tree,self.base)


if __name__=='__main__':
    unittest.main()
