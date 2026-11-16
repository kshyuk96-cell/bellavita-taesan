"""Private result-body retention. Bytes are inert data, never executable instructions."""
from contextlib import contextmanager
import ctypes
import errno
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
from .store import Conflict,NotFound,PermissionDenied,digest
from .maintenance_registry import name
from .three_tier import ThreeTier
from .operations_reporting import ArtifactObservation,KINDS,MAX_ARTIFACT_BYTES

_SECRET=re.compile(rb'(?:sk-(?:proj-)?[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|(?:AKIA|ASIA)[A-Z0-9]{16}|bearer\s+[^\s\x00]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|(?:https?|postgres(?:ql)?|redis)://[^/\s:@]{1,128}:[^/@\s]{1,256}@|authorization\s*:\s*basic\s+[A-Za-z0-9+/=]{8,}|(?:access_token|refresh_token|id_token|api_key|client_secret|aws_secret_access_key)["\x27]?\s*[:=]\s*["\x27]?[^\s"\x27,}\x00]{8,})',re.IGNORECASE)
_OPAQUE=re.compile(rb'[\x21-\x7e]{32,}')


def _physical_root(root):
    path=Path(root).absolute()
    if '..' in path.parts or any(part.is_symlink() for part in (path,*path.parents)):
        raise ValueError('physical private artifact directory required')
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('owned private artifact directory required')
    return path,(info.st_dev,info.st_ino)


def _publish(rootfd,temporary,reference):
    # Linux renameat2 provides atomic publication without replacement or hardlinks.
    library=ctypes.CDLL(None,use_errno=True)
    rename=getattr(library,'renameat2',None)
    if rename is None:raise ValueError('atomic no-replace artifact publication unavailable')
    rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    rename.restype=ctypes.c_int
    if rename(rootfd,os.fsencode(temporary),rootfd,os.fsencode(reference),1):
        code=ctypes.get_errno()
        if code==errno.EEXIST:raise Conflict('artifact destination already exists')
        raise OSError(code,'artifact publication failed')


class ArtifactContent:
    def __init__(self,core,private_root,*,sources=None,max_total_bytes=64*1024*1024):
        if not isinstance(core,ThreeTier):raise ValueError('registered core required')
        if type(max_total_bytes) is not int or not MAX_ARTIFACT_BYTES<=max_total_bytes<=1024*1024*1024:
            raise ValueError('bounded artifact content capacity required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.root,self.identity=_physical_root(private_root)
        self.root_binding=digest(self.identity);self.max_total_bytes=max_total_bytes
        self.sources=dict(sources or {})
        for key,observer in self.sources.items():
            if type(key) is not tuple or len(key)!=2 or type(key[1]) is not str or key[1] not in KINDS or not callable(observer):raise ValueError('trusted project artifact source required')
            name(key[0])
        with self._directory() as fd:self._usage(fd)
        with self.store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS artifact_content_records (
                reference TEXT PRIMARY KEY,project TEXT,kind TEXT,artifact_key TEXT,config TEXT,
                root_binding TEXT,sha256 TEXT,size_bytes INTEGER,observed_at REAL,expires REAL,
                device INTEGER,inode INTEGER,UNIQUE(project,kind,artifact_key,config,root_binding))''')

    @contextmanager
    def _directory(self):
        path,identity=_physical_root(self.root)
        if identity!=self.identity:raise Conflict('artifact directory identity changed')
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd)
            if (info.st_dev,info.st_ino)!=self.identity:raise Conflict('artifact directory identity changed')
            yield fd
        finally:os.close(fd)

    @staticmethod
    def _file(info):
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>MAX_ARTIFACT_BYTES:
            raise ValueError('bounded private unshared artifact file required')

    def _usage(self,fd):
        total=0
        with os.scandir(fd) as entries:
            for count,entry in enumerate(entries,1):
                if count>4096:raise ValueError('artifact directory entry capacity exceeded')
                info=entry.stat(follow_symlinks=False);self._file(info);total+=info.st_size
                if total>self.max_total_bytes:raise Conflict('artifact content capacity exceeded')
        return total

    def _scope(self,c,token,project):
        name(project);actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        return self.registry._get(c,token,project)['fingerprint']

    @staticmethod
    def _scan(c,token,data):
        if token.encode() in data or _SECRET.search(data):raise ValueError('artifact contains credential material')
        # Stored bearers are hashes. Scan every bounded printable ASCII substring, including
        # tokens embedded between binary bytes or adjacent ASCII prefixes/suffixes.
        rows=c.execute('SELECT token_hash FROM bridge_tokens LIMIT 10001').fetchall()
        if len(rows)>10000:raise ValueError('artifact credential scan capacity exceeded')
        known={row['token_hash'] for row in rows};attempts=0
        for match in _OPAQUE.finditer(data):
            value=match.group()
            if len(value)>2048:raise ValueError('artifact credential scan capacity exceeded')
            for length in range(32,min(len(value),1024)+1):
                for offset in range(len(value)-length+1):
                    attempts+=1
                    if attempts>100000:raise ValueError('artifact credential scan capacity exceeded')
                    if hashlib.sha256(value[offset:offset+length]).hexdigest() in known:
                        raise ValueError('artifact contains credential material')

    @staticmethod
    def _public(row,now):
        return {'reference':row['reference'],'project':row['project'],'kind':row['kind'],
                'sha256':row['sha256'],'size_bytes':row['size_bytes'],'observed_at':row['observed_at'],
                'expires_at':row['expires'],'state':'expired' if row['expires']<=now else 'retained',
                'retrieval':'metadata_only','body_access':'privileged_local_only','http_body_download_enabled':False,
                'physical_deletion_verified':False,'acceptance_authorized':False}

    def retain(self,token,project,kind,artifact_key,*,retention_seconds=86400):
        name(project);name(artifact_key)
        if type(kind) is not str or kind not in KINDS or type(retention_seconds) is not int or not 60<=retention_seconds<=30*86400:
            raise ValueError('bounded artifact retention required')
        with self.store._transaction() as c:
            config=self._scope(c,token,project)
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,{'project':project,'kind':kind,'artifact_key':artifact_key})
        with self._directory():pass
        observer=self.sources.get((project,kind))
        if observer is None:raise NotFound('trusted artifact source not configured')
        observation=observer(project,artifact_key)
        if not isinstance(observation,ArtifactObservation) or (observation.project,observation.kind,observation.artifact_key)!=(project,kind,artifact_key):raise ValueError('exact trusted artifact observation required')
        data=observation.data;sha=hashlib.sha256(data).hexdigest()
        with self.store._transaction() as c:
            if self._scope(c,token,project)!=config:raise Conflict('artifact configuration changed')
            now=self.store._now()
            if not 0<=now-observation.observed_at<=300:raise Conflict('fresh artifact observation required')
            self._scan(c,token,data)
            old=c.execute('SELECT * FROM artifact_content_records WHERE project=? AND kind=? AND artifact_key=? AND config=? AND root_binding=?',(project,kind,artifact_key,config,self.root_binding)).fetchone()
            if old:
                if (old['sha256'],old['size_bytes'])!=(sha,len(data)):raise Conflict('artifact content is immutable')
                return self._public(old,now)
            with self._directory() as rootfd:
                if self._usage(rootfd)+len(data)>self.max_total_bytes:raise Conflict('artifact content capacity exceeded')
                reference=secrets.token_hex(16);temporary='.pending-'+secrets.token_hex(16)
                published=False
                try:
                    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=rootfd)
                    with os.fdopen(fd,'wb') as stream:
                        stream.write(data);stream.flush();os.fsync(stream.fileno())
                    _publish(rootfd,temporary,reference);published=True;os.fsync(rootfd)
                    info=os.stat(reference,dir_fd=rootfd,follow_symlinks=False);self._file(info)
                    c.execute('INSERT INTO artifact_content_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (reference,project,kind,artifact_key,config,self.root_binding,sha,len(data),observation.observed_at,
                         observation.observed_at+retention_seconds,info.st_dev,info.st_ino))
                    row=c.execute('SELECT * FROM artifact_content_records WHERE reference=?',(reference,)).fetchone()
                    result=self._public(row,now);_reject_known_tokens(c,result)
                    return result
                finally:
                    # Remove only this operation's uncommitted temporary file.
                    # Published orphan content stays private and counts toward cap.
                    if not published:
                        try:os.unlink(temporary,dir_fd=rootfd)
                        except FileNotFoundError:pass

    def _row(self,c,token,reference):
        name(reference)
        actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
        row=c.execute('SELECT * FROM artifact_content_records WHERE reference=?',(reference,)).fetchone()
        if row is None:raise NotFound('retained artifact unavailable')
        config=self._scope(c,token,row['project'])
        if row['config']!=config or row['root_binding']!=self.root_binding:raise Conflict('artifact storage or configuration changed')
        return row

    def metadata(self,token,reference):
        with self.store._transaction() as c:
            row=self._row(c,token,reference);result=self._public(row,self.store._now())
            from .agent_directory import _reject_known_tokens
            _reject_known_tokens(c,result)
            with self._directory():pass
            return result

    def read_body(self,token,reference):
        """Privileged local Python API only. No HTTP content framing is provided."""
        with self.store._transaction() as c:
            row=dict(self._row(c,token,reference))
            if row['expires']<=self.store._now():raise Conflict('artifact retention expired')
        with self._directory() as rootfd:
            fd=os.open(reference,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=rootfd)
            with os.fdopen(fd,'rb') as stream:
                info=os.fstat(stream.fileno());self._file(info)
                if (info.st_dev,info.st_ino,info.st_size)!=(row['device'],row['inode'],row['size_bytes']):raise Conflict('artifact file identity or size changed')
                data=stream.read(MAX_ARTIFACT_BYTES+1)
            if len(data)!=row['size_bytes'] or not hmac.compare_digest(hashlib.sha256(data).hexdigest(),row['sha256']):raise Conflict('artifact content integrity changed')
        with self.store._transaction() as c:
            current=self._row(c,token,reference)
            if dict(current)!=row or current['expires']<=self.store._now():raise Conflict('artifact retention binding changed')
            self._scan(c,token,data)
            with self._directory():pass
        return data

    def metadata_source(self,token_loader,project,kind):
        """Trusted OperationsReporting observer over already retained, verified bytes."""
        name(project)
        if type(kind) is not str or kind not in KINDS or not callable(token_loader):raise ValueError('trusted artifact receipt source required')
        def observe(selected_project,artifact_key):
            if selected_project!=project:raise PermissionDenied('artifact source project changed')
            name(artifact_key);token=token_loader()
            with self.store._transaction() as c:
                config=self._scope(c,token,project)
                from .agent_directory import _reject_known_tokens
                _reject_known_tokens(c,{'artifact_key':artifact_key})
                row=c.execute('SELECT reference,observed_at FROM artifact_content_records WHERE project=? AND kind=? AND artifact_key=? AND config=? AND root_binding=?',
                    (project,kind,artifact_key,config,self.root_binding)).fetchone()
                if row is None:raise NotFound('retained artifact unavailable')
                reference,observed=row['reference'],row['observed_at']
            return ArtifactObservation(project,kind,artifact_key,self.read_body(token,reference),observed)
        return observe
