"""Trusted local source-only archive staging; no execution, activation or Git proof."""
from dataclasses import asdict
from pathlib import Path
import ctypes
import gzip
import hashlib
import io
import math
import os
import re
import shutil
import stat
import tarfile
import tempfile
import uuid
from .connection_readiness import SourcePin
from .three_tier import ThreeTier
from .store import Conflict,digest


def _private(path):
    path=Path(path).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('physical private staging root required')
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o700 or info.st_uid!=os.getuid():
        raise ValueError('owned private staging root required')
    return path,(info.st_dev,info.st_ino)


def _parts(value):
    if type(value) is not str or not value or len(value)>1024 or '\\' in value or '\x00' in value or ':' in value:
        raise Conflict('invalid archive member path')
    parts=value.rstrip('/').split('/')
    if len(parts)>32:raise Conflict('archive path depth bound exceeded')
    if value.startswith('/') or any(p in ('','.','..') for p in parts):raise Conflict('unsafe archive member path')
    return tuple(parts)


def _allowed(parts):
    for part in parts:
        lower=part.casefold()
        if (lower in {'.git','.codex','.aws','.ssh','.config','.docker','.npmrc','.pypirc','.netrc','.envrc','auth.json','credentials','credentials.json','token.json','tokens.json','secrets.json','id_rsa','id_ed25519'}
                or lower=='.env' or lower.startswith(('.env.','client_secret')) or lower in {'service-account.json','service_account.json','application_default_credentials.json'} or lower.endswith(('.pem','.p12','.pfx','.key','.token'))):
            raise Conflict('credential-bearing archive path refused')


def _rename_no_replace(source,target):
    # Linux atomic directory publication; refuse rather than unsafe fallback.
    libc=ctypes.CDLL(None,use_errno=True)
    rename=getattr(libc,'renameat2',None)
    if rename is None:raise Conflict('atomic no-replace staging unsupported')
    if rename(-100,os.fsencode(source),-100,os.fsencode(target),1)!=0:
        error=ctypes.get_errno()
        raise OSError(error,os.strerror(error))


class SourceSnapshotStage:
    def __init__(self,core,private_root,*,current_pin,max_archive_bytes=32*1024*1024,max_expanded_bytes=128*1024*1024,max_files=20000):
        if type(core) is not ThreeTier or not callable(current_pin):raise ValueError('same-core trusted source observer required')
        for value,maximum in ((max_archive_bytes,128*1024*1024),(max_expanded_bytes,512*1024*1024),(max_files,100000)):
            if type(value) is not int or not 1<=value<=maximum:raise ValueError('bounded archive limits required')
        self.core,self.store,self.registry=core,core.store,core.registry
        self.root,self.identity=_private(private_root);self.current_pin=current_pin
        self.max_archive_bytes,self.max_expanded_bytes,self.max_files=max_archive_bytes,max_expanded_bytes,max_files

    def _check(self,token,pin,observed_at,expected_config=None):
        if _private(self.root)[1]!=self.identity:raise Conflict('staging root identity changed')
        now=self.store._now()
        if type(observed_at) not in (int,float) or not math.isfinite(observed_at) or not 0<=now-observed_at<=300:
            raise Conflict('fresh trusted source observation required')
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token);self.store._role(actor,('owner','manager'))
            config=self.registry._get(c,token,pin.project,('owner','manager'))
            registered=config['config']
            if (registered['source_kind'],registered['source_ref'],registered['branch'])!=(pin.source_kind,pin.source_ref,pin.branch):
                raise Conflict('registered source binding changed')
            fingerprint=config['fingerprint']
        if expected_config is not None and fingerprint!=expected_config:raise Conflict('project configuration changed during staging')
        current=self.current_pin(pin.project)
        if type(current) is not SourcePin or current!=pin:raise Conflict('current trusted source pin changed')
        with self.store._transaction() as c:
            latest=self.registry._get(c,token,pin.project,('owner','manager'))
            if latest['fingerprint']!=fingerprint:raise Conflict('configuration changed during source observation')
        if not 0<=self.store._now()-observed_at<=300:raise Conflict('source observation expired during staging')
        return fingerprint

    def stage(self,token,pin,archive_sha256,observed_at,archive,*,strip_prefix=None):
        if type(pin) is not SourcePin or type(archive) is not bytes or not 0<len(archive)<=self.max_archive_bytes:
            raise ValueError('typed source pin and bounded archive bytes required')
        if type(archive_sha256) is not str or not re.fullmatch('[a-f0-9]{64}',archive_sha256):raise ValueError('exact trusted archive hash required')
        if hashlib.sha256(archive).hexdigest()!=archive_sha256:raise Conflict('archive hash mismatch')
        prefix=None if strip_prefix is None else _parts(strip_prefix)
        if prefix is not None and len(prefix)!=1:raise ValueError('one known archive folder required')
        if prefix is not None:_allowed(prefix)
        config=self._check(token,pin,observed_at)
        temporary=Path(tempfile.mkdtemp(prefix='.staging-',dir=self.root));published=False
        final=self.root/('source-'+uuid.uuid4().hex)
        try:
            # Bound decompression itself before tarfile parses extended metadata.
            with tempfile.TemporaryFile(dir=self.root) as raw:
                stream=gzip.GzipFile(fileobj=io.BytesIO(archive)) if archive.startswith(b'\x1f\x8b') else io.BytesIO(archive)
                expanded=0
                with stream:
                    while True:
                        block=stream.read(min(65536,self.max_expanded_bytes-expanded+1))
                        if not block:break
                        expanded+=len(block)
                        if expanded>self.max_expanded_bytes:raise Conflict('expanded archive bound exceeded')
                        raw.write(block)
                raw.seek(0);seen=set();raw_seen=set();nodes=set();files=[];total=0;members=0
                with tarfile.open(fileobj=raw,mode='r|') as tar:
                    for member in tar:
                        members+=1
                        if members>self.max_files:raise Conflict('archive member bound exceeded')
                        parts=_parts(member.name);_allowed(parts)
                        if parts in raw_seen:raise Conflict('duplicate archive member')
                        raw_seen.add(parts)
                        if prefix:
                            if parts[:1]!=prefix:raise Conflict('known top folder mismatch')
                            parts=parts[1:]
                            if not parts:
                                if not member.isdir():raise Conflict('top folder is not directory')
                                continue
                        _allowed(parts)
                        key='/'.join(parts)
                        if key in seen:raise Conflict('duplicate archive member')
                        seen.add(key)
                        if not member.isdir() and not member.isfile() or member.sparse is not None:
                            raise Conflict('nonregular archive member refused')
                        # Count every actual node, including parents omitted by tar.
                        nodes.update(parts[:depth] for depth in range(1,len(parts)+1))
                        if len(nodes)>self.max_files:raise Conflict('source filesystem node bound exceeded')
                        target=temporary.joinpath(*parts)
                        target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                        if member.isdir():
                            target.mkdir(mode=0o700,exist_ok=True);continue
                        total+=member.size
                        if member.size<0 or total>self.max_expanded_bytes:raise Conflict('source byte bound exceeded')
                        source=tar.extractfile(member)
                        if source is None:raise Conflict('regular archive member unavailable')
                        sha=hashlib.sha256();written=0
                        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                        with os.fdopen(fd,'wb') as output,source:
                            while True:
                                block=source.read(min(65536,member.size-written+1))
                                if not block:break
                                written+=len(block)
                                if written>member.size:raise Conflict('archive member size mismatch')
                                sha.update(block);output.write(block)
                            if written!=member.size:raise Conflict('truncated archive member')
                            output.flush();os.fsync(output.fileno())
                        files.append({'path':key,'sha256':sha.hexdigest(),'bytes':written})
            if not files:raise Conflict('empty source snapshot refused')
            self._check(token,pin,observed_at,config)
            # Restrict all implicitly-created intermediate directory permissions.
            for directory,_,_ in os.walk(temporary):os.chmod(directory,0o700)
            _rename_no_replace(temporary,final);published=True
            self._check(token,pin,observed_at,config)
            return {'pin':asdict(pin),'config_fingerprint':config,'archive_sha256':archive_sha256,
                'tree_sha256':digest(sorted(files,key=lambda f:f['path'])),'files':len(files),'filesystem_nodes':len(nodes),'source_bytes':total,
                'expanded_archive_bytes':expanded,'observed_at':observed_at,'path':str(final),
                'scope':'source_snapshot_only','git_commit_independently_verified':False,'credential_content_audited':False,
                'tests_executed':False,'worker_connected':False,'activated':False}
        except BaseException:
            if _private(self.root)[1]==self.identity:
                shutil.rmtree(final if published else temporary,ignore_errors=True)
            raise
