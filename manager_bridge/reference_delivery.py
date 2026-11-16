"""Trusted exact-file reference handoff context; no model, mount or viewing claim.

The host must independently probe and mount these exact files read-only. A quality
binding identifies criteria; mount_digest additionally pins this physical instance.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from .reference_bundle import ReferenceBundles,PreparedReferenceBundle,PreparedReference
from .store import Conflict,canonical_json,digest,token_hash


class ReferenceDeliveryContext:
    """Local host handle; metadata is inert and never contains reference bodies."""
    def __init__(self,delivery,prepared,project,task_id,role,candidate_digest,quality_packet,token):
        if type(prepared) is not PreparedReferenceBundle:raise ValueError('typed prepared reference bundle required')
        self._delivery,self._prepared=delivery,prepared
        self._project,self._task_id,self._role,self._candidate=project,task_id,role,candidate_digest
        self._quality=json.loads(canonical_json(quality_packet));self._token_identity=token_hash(token)
        self._rootfd=self._fd=None;self._closed=False
        self.directory=Path(prepared.directory);self.files=prepared.files;self.binding_digest=prepared.binding_digest
        if (prepared.handoff!=self._quality or prepared.binding_digest!=self._quality.get('binding_digest')
                or self._quality.get('state')!='bound' or self._quality.get('task',{}).get('project')!=project
                or self._quality['task'].get('task_id')!=task_id or self._quality.get('role')!=role
                or self._quality.get('candidate_digest')!=candidate_digest):raise Conflict('prepared references do not match exact quality/task/role binding')
        bundles=delivery.bundles
        if self.directory.parent!=bundles.root or not re.fullmatch('reference-bundle-[a-f0-9]{32}',self.directory.name):raise Conflict('server-generated reference directory required')
        if type(self.files) is not tuple or len(self.files)>bundles.max_files or any(type(f) is not PreparedReference for f in self.files):raise Conflict('typed bounded reference files required')
        approved={r['reference']:r['sha256'] for r in self._quality['references']}
        if len(approved)!=len(self.files) or len({f.filename for f in self.files})!=len(self.files) or {f.reference:f.sha256 for f in self.files}!=approved:raise Conflict('prepared files differ from approved references')
        for index,reference in enumerate(self.files):
            if reference.filename!='reference-'+str(index).zfill(2)+'-'+reference.sha256+'.data':raise Conflict('server-generated inert reference filename required')
        try:
            self._rootfd=bundles._root()
            self._fd=os.open(self.directory.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self._rootfd)
            self._rows=bundles._rows(token,self._quality)
            self._identity=self._physical_identity()
            self.mount_digest=digest({'binding_digest':self.binding_digest,'mount':self._identity})
            self._metadata={'schema_version':'reference-delivery-context/v1','project':project,'task_id':task_id,'role':role,
                            'candidate_digest':candidate_digest,'binding_digest':self.binding_digest,'mount_digest':self.mount_digest,
                            'directory':str(self.directory),'files':[{'reference':f.reference,'sha256':f.sha256,'size_bytes':f.size_bytes,
                                'filename':f.filename,'path':str(self.directory/f.filename)} for f in self.files],
                            'prepared':True,'delivered':False,'viewed':False,'visual_validation':'not_measured',
                            'read_only_mount_verified':False,'os_sandbox_enforced':False,'owner_process_can_modify':True,
                            'execution_authorized':False,'operational_authorized':False}
            if len(canonical_json(self._metadata).encode())>16384:raise Conflict('bounded inert reference prompt metadata required')
            self.revalidate()
        except BaseException:
            self.close();raise

    @property
    def metadata(self):
        if self._closed:raise Conflict('reference delivery context is closed')
        return json.loads(canonical_json(self._metadata))

    def _physical_identity(self):
        if self._closed or self._fd is None:raise Conflict('reference delivery context is closed')
        rootcheck=self._delivery.bundles._root();os.close(rootcheck)
        directory=os.fstat(self._fd);named=os.stat(self.directory.name,dir_fd=self._rootfd,follow_symlinks=False)
        if ((directory.st_dev,directory.st_ino)!=(named.st_dev,named.st_ino) or not stat.S_ISDIR(named.st_mode)
                or named.st_uid!=os.getuid() or stat.S_IMODE(named.st_mode)!=0o500):raise Conflict('prepared reference directory identity/permission changed')
        if set(os.listdir(self._fd))!={f.filename for f in self.files}:raise Conflict('unexpected reference file or metadata collision')
        records=[];total=0
        for reference in self.files:
            fd=os.open(reference.filename,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self._fd)
            with os.fdopen(fd,'rb') as stream:
                before=os.fstat(stream.fileno());h=hashlib.sha256();size=0
                if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1
                        or stat.S_IMODE(before.st_mode)!=0o400 or before.st_size!=reference.size_bytes):raise Conflict('private regular reference identity/permissions changed')
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    size+=len(chunk);total+=len(chunk)
                    if size>reference.size_bytes or total>self._delivery.bundles.max_bytes:raise Conflict('reference file byte bound changed')
                    h.update(chunk)
                after=os.fstat(stream.fileno())
                if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise Conflict('reference file changed during verification')
                if size!=reference.size_bytes or h.hexdigest()!=reference.sha256:raise Conflict('reference file differs from approved hash')
                named_file=os.stat(reference.filename,dir_fd=self._fd,follow_symlinks=False)
                if (named_file.st_dev,named_file.st_ino)!=(before.st_dev,before.st_ino):raise Conflict('reference filename identity changed during verification')
                records.append({'filename':reference.filename,'path':str(self.directory/reference.filename),'device':before.st_dev,
                                'inode':before.st_ino,'uid':before.st_uid,'links':before.st_nlink,'mode':stat.S_IMODE(before.st_mode),
                                'size_bytes':size,'sha256':reference.sha256,'mtime_ns':before.st_mtime_ns,'ctime_ns':before.st_ctime_ns})
        if set(os.listdir(self._fd))!={f.filename for f in self.files}:raise Conflict('reference directory changed during verification')
        named=os.stat(self.directory.name,dir_fd=self._rootfd,follow_symlinks=False)
        if (named.st_dev,named.st_ino)!=(directory.st_dev,directory.st_ino):raise Conflict('reference directory changed during verification')
        return {'directory':str(self.directory),'device':directory.st_dev,'inode':directory.st_ino,'uid':directory.st_uid,
                'links':directory.st_nlink,'mode':stat.S_IMODE(directory.st_mode),'mtime_ns':directory.st_mtime_ns,'ctime_ns':directory.st_ctime_ns,'files':records}

    def revalidate(self):
        """Heartbeat check; return True or fail closed on any authority/file drift."""
        if self._closed:raise Conflict('reference delivery context is closed')
        token=self._delivery.token_loader()
        if token_hash(token)!=self._token_identity:raise Conflict('reference delivery caller identity changed')
        current=self._delivery.bundles.handoff.generate(token,self._task_id,self._role,self._candidate)
        if current!=self._quality:raise Conflict('quality handoff changed during reference context')
        if self._delivery.bundles._rows(token,current)!=self._rows:raise Conflict('reference metadata/retention changed during reference context')
        identity=self._physical_identity()
        if identity!=self._identity or digest({'binding_digest':self.binding_digest,'mount':identity})!=self.mount_digest:
            raise Conflict('exact reference mount instance changed')
        return True

    def close(self):
        if self._closed:return
        self._closed=True
        try:
            if self._fd is not None:os.close(self._fd)
        finally:
            if self._rootfd is not None:os.close(self._rootfd)
            self._fd=self._rootfd=None


class ReferenceDelivery:
    def __init__(self,bundles,token_loader):
        if type(bundles) is not ReferenceBundles or not callable(token_loader):raise ValueError('trusted reference bundles and token loader required')
        self.bundles,self.token_loader=bundles,token_loader
        self.core,self.store=bundles.core,bundles.store

    @contextmanager
    def prepare(self,project,task_id,role,candidate_digest,quality_packet):
        """Prepare exact local data for a host; never accept caller output paths."""
        if type(quality_packet) is not dict:raise ValueError('exact trusted quality packet required')
        token=self.token_loader()
        current=self.bundles.handoff.generate(token,task_id,role,candidate_digest)
        if current!=quality_packet or current.get('task',{}).get('project')!=project:raise Conflict('supplied quality packet differs from current task/project')
        with self.bundles.prepare(token,task_id,role,candidate_digest) as prepared:
            context=ReferenceDeliveryContext(self,prepared,project,task_id,role,candidate_digest,current,token)
            try:yield context
            finally:context.close()
