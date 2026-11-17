"""Source archive checks only; no project execution or model calls."""
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from manager_bridge.source_snapshot_stage import SourceSnapshotStage
from manager_bridge.connection_readiness import SourcePin
from manager_bridge.store import Store,Conflict,PermissionDenied,NotFound,token_hash
from manager_bridge.maintenance_registry import Registry
from manager_bridge.three_tier import ThreeTier


class SnapshotStageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'snapshots';self.root.mkdir(mode=0o700)
        self.now=1000;self.store=Store(self.base/'manager.sqlite3',clock=lambda:self.now)
        self.registry=Registry(self.store);self.core=ThreeTier(self.store,self.registry)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry.register(self.owner,'project',{'company':'fixture','name':'Fixture','source_kind':'github','source_ref':'fixture/project','branch':'main','services':[],'maintenance_enabled':False})
        self.manager=self.store.add_token('manager','manager',['project'])
        self.store.register_project(self.owner,'other','fixture/other')
        self.other=self.store.add_token('other','manager',['other'])
        self.pin=SourcePin('project','github','fixture/project','main','a'*40,'v47');self.current=self.pin
        self.service=SourceSnapshotStage(self.core,self.root,current_pin=lambda _:self.current)
    def archive(self,entries,compression='w:gz'):
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode=compression) as tar:
            for name,kind,body in entries:
                member=tarfile.TarInfo(name)
                if kind=='file':member.size=len(body);tar.addfile(member,io.BytesIO(body))
                elif kind=='dir':member.type=tarfile.DIRTYPE;tar.addfile(member)
                else:
                    member.type={'symlink':tarfile.SYMTYPE,'hardlink':tarfile.LNKTYPE,'device':tarfile.CHRTYPE}[kind];member.linkname=body.decode();tar.addfile(member)
        return output.getvalue()
    def stage(self,data,**kwargs):
        return self.service.stage(self.manager,self.pin,hashlib.sha256(data).hexdigest(),self.now,data,**kwargs)
    def test_exact_private_snapshot_and_no_overwrite(self):
        data=self.archive([('top','dir',b''),('top/src/main.py','file',b'print(1)\n')])
        one=self.stage(data,strip_prefix='top');two=self.stage(data,strip_prefix='top')
        self.assertNotEqual(one['path'],two['path']);self.assertEqual(one['tree_sha256'],two['tree_sha256'])
        self.assertEqual((Path(one['path'])/'src/main.py').read_bytes(),b'print(1)\n')
        self.assertEqual(os.stat(Path(one['path'])/'src/main.py').st_mode&0o777,0o600)
        self.assertEqual(os.stat(Path(one['path'])/'src').st_mode&0o777,0o700)
        for flag in ('git_commit_independently_verified','credential_content_audited','tests_executed','worker_connected','activated'):self.assertFalse(one[flag])
    def test_unsafe_paths_links_devices_duplicates_and_credentials(self):
        for path in ('../escape','/absolute','a/../escape','a\\b','C:/x','a//b','.git/config','.env','.env.production','.codex/auth.json','.aws/credentials','auth.json','key.pem'):
            with self.subTest(path=path),self.assertRaises(Conflict):self.stage(self.archive([(path,'file',b'x')]))
        for kind in ('symlink','hardlink','device'):
            with self.subTest(kind=kind),self.assertRaises(Conflict):self.stage(self.archive([('a',kind,b'outside')]))
        with self.assertRaises(Conflict):self.stage(self.archive([('a','file',b'x'),('a','file',b'x')]))
        self.assertEqual(list(self.root.iterdir()),[])
    def test_hash_scope_staleness_and_pin_change(self):
        data=self.archive([('a','file',b'x')])
        with self.assertRaises(Conflict):self.service.stage(self.manager,self.pin,'0'*64,self.now,data)
        with self.assertRaises((PermissionDenied,NotFound)):self.service.stage(self.other,self.pin,hashlib.sha256(data).hexdigest(),self.now,data)
        with self.assertRaises(Conflict):self.service.stage(self.manager,self.pin,hashlib.sha256(data).hexdigest(),self.now-301,data)
        calls=[]
        def change(_):
            calls.append(1)
            return self.pin if len(calls)==1 else SourcePin('project','github','fixture/project','main','b'*40,'v48')
        self.service.current_pin=change
        with self.assertRaises(Conflict):self.stage(data)
        self.assertEqual(list(self.root.iterdir()),[])
    def test_config_changed_callback_and_wrong_prefix_cleanup(self):
        data=self.archive([('top/a','file',b'x')])
        with self.assertRaises(Conflict):self.stage(data,strip_prefix='wrong')
        count=[]
        def change(_):
            count.append(1)
            if len(count)==1:
                config=self.registry.get(self.owner,'project')['config'];config['name']='Changed'
                self.registry.register(self.owner,'project',config)
            return self.pin
        self.service.current_pin=change
        with self.assertRaises(Conflict):self.stage(data)
        self.assertEqual(list(self.root.iterdir()),[])
    def test_decompression_and_member_bounds(self):
        data=self.archive([('a','file',b'x'*10000)])
        self.service.max_expanded_bytes=4096
        with self.assertRaises(Conflict):self.stage(data)
        self.service.max_expanded_bytes=128*1024;self.service.max_files=1
        with self.assertRaises(Conflict):self.stage(self.archive([('a','file',b'a'),('b','file',b'b')]))
        self.assertEqual(list(self.root.iterdir()),[])
    def test_deep_and_implicit_directory_nodes_are_bounded(self):
        with self.assertRaises(Conflict):self.stage(self.archive([('/'.join(['a']*33),'file',b'a')]))
        self.service.max_files=5
        # Only two headers, but six actual nodes including implicit parents.
        with self.assertRaises(Conflict):self.stage(self.archive([('a/b/c','file',b'a'),('d/e/f','file',b'b')]))
        self.assertEqual(list(self.root.iterdir()),[])
        result=self.stage(self.archive([('a/b','file',b'a'),('a/c','file',b'b')]))
        self.assertEqual(result['filesystem_nodes'],3)
    def test_final_callback_revocation_removes_published_snapshot(self):
        calls=[]
        def revoke(_):
            calls.append(1)
            if len(calls)==3:self.store.revoke_token(self.owner,token_hash(self.manager))
            return self.pin
        self.service.current_pin=revoke
        with self.assertRaises(PermissionDenied):self.stage(self.archive([('a','file',b'a')]))
        self.assertEqual(len(calls),3);self.assertEqual(list(self.root.iterdir()),[])
    def test_final_callback_configuration_change_removes_snapshot(self):
        calls=[]
        def change(_):
            calls.append(1)
            if len(calls)==3:
                config=self.registry.get(self.owner,'project')['config'];config['name']='Changed after publication'
                self.registry.register(self.owner,'project',config)
            return self.pin
        self.service.current_pin=change
        with self.assertRaises(Conflict):self.stage(self.archive([('a','file',b'a')]))
        self.assertEqual(len(calls),3);self.assertEqual(list(self.root.iterdir()),[])
    def test_pax_traversal_and_sparse_metadata_refused(self):
        for headers in ({'path':'../escape'},{'GNU.sparse.map':'0,1','GNU.sparse.size':'1'}):
            output=io.BytesIO()
            with tarfile.open(fileobj=output,mode='w:gz',format=tarfile.PAX_FORMAT) as tar:
                member=tarfile.TarInfo('safe');member.size=1;member.pax_headers=headers
                tar.addfile(member,io.BytesIO(b'a'))
            with self.assertRaises(Conflict):self.stage(output.getvalue())
        self.assertEqual(list(self.root.iterdir()),[])
    def test_final_callback_pin_change_removes_snapshot(self):
        calls=[]
        def change(_):
            calls.append(1)
            return self.pin if len(calls)<3 else SourcePin('project','github','fixture/project','main','b'*40,'v48')
        self.service.current_pin=change
        with self.assertRaises(Conflict):self.stage(self.archive([('a','file',b'a')]))
        self.assertEqual(len(calls),3);self.assertEqual(list(self.root.iterdir()),[])
    def test_credential_top_folder_cannot_be_stripped(self):
        with self.assertRaises(Conflict):self.stage(self.archive([('.aws/config','file',b'private')]),strip_prefix='.aws')
        self.assertEqual(list(self.root.iterdir()),[])
    def test_root_private_and_identity(self):
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):SourceSnapshotStage(self.core,self.root,current_pin=lambda _:self.pin)
        self.root.chmod(0o700);self.root.rename(self.base/'old');self.root.mkdir(mode=0o700)
        with self.assertRaises(Conflict):self.stage(self.archive([('a','file',b'a')]))
    def test_plain_tar_supported_empty_refused(self):
        result=self.stage(self.archive([('a','file',b'a')],compression='w'))
        self.assertEqual(result['files'],1)
        with self.assertRaises(Conflict):self.stage(self.archive([]))
