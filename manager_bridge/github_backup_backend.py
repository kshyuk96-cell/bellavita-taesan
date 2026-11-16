"""Private-repository create-only recovery objects using the existing gh login.

No repository is created, no existing object is updated/deleted, and no auth
contents or endpoint selector enter from HTTP. Register only a dedicated backup
repository: normal customer/source repositories are not a default destination.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
from urllib.parse import quote
from .encrypted_backup import BackupBackend
from .maintenance_registry import name
from .store import Conflict, canonical_json

MAX_OBJECT_BYTES=1024*1024


class GhJsonClient:
    def __init__(self,binary='/usr/bin/gh'):
        if binary!='/usr/bin/gh':raise ValueError('fixed trusted gh client required')
        self.binary=binary

    def __call__(self,method,path,body=None):
        if method not in ('GET','PUT') or not path.startswith('repos/') or '://' in path or '\n' in path:
            raise ValueError('bounded fixed GitHub resource required')
        command=[self.binary,'api','--hostname','github.com','--method',method,path]
        encoded=None
        if body is not None:
            encoded=canonical_json(body).encode()
            if len(encoded)>2*MAX_OBJECT_BYTES:raise ValueError('bounded GitHub request required')
            command+=['--input','-']
        allowed={'HOME','PATH','GH_TOKEN','GITHUB_TOKEN','HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','NO_PROXY','SSL_CERT_FILE','SSL_CERT_DIR'}
        env={k:v for k,v in os.environ.items() if k in allowed}
        try:
            value=subprocess.run(command,input=encoded,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,timeout=30)
            if value.returncode or len(value.stdout)>2*MAX_OBJECT_BYTES:raise ValueError()
            result=json.loads(value.stdout)
            if type(result) is not dict:raise ValueError()
            return result
        except (ValueError,OSError,subprocess.SubprocessError):raise Conflict('GitHub recovery transport unavailable') from None


class GithubBackupBackend(BackupBackend):
    def __init__(self,repository,repository_id,prefix,*,authorizer,client=None,branch='main'):
        if type(repository) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) or any(part in ('.','..') for part in repository.split('/')):raise ValueError('exact dedicated repository required')
        if type(repository_id) is not int or repository_id<=0 or not callable(authorizer):raise ValueError('pinned repository and trusted current-owner authorization required')
        name(prefix);name(branch)
        if type(client) is not type(None) and not callable(client):raise ValueError('trusted fixed GitHub client required')
        self.repository,self.repository_id,self.prefix,self.branch=repository,repository_id,prefix,branch
        self.authorizer,self.client=authorizer,client or GhJsonClient()

    def _request(self,method,path,body=None):
        if self.authorizer() is not True:raise Conflict('backup transport owner authority unavailable')
        try:result=self.client(method,path,body)
        except Exception:raise Conflict('GitHub recovery transport unavailable') from None
        if self.authorizer() is not True:raise Conflict('backup transport owner authority changed')
        if type(result) is not dict:raise Conflict('bounded GitHub response required')
        return result

    def _repo(self):
        value=self._request('GET','repos/'+self.repository)
        if (value.get('id')!=self.repository_id or value.get('full_name')!=self.repository or value.get('private') is not True
                or value.get('visibility')!='private' or value.get('archived') is not False):
            raise Conflict('exact private dedicated backup repository required')

    def _path(self,backup_id):
        name(backup_id)
        return 'repos/'+self.repository+'/contents/'+quote(self.prefix,safe='')+'/'+quote(backup_id,safe='')+'.aead'

    @staticmethod
    def _sha(payload):return hashlib.sha1(b'blob '+str(len(payload)).encode()+b'\0'+payload).hexdigest()

    def put_if_absent(self,backup_id,payload):
        if type(payload) is not bytes or not 1<=len(payload)<=MAX_OBJECT_BYTES:raise ValueError('bounded recovery object required')
        self._repo()
        value=self._request('PUT',self._path(backup_id),{'branch':self.branch,'message':'Retain Taesan recovery object',
            'content':base64.b64encode(payload).decode('ascii')})
        # No SHA is supplied: GitHub's create-content operation rejects replacing
        # an existing file. Failed/uncertain requests are never automatically replayed.
        content=value.get('content');commit=value.get('commit')
        if (type(content) is not dict or content.get('sha')!=self._sha(payload) or type(content.get('size')) is not int or content.get('size')!=len(payload)
                or type(commit) is not dict or type(commit.get('sha')) is not str or not re.fullmatch('[a-f0-9]{40}',commit['sha'])):
            raise Conflict('exact GitHub recovery object receipt required')
        self._repo()
        return True

    def get(self,backup_id,max_bytes):
        if type(max_bytes) is not int or max_bytes<1:raise ValueError('bounded recovery read required')
        self._repo();value=self._request('GET',self._path(backup_id)+'?ref='+quote(self.branch,safe=''))
        if (value.get('type')!='file' or value.get('encoding')!='base64' or type(value.get('content')) is not str
                or type(value.get('size')) is not int or not 0<value['size']<=min(max_bytes,MAX_OBJECT_BYTES)
                or len(value['content'])>2*MAX_OBJECT_BYTES):raise Conflict('bounded GitHub recovery object required')
        try:payload=base64.b64decode(''.join(value['content'].splitlines()),validate=True)
        except (ValueError,UnicodeError):raise Conflict('invalid GitHub recovery object encoding') from None
        if len(payload)!=value['size'] or self._sha(payload)!=value.get('sha'):raise Conflict('GitHub recovery object integrity mismatch')
        self._repo()
        return payload
