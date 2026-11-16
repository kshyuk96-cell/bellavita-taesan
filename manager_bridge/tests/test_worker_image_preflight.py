"""Recipe/context policy tests; fixture Docker inspect is not a built image."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from manager_bridge.worker_image_preflight import BASE_IMAGE,ROLES,prepare_build_context,inspect_local_image


class WorkerImageTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.source=self.root/'source';self.source.mkdir()
        recipes=self.source/'ops'/'worker-images';recipes.mkdir(parents=True)
        actual=Path(__file__).resolve().parents[2]/'ops'/'worker-images'
        for item in actual.iterdir():
            if item.is_file():(recipes/item.name).write_bytes(item.read_bytes())
        (self.source/'application.py').write_text('print("trusted source")\n')
        self.git('init','-q');self.git('add','.');self.git('commit','-qm','Image fixture')
        self.commit=self.git('rev-parse','HEAD')

    def git(self,*args):
        return subprocess.check_output(['git','-c','user.name=Fixture','-c','user.email=fixture@invalid','-C',str(self.source),*args],stderr=subprocess.DEVNULL,text=True).strip()

    def test_distinct_roles_fixed_base_no_auth_context_and_exact_source_pin(self):
        self.assertEqual(3,len(set(ROLES.values())))
        (self.source/'auth.json').write_text('never include untracked credentials')
        value=prepare_build_context(source=self.source,expected_commit=self.commit,role='control',destination=self.root/'context')
        self.assertFalse((self.root/'context'/'app'/'auth.json').exists())
        self.assertEqual(self.commit,self.git('rev-parse','HEAD'))
        self.assertFalse(value['images_built']);self.assertFalse(value['credentials_included'])
        for role,uid in ROLES.items():
            recipe=(self.source/'ops'/'worker-images'/(role+'.Dockerfile')).read_text()
            self.assertIn('FROM '+BASE_IMAGE,recipe)
            self.assertIn('USER '+str(uid)+':'+str(uid),recipe)
            self.assertNotIn('COPY .',recipe)
            self.assertIn('--only-binary=:all: -r /app/manager_bridge/runtime-requirements.txt',recipe)
            self.assertNotIn('-r /app/requirements.txt',recipe)
        with self.assertRaises(ValueError):prepare_build_context(source=self.source,expected_commit='a'*40,role='control',destination=self.root/'wrong')

    def test_tracked_auth_and_missing_binary_fail_closed(self):
        with self.assertRaises(ValueError):prepare_build_context(source=self.source,expected_commit=self.commit,role='developer',destination=self.root/'missing')
        (self.source/'auth.json').write_text('synthetic forbidden credential fixture');self.git('add','auth.json');self.git('commit','-qm','Forbidden fixture')
        with self.assertRaises(ValueError):prepare_build_context(source=self.source,expected_commit=self.git('rev-parse','HEAD'),role='control',destination=self.root/'forbidden')

    def test_image_metadata_does_not_claim_runtime_verification(self):
        image='sha256:'+'a'*64
        fixture=[{'Id':image,'Config':{'User':'10002:10002','Labels':{'org.opencontainers.image.revision':self.commit,'io.taesan.worker.role':'developer'}}}]
        with patch('manager_bridge.worker_image_preflight.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(fixture).encode())):
            value=inspect_local_image(image,expected_role='developer',expected_commit=self.commit)
            self.assertTrue(value['expected_bindings_match']);self.assertFalse(value['runtime_probe_performed']);self.assertFalse(value['isolation_verified'])
            self.assertFalse(inspect_local_image(image,expected_role='reviewer',expected_commit=self.commit)['expected_bindings_match'])
        with self.assertRaises(ValueError):inspect_local_image('arbitrary:latest',expected_role='control',expected_commit=self.commit)
