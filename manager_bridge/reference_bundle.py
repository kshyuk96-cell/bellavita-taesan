"""Privileged local reference preparation, not worker delivery or a sandbox.

Caller-selected output names/paths, rendering, network fetches and execution are
absent. Owned mode bits are hygiene; the owner process can still change them.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import stat
from .artifact_content import ArtifactContent,_physical_root
from .store import Conflict
from .worker_quality_handoff import WorkerQualityHandoff


@dataclass(frozen=True)
class PreparedReference:
    reference:str
    sha256:str
    size_bytes:int
    filename:str


@dataclass(frozen=True)
class PreparedReferenceBundle:
    directory:Path
    files:tuple
    handoff:dict
    binding_digest:str
    prepared:bool=True
    delivered:bool=False
    viewed:bool=False
    execution_authorized:bool=False
    os_sandbox_enforced:bool=False
    owner_process_can_modify:bool=True


def _overlap(a,b):return a==b or a in b.parents or b in a.parents


def _clear(fd):
    """Unlink owned temporary contents without following any substituted links."""
    os.fchmod(fd,0o700)
    for name in os.listdir(fd):
        info=os.stat(name,dir_fd=fd,follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            try:
                current=os.fstat(child)
                if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise Conflict('temporary reference directory changed')
                _clear(child)
            finally:os.close(child)
            os.rmdir(name,dir_fd=fd)
        else:os.unlink(name,dir_fd=fd)


class ReferenceBundles:
    def __init__(self,core,artifact_content,private_root,*,handoff=None,max_bytes=16*1024*1024,max_files=10):
        if type(artifact_content) is not ArtifactContent or artifact_content.core is not core:raise ValueError('same-core private artifact content required')
        self.handoff=handoff or WorkerQualityHandoff(core)
        if type(self.handoff) is not WorkerQualityHandoff or self.handoff.core is not core:raise ValueError('same-core quality handoff required')
        if type(max_bytes) is not int or not 1<=max_bytes<=64*1024*1024 or type(max_files) is not int or not 1<=max_files<=10:raise ValueError('bounded private reference preparation required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.artifacts=artifact_content;self.root,self.identity=_physical_root(private_root)
        if _overlap(self.root,artifact_content.root) or self.root==self.store.path or self.root in self.store.path.parents:
            raise ValueError('dedicated reference preparation root required')
        self.max_bytes,self.max_files=max_bytes,max_files

    def _root(self):
        _,identity=_physical_root(self.root)
        if identity!=self.identity:raise Conflict('reference preparation root identity changed')
        fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        info=os.fstat(fd)
        if (info.st_dev,info.st_ino)!=self.identity:
            os.close(fd);raise Conflict('reference preparation root identity changed')
        return fd

    def _rows(self,token,packet):
        references=packet.get('references')
        if type(references) is not list or len(references)>self.max_files:raise Conflict('reference preparation count limit exceeded')
        if len({r['reference'] for r in references})!=len(references):raise Conflict('duplicate approved reference identity')
        rows=[];total=0
        with self.store._transaction() as c:
            for reference in references:
                row=dict(self.artifacts._row(c,token,reference['reference']))
                if (row['project']!=packet['task']['project'] or row['config']!=packet['task']['config_fingerprint']
                        or row['sha256']!=reference['sha256'] or row['expires']<=self.store._now()):
                    raise Conflict('approved reference project/config/hash/retention changed')
                total+=row['size_bytes']
                if total>self.max_bytes:raise Conflict('reference preparation byte limit exceeded')
                rows.append(row)
        return rows

    @contextmanager
    def prepare(self,token,task_id,role,candidate_digest=None):
        """Yield local read-only data copies; always clean them on exit/failure.

        The original handoff is preserved: its unmaterialized delivery metadata
        is not rewritten into a claim that a worker received or viewed a body.
        """
        packet=self.handoff.generate(token,task_id,role,candidate_digest)
        if packet.get('state')!='bound':raise Conflict('approved task-bound quality handoff required')
        rows=self._rows(token,packet)
        rootfd=self._root();folder='reference-bundle-'+secrets.token_hex(16);directoryfd=None;created=False
        try:
            os.mkdir(folder,mode=0o700,dir_fd=rootfd);created=True
            directoryfd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=rootfd)
            directory_identity=os.fstat(directoryfd);prepared=[];total=0
            for index,row in enumerate(rows):
                data=self.artifacts.read_body(token,row['reference']);total+=len(data)
                if total>self.max_bytes or len(data)!=row['size_bytes'] or hashlib.sha256(data).hexdigest()!=row['sha256']:
                    raise Conflict('reference body differs from approved metadata or bounds')
                filename='reference-'+str(index).zfill(2)+'-'+row['sha256']+'.data'
                fd=os.open(filename,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directoryfd)
                with os.fdopen(fd,'wb') as output:
                    output.write(data);output.flush();os.fsync(output.fileno());os.fchmod(output.fileno(),0o400)
                    info=os.fstat(output.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1:raise Conflict('private regular reference file required')
                prepared.append(PreparedReference(row['reference'],row['sha256'],len(data),filename))
            # Body reads can take time. Refresh every task, baseline, QA, source,
            # permission and retention fence before exposing this temporary copy.
            current=self.handoff.generate(token,task_id,role,candidate_digest)
            if current!=packet or current.get('binding_digest')!=packet.get('binding_digest'):
                raise Conflict('quality handoff changed during reference preparation')
            if self._rows(token,current)!=rows:raise Conflict('reference metadata changed during preparation')
            for reference in prepared:
                fd=os.open(reference.filename,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directoryfd)
                with os.fdopen(fd,'rb') as stream:
                    info=os.fstat(stream.fileno());body=stream.read(self.max_bytes+1)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid()
                            or stat.S_IMODE(info.st_mode)!=0o400 or len(body)!=reference.size_bytes
                            or hashlib.sha256(body).hexdigest()!=reference.sha256):raise Conflict('prepared reference file changed')
            os.fchmod(directoryfd,0o500);os.fsync(directoryfd)
            check=self._root();os.close(check)
            info=os.stat(folder,dir_fd=rootfd,follow_symlinks=False)
            if (info.st_dev,info.st_ino)!=(directory_identity.st_dev,directory_identity.st_ino):raise Conflict('temporary reference bundle changed')
            yield PreparedReferenceBundle(self.root/folder,tuple(prepared),packet,packet['binding_digest'])
        finally:
            try:
                if directoryfd is not None:
                    try:_clear(directoryfd)
                    finally:os.close(directoryfd)
            finally:
                try:
                    if created:os.rmdir(folder,dir_fd=rootfd)
                finally:os.close(rootfd)
