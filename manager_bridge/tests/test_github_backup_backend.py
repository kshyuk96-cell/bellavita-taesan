"""Provider contract fixtures; these do not attest a real offsite repository."""
import base64
import json
import subprocess
import unittest
from unittest.mock import patch
from manager_bridge.github_backup_backend import GithubBackupBackend,GhJsonClient,MAX_OBJECT_BYTES
from manager_bridge.store import Conflict


class GithubBackupBackendTests(unittest.TestCase):
    def setUp(self):
        self.active=True;self.files={};self.calls=[]
        self.meta={'id':123,'full_name':'fixture/recovery','private':True,'visibility':'private','archived':False}
        self.backend=GithubBackupBackend('fixture/recovery',123,'objects',authorizer=lambda:self.active,client=self.client)

    def client(self,method,path,body=None):
        self.calls.append((method,path,body))
        if path=='repos/fixture/recovery':return dict(self.meta)
        key=path.split('?')[0]
        if method=='PUT':
            if key in self.files:raise Conflict('existing object')
            data=base64.b64decode(body['content']);self.files[key]=data
            return {'content':{'sha':self.backend._sha(data),'size':len(data)},'commit':{'sha':'a'*40}}
        data=self.files[key]
        return {'type':'file','encoding':'base64','content':base64.b64encode(data).decode(),'sha':self.backend._sha(data),'size':len(data)}

    def test_create_readback_never_sends_overwrite_sha(self):
        self.assertTrue(self.backend.put_if_absent('one',b'synthetic'))
        self.assertEqual(self.backend.get('one',100),b'synthetic')
        for method,path,body in self.calls:
            if method=='PUT':self.assertNotIn('sha',body)
        with self.assertRaises(Conflict):self.backend.put_if_absent('one',b'overwrite')
        self.assertEqual(self.backend.get('one',100),b'synthetic')

    def test_public_unknown_or_changed_repository_denies_without_upload(self):
        for changes in ({'private':False,'visibility':'public'},{'id':124},{'full_name':'fixture/other'},{'archived':True}):
            self.calls.clear();original=dict(self.meta);self.meta.update(changes)
            with self.assertRaises(Conflict):self.backend.put_if_absent('denied',b'synthetic')
            self.assertFalse(any(c[0]=='PUT' for c in self.calls));self.meta=original

    def test_corrupted_or_unbounded_read_rejected(self):
        self.backend.put_if_absent('one',b'content')
        with self.assertRaises(Conflict):self.backend.get('one',3)
        actual=self.backend.client
        def corrupt(method,path,body=None):
            value=actual(method,path,body)
            if 'contents/' in path and method=='GET':value['sha']='b'*40
            return value
        self.backend.client=corrupt
        with self.assertRaises(Conflict):self.backend.get('one',100)
        with self.assertRaises(ValueError):self.backend.put_if_absent('huge',b'a'*(MAX_OBJECT_BYTES+1))

    def test_authority_revocation_during_metadata_blocks_upload(self):
        actual=self.backend.client
        def revoke(method,path,body=None):
            result=actual(method,path,body);self.active=False;return result
        self.backend.client=revoke
        with self.assertRaises(Conflict):self.backend.put_if_absent('denied',b'content')
        self.assertEqual(len(self.calls),1);self.assertEqual(self.files,{})

    def test_visibility_change_after_put_is_uncertain_not_success(self):
        actual=self.backend.client
        def changed(method,path,body=None):
            result=actual(method,path,body)
            if method=='PUT':self.meta.update(private=False,visibility='public')
            return result
        self.backend.client=changed
        with self.assertRaises(Conflict):self.backend.put_if_absent('uncertain',b'content')
        self.assertEqual(len(self.files),1)

    def test_cli_errors_do_not_expose_transport_or_key_details(self):
        client=GhJsonClient()
        with patch('manager_bridge.github_backup_backend.subprocess.run',return_value=subprocess.CompletedProcess([],1,b'',b'private-sentinel')):
            with self.assertRaises(Conflict) as error:client('GET','repos/fixture/recovery')
        self.assertNotIn('sentinel',str(error.exception))
        with patch.dict('os.environ',{'OPENAI_API_KEY':'must-not-forward'}), patch('manager_bridge.github_backup_backend.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'{}',b'')) as run:
            client('GET','repos/fixture/recovery')
        self.assertNotIn('OPENAI_API_KEY',run.call_args.kwargs['env'])
        self.assertIn('--hostname',run.call_args.args[0]);self.assertIn('github.com',run.call_args.args[0])

    def test_client_selectors_are_not_arbitrary_endpoints(self):
        for repository in ('https://other.example/repo','../repo','owner/repo?token=x'):
            with self.assertRaises(ValueError):GithubBackupBackend(repository,123,'objects',authorizer=lambda:True)
        with self.assertRaises(ValueError):self.backend.get('../escape',100)
        with self.assertRaises(ValueError):GhJsonClient('/tmp/untrusted')
