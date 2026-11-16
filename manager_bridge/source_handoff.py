"""Link-free provider archive handoff, not a Git clone or execution authority.

Only trusted host code supplies snapshot identity, archive path/hash and an owned
private root. No network, Git command, hook or customer code is executed here.
"""
from dataclasses import asdict,dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import stat
import struct
import tarfile
import tempfile
import unicodedata
import zipfile
from .connection_readiness import SourcePin
from .store import Conflict,canonical_json,digest,_REPOSITORY

MAX_BYTES=128*1024*1024
MAX_FILES=20000


@dataclass(frozen=True)
class SourceSnapshot:
    project:str
    source_kind:str
    source_ref:str
    branch:str
    sha:str
    revision:str
    archive_digest:str

    def __post_init__(self):
        SourcePin(self.project,self.source_kind,self.source_ref,self.branch,self.sha,self.revision)
        if self.source_kind=='github' and not _REPOSITORY.fullmatch(self.source_ref):raise ValueError('exact GitHub repository reference required')
        if self.source_kind=='sites' and not re.fullmatch(r'appgprj_[A-Za-z0-9_]+',self.source_ref):raise ValueError('exact Sites project reference required')
        if type(self.archive_digest) is not str or not re.fullmatch('[a-f0-9]{64}',self.archive_digest):
            raise ValueError('exact trusted archive SHA256 required')


def _physical(path):
    path=Path(path).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('physical trusted local path required')
    return path


def _private_directory(path):
    path=_physical(path)
    if not path.exists():path.mkdir(parents=True,mode=0o700)
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('owned private 0700 source root required')
    return path


def _hash(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def _name(raw,directory):
    if type(raw) is not str or not raw or len(raw.encode('utf-8'))>512:raise Conflict('bounded archive path required')
    if raw.endswith('/'):
        if not directory:raise Conflict('file path cannot end with a directory separator')
        raw=raw[:-1]
    if (not raw or raw.startswith('/') or '\\' in raw or ':' in raw
            or any(unicodedata.category(ch).startswith('C') for ch in raw)):
        raise Conflict('unsafe archive path')
    parts=raw.split('/')
    if any(p in ('','.','..') or len(p.encode('utf-8'))>255 for p in parts):raise Conflict('noncanonical archive path')
    prohibited={'.git','node_modules','.ssh','.aws','.codex','.npmrc','.netrc'}
    if any(p.casefold() in prohibited or p.casefold() in ('.env','.env.local','.env.production') for p in parts):raise Conflict('Git/dependency/credential metadata forbidden in source archive')
    return raw


def _records(entries,max_bytes,max_files):
    records=[];seen={};total=0
    for raw,directory,size,handle in entries:
        path=_name(raw,directory);key=unicodedata.normalize('NFC',path).casefold()
        if key in seen:raise Conflict('duplicate or colliding archive path')
        if type(size) is not int or size<0 or (directory and size):raise Conflict('invalid archive entry size')
        total+=size
        if total>max_bytes or len(records)>=max_files:raise Conflict('source archive byte/file limit exceeded')
        seen[key]=(path,directory);records.append((path,directory,size,handle))
    spellings={}
    for path,directory,_,_ in records:
        parts=path.split('/')
        for i in range(1,len(parts)+1):
            spelling='/'.join(parts[:i]);folded=unicodedata.normalize('NFC',spelling).casefold()
            if folded in spellings and spellings[folded]!=spelling:raise Conflict('archive parent spelling collision')
            if folded not in spellings and len(spellings)>=max_files:raise Conflict('implicit source directory limit exceeded')
            spellings[folded]=spelling
    for key,(path,directory) in seen.items():
        parts=key.split('/')
        for i in range(1,len(parts)):
            parent='/'.join(parts[:i])
            if parent in seen:
                original,is_dir=seen[parent]
                if not is_dir:raise Conflict('archive file/directory name collision')
                if '/'.join(path.split('/')[:i])!=original:raise Conflict('archive parent spelling collision')
    if not any(not r[1] for r in records):raise Conflict('source archive must contain regular files')
    return records


def _zip_directory_bounds(path,max_files):
    """Bound central-directory parsing before ZipFile allocates entry objects."""
    size=path.stat().st_size
    with path.open('rb') as stream:
        stream.seek(max(0,size-65557));tail=stream.read(65557)
        position=tail.rfind(b'PK\x05\x06')
        if position<0 or position+22>len(tail):raise Conflict('ZIP directory missing')
        fields=struct.unpack('<4s4H2IH',tail[position:position+22])
        _,disk,start_disk,disk_entries,entries,directory_size,offset,comment=fields
        if (disk or start_disk or disk_entries!=entries or entries>max_files
                or entries==65535 or offset==0xffffffff or directory_size==0xffffffff
                or position+22+comment!=len(tail) or offset+directory_size!=size-(len(tail)-position)):
            raise Conflict('bounded single-disk ZIP directory required')
        stream.seek(offset);end=offset+directory_size;count=0
        while stream.tell()<end:
            header=stream.read(46)
            if len(header)!=46 or header[:4]!=b'PK\x01\x02':raise Conflict('invalid ZIP directory entry')
            name_length,extra_length,comment_length=struct.unpack('<3H',header[28:34])
            if not 1<=name_length<=512 or extra_length>1024 or comment_length>1024:raise Conflict('bounded ZIP entry metadata required')
            stream.seek(name_length+extra_length+comment_length,1);count+=1
            if count>max_files or stream.tell()>end:raise Conflict('ZIP entry count/metadata limit exceeded')
        if count!=entries:raise Conflict('ZIP directory count mismatch')


def _tree(source,max_bytes,max_files):
    if source.is_symlink() or not source.is_dir():raise Conflict('prepared source root changed')
    source=_physical(source)
    records=[];directories=[];total=0
    for path in sorted(source.rglob('*')):
        if len(records)+len(directories)>=max_files:raise Conflict('prepared source entry limit changed')
        if path.is_symlink():raise Conflict('prepared source link detected')
        relative=path.relative_to(source).as_posix()
        if path.is_dir():directories.append({'path':relative,'mode':stat.S_IMODE(path.stat().st_mode)});continue
        if not path.is_file():raise Conflict('prepared source special file detected')
        size=path.stat().st_size;total+=size
        if total>max_bytes or len(records)>=max_files:raise Conflict('prepared source bounds changed')
        records.append({'path':relative,'bytes':size,'sha256':_hash(path),'mode':stat.S_IMODE(path.stat().st_mode)})
    return {'files':records,'directories':directories,'file_count':len(records),'bytes':total,'tree_digest':digest({'files':records,'directories':directories})}


def _copy(stream,path,size):
    h=hashlib.sha256();copied=0
    with path.open('xb') as output:
        while True:
            chunk=stream.read(min(1024*1024,size-copied+1))
            if not chunk:break
            copied+=len(chunk)
            if copied>size:raise Conflict('archive entry exceeded declared size')
            output.write(chunk);h.update(chunk)
    if copied!=size:raise Conflict('truncated source archive entry')
    os.chmod(path,0o400)
    return h.hexdigest()


def prepare_source_snapshot(snapshot,archive_path,owned_root,*,max_bytes=MAX_BYTES,max_files=MAX_FILES):
    """Publish an immutable private project/version source tree from exact bytes.

    Tar, gzip-compressed tar and ZIP are supported. Names are preserved; provider
    wrapper directories are never silently stripped. Git provenance remains a
    trusted provider assertion, independently pinned by actual archive/tree hashes.
    """
    if not isinstance(snapshot,SourceSnapshot):raise ValueError('typed trusted source snapshot required')
    if type(max_bytes) is not int or not 1<=max_bytes<=MAX_BYTES or type(max_files) is not int or not 1<=max_files<=MAX_FILES:raise ValueError('bounded source archive limits required')
    archive_path=_physical(archive_path)
    if not archive_path.is_file() or archive_path.stat().st_size>max_bytes:raise Conflict('bounded physical source archive required')
    root=_private_directory(owned_root)
    if archive_path==root or root in archive_path.parents:raise ValueError('archive input must remain separate from handoff root')
    project_root=_private_directory(root/snapshot.project)
    identity=asdict(snapshot);key=digest(identity);target=project_root/key
    lock=project_root/'.handoff-lock'
    try:descriptor=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError as exc:raise Conflict('project source handoff already in progress') from exc
    os.close(descriptor);stage=None
    try:
        stage=Path(tempfile.mkdtemp(prefix='.handoff-stage-',dir=project_root));source=stage/'source';source.mkdir(mode=0o700)
        # Copy and hash the input first. Subsequent validation/extraction only uses
        # this private frozen byte stream, preventing input-path replacement races.
        frozen=stage/'archive.bin';actual=hashlib.sha256();copied=0
        with archive_path.open('rb') as stream,frozen.open('xb') as output:
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                copied+=len(chunk)
                if copied>max_bytes:raise Conflict('source archive byte limit exceeded')
                actual.update(chunk);output.write(chunk)
        if actual.hexdigest()!=snapshot.archive_digest:raise Conflict('source archive digest differs')
        archive_format='zip' if zipfile.is_zipfile(frozen) else 'tar'
        tar_path=frozen
        with frozen.open('rb') as stream:magic=stream.read(2)
        if archive_format=='tar' and magic==bytes((31,139)):
            tar_path=stage/'expanded.tar';expanded=0
            with gzip.open(frozen,'rb') as stream,tar_path.open('xb') as output:
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    expanded+=len(chunk)
                    if expanded>max_bytes:raise Conflict('expanded source tar limit exceeded')
                    output.write(chunk)
            archive_format='tar.gz'
        if archive_format=='zip':
            _zip_directory_bounds(frozen,max_files)
            with zipfile.ZipFile(frozen) as bundle:
                entries=[]
                for member in bundle.infolist():
                    mode=(member.external_attr>>16)&0xffff;kind=stat.S_IFMT(mode)
                    if member.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):raise Conflict('unsupported ZIP compression')
                    if member.flag_bits&1 or kind not in (0,stat.S_IFREG,stat.S_IFDIR):raise Conflict('encrypted/link/special ZIP entry rejected')
                    entries.append((member.filename,member.is_dir(),member.file_size,member))
                records=_records(entries,max_bytes,max_files)
                _extract_records(records,source,lambda member:bundle.open(member,'r'))
        else:
            with tarfile.open(tar_path,'r:') as bundle:
                entries=[];declared=0
                for member in bundle:
                    if member.type not in (tarfile.REGTYPE,tarfile.AREGTYPE,tarfile.DIRTYPE) or member.sparse is not None:raise Conflict('link/special tar entry rejected')
                    entries.append((member.name,member.isdir(),member.size,member))
                    declared+=member.size
                    if len(entries)>max_files or declared>max_bytes:raise Conflict('source tar bounds exceeded')
                records=_records(entries,max_bytes,max_files)
                _extract_records(records,source,bundle.extractfile)
        for path in sorted((p for p in source.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):os.chmod(path,0o500)
        os.chmod(source,0o500)
        tree=_tree(source,max_bytes,max_files)
        manifest={'schema_version':'source-archive-handoff/v1','snapshot':identity,'archive_format':archive_format,
                  'archive_bytes':copied,'source_root':str(target/'source'),'manifest_path':str(target/'source-manifest.json'),
                  'source_form':'provider_archive','actual_git_clone':False,'git_origin_verified':False,
                  'provider_revision_independently_verified':False,'tree':tree,'hooks_executed':False,
                  'customer_code_executed':False,'sourcewatch_registered':False,'worker_ready':False,'approval_granted':False}
        frozen.unlink()
        if tar_path!=frozen:tar_path.unlink()
        manifest_path=stage/'source-manifest.json';manifest_path.write_text(canonical_json(manifest)+'\n');os.chmod(manifest_path,0o400)
        os.chmod(stage,0o700)
        if target.exists():
            target=_physical(target);stored_path=target/'source-manifest.json'
            if (not stored_path.is_file() or stored_path.is_symlink() or stored_path.stat().st_size>32*1024*1024
                    or {p.name for p in target.iterdir()}!={'source','source-manifest.json'}):
                raise Conflict('immutable source handoff manifest unavailable')
            if json.loads(stored_path.read_text())!=manifest or _tree(target/'source',max_bytes,max_files)!=tree:
                raise Conflict('immutable source handoff changed')
            return manifest
        stage.rename(target);stage=None
        return manifest
    except (tarfile.TarError,zipfile.BadZipFile,EOFError) as exc:
        raise Conflict('invalid source archive') from exc
    finally:
        if stage is not None:
            for path in [stage,*stage.rglob('*')]:
                if path.is_dir() and not path.is_symlink():os.chmod(path,0o700)
            shutil.rmtree(stage)
        lock.unlink()


def _extract_records(records,source,open_member):
    for relative,directory,size,member in sorted(records,key=lambda r:(len(PurePosixPath(r[0]).parts),r[0])):
        path=source/relative
        if directory:path.mkdir(parents=True,exist_ok=True,mode=0o700);continue
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with open_member(member) as stream:_copy(stream,path,size)
