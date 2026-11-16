import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from manager_bridge.source_handoff import SourceSnapshot,prepare_source_snapshot
from manager_bridge.store import Conflict


class SourceHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory());self.root=Path(self.temp)
        self.addCleanup(self.writable_cleanup)

    def writable_cleanup(self):
        for path in [self.root,*self.root.rglob('*')]:
            if path.is_dir() and not path.is_symlink():os.chmod(path,0o700)

    def tar(self,entries,gz=False):
        body=io.BytesIO()
        with tarfile.open(fileobj=body,mode='w') as bundle:
            for path,content,kind in entries:
                info=tarfile.TarInfo(path);info.type=kind
                if kind==tarfile.REGTYPE:info.size=len(content);bundle.addfile(info,io.BytesIO(content))
                else:
                    if kind in (tarfile.LNKTYPE,tarfile.SYMTYPE):info.linkname='outside'
                    bundle.addfile(info)
        return gzip.compress(body.getvalue()) if gz else body.getvalue()

    def prepare(self,body,source_kind='sites',**options):
        archive=self.root/('archive-'+hashlib.sha256(body).hexdigest()[:12]);archive.write_bytes(body)
        snap=SourceSnapshot('hope-sunrise',source_kind,'appgprj_fixture' if source_kind=='sites' else 'fixture/repo','main','a'*40,'v39',hashlib.sha256(body).hexdigest())
        return prepare_source_snapshot(snap,archive,self.root/'handoffs',**options)

    def test_tar_gzip_and_zip_preserve_bytes_without_git_hooks_or_customer_execution(self):
        entries=[('README.md',b'preserve original',tarfile.REGTYPE),('.gitattributes',b'*.json export-subst\n',tarfile.REGTYPE),('test.json',b'{"literal":"$Format:%H$"}',tarfile.REGTYPE)]
        for encoding in ('tar','gzip','zip'):
            if encoding=='zip':
                stream=io.BytesIO()
                with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as bundle:
                    for name,content,_ in entries:bundle.writestr(name,content)
                body=stream.getvalue()
            else:body=self.tar(entries,gz=encoding=='gzip')
            with self.subTest(encoding=encoding),patch('subprocess.run',side_effect=AssertionError('must not execute anything')):
                manifest=self.prepare(body)
            source=Path(manifest['source_root'])
            self.assertEqual((source/'test.json').read_bytes(),entries[-1][1])
            self.assertEqual(manifest['tree']['file_count'],3);self.assertFalse(manifest['actual_git_clone'])
            self.assertFalse(manifest['git_origin_verified']);self.assertFalse(manifest['worker_ready'])
            self.assertFalse((source/'.git').exists())
            self.assertEqual(stat.S_IMODE(source.parent.stat().st_mode),0o700)
            self.assertEqual(manifest,self.prepare(body))

    def test_tar_traversal_absolute_links_specials_git_and_dependency_metadata_rejected(self):
        cases=[('../escape',tarfile.REGTYPE),('/absolute',tarfile.REGTYPE),('a/../../escape',tarfile.REGTYPE),('C:\\escape',tarfile.REGTYPE),('.git/hooks/pre-commit',tarfile.REGTYPE),('nested/node_modules/code.js',tarfile.REGTYPE),('link',tarfile.SYMTYPE),('hard',tarfile.LNKTYPE),('device',tarfile.CHRTYPE),('secret/.env.local',tarfile.REGTYPE)]
        for name,kind in cases:
            with self.subTest(name=name):
                with self.assertRaises(Conflict):self.prepare(self.tar([(name,b'bad',kind)]))
        self.assertFalse((self.root/'escape').exists())
        self.assertFalse(any((self.root/'handoffs').glob('*/*/source-manifest.json')))

    def test_duplicate_case_unicode_parent_and_file_directory_collisions_rejected(self):
        cases=[['same','same'],['README','readme'],['café','cafe\u0301'],['A/file','a/other'],['file','file/nested']]
        for names in cases:
            with self.subTest(names=names):
                with self.assertRaises(Conflict):self.prepare(self.tar([(n,b'x',tarfile.REGTYPE) for n in names]))

    def test_zip_symlink_duplicates_and_file_count_bounds_rejected(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as bundle:
            info=zipfile.ZipInfo('link');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
            bundle.writestr(info,'outside')
        with self.assertRaises(Conflict):self.prepare(stream.getvalue())
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as bundle:
            bundle.writestr('one','x');bundle.writestr('two','x')
        with self.assertRaises(Conflict):self.prepare(stream.getvalue(),max_files=1)

    def test_size_limits_digest_mismatch_and_unsafe_root_leave_no_published_snapshot(self):
        body=self.tar([('big',b'x'*20000,tarfile.REGTYPE)],gz=True)
        with self.assertRaises(Conflict):self.prepare(body,max_bytes=1024)
        archive=self.root/'bad.tar';archive.write_bytes(body)
        snapshot=SourceSnapshot('hope-sunrise','sites','appgprj_fixture','main','a'*40,'v39','b'*64)
        with self.assertRaises(Conflict):prepare_source_snapshot(snapshot,archive,self.root/'bad-handoff')
        public=self.root/'public';public.mkdir(mode=0o755)
        with self.assertRaises(ValueError):prepare_source_snapshot(snapshot,archive,public)
        with self.assertRaises(ValueError):self.prepare(body,max_files=True)

    def test_immutable_revision_cache_detects_modified_bytes_and_new_revision_is_separate(self):
        body=self.tar([('file.txt',b'original',tarfile.REGTYPE)])
        first=self.prepare(body);source=Path(first['source_root']);os.chmod(source/'file.txt',0o600);(source/'file.txt').write_bytes(b'changed')
        with self.assertRaises(Conflict):self.prepare(body)
        archive=self.root/'new.tar';archive.write_bytes(body)
        new=SourceSnapshot('hope-sunrise','sites','appgprj_fixture','main','a'*40,'v40',hashlib.sha256(body).hexdigest())
        second=prepare_source_snapshot(new,archive,self.root/'handoffs')
        self.assertNotEqual(second['source_root'],first['source_root'])
        self.assertEqual((Path(second['source_root'])/'file.txt').read_bytes(),b'original')

    def test_real_github_identity_is_not_invented_for_sites_and_raw_mapping_not_accepted(self):
        body=self.tar([('file',b'x',tarfile.REGTYPE)])
        result=self.prepare(body)
        self.assertEqual(result['snapshot']['source_kind'],'sites');self.assertEqual(result['snapshot']['source_ref'],'appgprj_fixture')
        self.assertFalse(result['provider_revision_independently_verified'])
        with self.assertRaises(ValueError):prepare_source_snapshot(result['snapshot'],self.root/'missing',self.root/'other')

    def test_cache_cannot_promote_archive_to_git_or_worker_authority(self):
        body=self.tar([('file',b'original',tarfile.REGTYPE)])
        first=self.prepare(body);manifest=Path(first['manifest_path'])
        os.chmod(manifest,0o600);forged=json.loads(manifest.read_text());forged['actual_git_clone']=True;forged['worker_ready']=True
        manifest.write_text(json.dumps(forged))
        with self.assertRaises(Conflict):self.prepare(body)

    def test_implicit_directories_are_bounded_before_extraction(self):
        body=self.tar([('one/two/three/file',b'x',tarfile.REGTYPE)])
        with self.assertRaisesRegex(Conflict,'implicit source directory'):self.prepare(body,max_files=2)

    def test_cached_source_root_link_is_rejected_without_reading_outside_files(self):
        body=self.tar([('file',b'original',tarfile.REGTYPE)])
        first=self.prepare(body);source=Path(first['source_root'])
        os.chmod(source,0o700);source.rename(self.root/'saved-source')
        outside=self.root/'outside';outside.mkdir();(outside/'private').write_bytes(b'not a source file')
        source.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(Conflict,'root changed'):self.prepare(body)
