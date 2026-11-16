import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from manager_bridge.customer_runtime_prep import prepare_locked_node, inspect_dependency_tree, validate_locked_registry_inputs, prepare_locked_registry_package, NODE, NPM
from manager_bridge.store import Conflict


class RuntimePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory());self.root=Path(self.temp)
        self.source=self.root/'original';self.source.mkdir()
        package={'name':'runtime-fixture','version':'1.0.0','scripts':{'preinstall':'node -e "require(\'fs\').writeFileSync(\'LIFECYCLE-RAN\',\'bad\')"'}}
        (self.source/'package.json').write_text(json.dumps(package))
        lock={'name':'runtime-fixture','version':'1.0.0','lockfileVersion':3,'requires':True,'packages':{'':{'name':'runtime-fixture','version':'1.0.0','hasInstallScript':True}}}
        (self.source/'package-lock.json').write_text(json.dumps(lock))
        def git(*args):return subprocess.check_output(['/usr/bin/git','-C',str(self.source),*args],stderr=subprocess.DEVNULL).decode().strip()
        git('init');git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@invalid','commit','-m','fixture')
        self.sha=git('rev-parse','HEAD')

    @unittest.skipUnless(Path(NODE).is_file() and Path(NPM).is_file(),'trusted Node/npm not installed')
    def test_actual_empty_locked_install_does_not_execute_root_lifecycle_or_modify_original(self):
        before={p.name:p.read_bytes() for p in self.source.glob('package*.json')}
        result=prepare_locked_node(self.source,self.sha,self.root/'prepared',timeout=60)
        self.assertFalse((self.root/'prepared'/'source'/'LIFECYCLE-RAN').exists())
        self.assertFalse((self.source/'node_modules').exists())
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.source.glob('package*.json')})
        self.assertFalse(result['lifecycle_scripts_executed']);self.assertFalse(result['customer_tests_executed'])
        self.assertIn('--ignore-scripts',result['npm_command']);self.assertIn('--omit=dev',result['npm_command'])
        self.assertEqual(result['source_sha'],self.sha);self.assertTrue(result['node']['version'].startswith('v'))
        self.assertEqual(result['dependencies']['sbom_root'],inspect_dependency_tree(result['dependency_root'])['sbom_root'])

    def test_dependency_manifest_hashes_actual_files_and_rejects_symlinks(self):
        deps=self.root/'deps';deps.mkdir();(deps/'package.json').write_text('{"name":"fixture","version":"1.2.3"}')
        first=inspect_dependency_tree(deps);self.assertEqual(first['packages'][0]['version'],'1.2.3')
        (deps/'file.js').write_text('first');second=inspect_dependency_tree(deps)
        self.assertNotEqual(first['tree_digest'],second['tree_digest'])
        (deps/'file.js').write_text('other');self.assertNotEqual(second['tree_digest'],inspect_dependency_tree(deps)['tree_digest'])
        (deps/'link').symlink_to(deps/'file.js')
        with self.assertRaises(ValueError):inspect_dependency_tree(deps)

    def test_existing_target_dirty_source_wrong_sha_and_nested_target_rejected(self):
        target=self.root/'existing';target.mkdir()
        with self.assertRaises(ValueError):prepare_locked_node(self.source,self.sha,target)
        with self.assertRaises(ValueError):prepare_locked_node(self.source,self.sha,self.source/'nested')
        with self.assertRaises(Conflict):prepare_locked_node(self.source,'a'*40,self.root/'wrong')
        (self.source/'untracked').write_text('pending original')
        with self.assertRaises(Conflict):prepare_locked_node(self.source,self.sha,self.root/'dirty')

    def test_committed_project_npmrc_is_rejected_before_install(self):
        (self.source/'.npmrc').write_text('registry=https://untrusted.invalid\n')
        subprocess.run(['/usr/bin/git','-C',str(self.source),'add','.npmrc'],check=True,stdout=subprocess.DEVNULL)
        subprocess.run(['/usr/bin/git','-C',str(self.source),'-c','user.name=Fixture','-c','user.email=fixture@invalid','commit','-m','config'],check=True,stdout=subprocess.DEVNULL)
        sha=subprocess.check_output(['/usr/bin/git','-C',str(self.source),'rev-parse','HEAD']).decode().strip()
        with self.assertRaisesRegex(Conflict,'override'):prepare_locked_node(self.source,sha,self.root/'config')

    def test_entire_lock_rejects_nonregistry_targets_and_missing_integrity_before_npm(self):
        base=json.loads((self.source/'package-lock.json').read_text())
        good={'version':'1.2.3','resolved':'https://registry.npmjs.org/fixture/-/fixture-1.2.3.tgz','integrity':'sha512-'+base64.b64encode(bytes(64)).decode(),'dev':True}
        bad_targets=('file:/tmp/private','git+ssh://git@github.com/private/repo.git','https://other.invalid/package.tgz','https://user:password@registry.npmjs.org/x.tgz','https://registry.npmjs.org/x.tgz?auth=unsafe','https://registry.npmjs.org/x.tgz#fragment')
        cases=[{**good,'resolved':target} for target in bad_targets]+[{k:v for k,v in good.items() if k!='integrity'},{**good,'link':True},{**good,'version':'file:/tmp/private'}]
        actual_run=subprocess.run
        for i,entry in enumerate(cases):
            lock=copy.deepcopy(base);lock['packages']['node_modules/fixture']=entry
            (self.source/'package-lock.json').write_text(json.dumps(lock))
            actual_run(['/usr/bin/git','-C',str(self.source),'add','package-lock.json'],check=True,stdout=subprocess.DEVNULL)
            actual_run(['/usr/bin/git','-C',str(self.source),'-c','user.name=Fixture','-c','user.email=fixture@invalid','commit','--allow-empty','-m','lock'],check=True,stdout=subprocess.DEVNULL)
            sha=subprocess.check_output(['/usr/bin/git','-C',str(self.source),'rev-parse','HEAD']).decode().strip()
            calls=[]
            def observe_run(argv,*args,**kwargs):
                calls.append(argv)
                if str(argv[0])==NPM:raise AssertionError('installer invoked for hostile lock')
                return actual_run(argv,*args,**kwargs)
            with self.subTest(case=i),patch('manager_bridge.customer_runtime_prep.subprocess.run',side_effect=observe_run):
                with self.assertRaises(Conflict):prepare_locked_node(self.source,sha,self.root/('hostile-'+str(i)))
                self.assertFalse(any(str(argv[0])==NPM for argv in calls))

    def test_omitted_root_dev_dependencies_are_validated_and_duplicate_lock_keys_rejected(self):
        package=json.loads((self.source/'package.json').read_text())
        package['devDependencies']={'hidden':'file:/tmp/private'}
        (self.source/'package.json').write_text(json.dumps(package))
        with self.assertRaises(Conflict):validate_locked_registry_inputs(self.source/'package.json',self.source/'package-lock.json')
        package.pop('devDependencies');(self.source/'package.json').write_text(json.dumps(package))
        (self.source/'package-lock.json').write_text('{"lockfileVersion":3,"lockfileVersion":3,"packages":{"":{}}}')
        with self.assertRaises(Conflict):validate_locked_registry_inputs(self.source/'package.json',self.source/'package-lock.json')

    def test_source_locked_package_extracts_only_after_exact_integrity_and_never_executes(self):
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w:gz') as bundle:
            for name,content in [('package/package.json',b'{"name":"fixture","version":"1.2.3"}'),('package/lib/compiler.js',b'throw new Error("must not execute");')]:
                member=tarfile.TarInfo(name);member.size=len(content);bundle.addfile(member,io.BytesIO(content))
        body=archive.getvalue();url='https://registry.npmjs.org/fixture/-/fixture-1.2.3.tgz'
        lock=json.loads((self.source/'package-lock.json').read_text())
        lock['packages']['node_modules/fixture']={'version':'1.2.3','resolved':url,'integrity':'sha512-'+base64.b64encode(hashlib.sha512(body).digest()).decode()}
        (self.source/'package-lock.json').write_text(json.dumps(lock))
        subprocess.run(['/usr/bin/git','-C',str(self.source),'add','.'],check=True,stdout=subprocess.DEVNULL)
        subprocess.run(['/usr/bin/git','-C',str(self.source),'-c','user.name=Fixture','-c','user.email=fixture@invalid','commit','-m','package'],check=True,stdout=subprocess.DEVNULL)
        sha=subprocess.check_output(['/usr/bin/git','-C',str(self.source),'rev-parse','HEAD']).decode().strip()
        class Response(io.BytesIO):
            def geturl(self):return url
        with patch('manager_bridge.customer_runtime_prep.build_opener') as factory:
            factory.return_value.open.return_value=Response(body)
            result=prepare_locked_registry_package(self.source,sha,self.root/'one-package','fixture','1.2.3')
            self.assertTrue(result['integrity_verified']);self.assertFalse(result['package_code_executed'])
            self.assertTrue((Path(result['package_root'])/'lib'/'compiler.js').is_file())
        with patch('manager_bridge.customer_runtime_prep.build_opener') as factory:
            factory.return_value.open.return_value=Response(body+b'tampered')
            with self.assertRaisesRegex(Conflict,'integrity mismatch'):
                prepare_locked_registry_package(self.source,sha,self.root/'tampered','fixture','1.2.3')
            self.assertFalse((self.root/'tampered'/'extracted').exists())
        with patch('manager_bridge.customer_runtime_prep.build_opener') as factory:
            with self.assertRaises(Conflict):prepare_locked_registry_package(self.source,sha,self.root/'wrong-version','fixture','9.9.9')
            factory.assert_not_called()

    def test_export_substitution_cannot_transform_package_inputs_before_install(self):
        package=json.loads((self.source/'package.json').read_text())
        package['description']='valid JSON attribute marker $Format:%H$'
        (self.source/'package.json').write_text(json.dumps(package))
        (self.source/'.gitattributes').write_text('package.json export-subst\n')
        subprocess.run(['/usr/bin/git','-C',str(self.source),'add','.'],check=True,stdout=subprocess.DEVNULL)
        subprocess.run(['/usr/bin/git','-C',str(self.source),'-c','user.name=Fixture','-c','user.email=fixture@invalid','commit','-m','attributes'],check=True,stdout=subprocess.DEVNULL)
        sha=subprocess.check_output(['/usr/bin/git','-C',str(self.source),'rev-parse','HEAD']).decode().strip()
        actual_run=subprocess.run;calls=[]
        def observe(argv,*args,**kwargs):
            calls.append(argv)
            if str(argv[0])==NPM:raise AssertionError('installer invoked on attribute-transformed input')
            return actual_run(argv,*args,**kwargs)
        with patch('manager_bridge.customer_runtime_prep.subprocess.run',side_effect=observe):
            with self.assertRaisesRegex(Conflict,'exact committed blob'):
                prepare_locked_node(self.source,sha,self.root/'substituted')
        staged=json.loads((self.root/'substituted'/'source'/'package.json').read_text())
        self.assertIn(sha,staged['description'])
        self.assertFalse(any(str(argv[0])==NPM for argv in calls))
