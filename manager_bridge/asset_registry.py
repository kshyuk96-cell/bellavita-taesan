"""Immutable, project-scoped approved assets; no path-read or rendering API.

Content is supplied as bounded bytes. The cache never fetches a URL, substitutes
another image, executes an engine, or declares an unknown original approved.
The caller must display/verify an asset before the owner's exact-hash approval.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat

from .store import Conflict, NotFound


MAX_ASSET_BYTES = 8 * 1024 * 1024
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
_SHA256 = re.compile(r'[a-f0-9]{64}')
_KINDS = frozenset(('image', 'template', 'engine', 'font'))


class AssetUnavailable(ValueError):
    """Safe fixed-category asset failure; contents and host paths stay private."""


def _identity(asset_id, version):
    if any(not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
           for value in (asset_id, version)):
        raise ValueError('에셋 ID와 버전을 영문·숫자 식별값으로 지정해 주세요.')


def _expected(value):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError('정확한 SHA256 식별값이 필요해요.')


class AssetRegistry:
    def __init__(self, store, root):
        self.store = store
        self.root = Path(root).absolute()
        if any(parent.is_symlink() for parent in (self.root, *self.root.parents)):
            raise AssetUnavailable('에셋 캐시의 심볼릭 링크 경로는 사용할 수 없어요.')
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        with self._directory():
            pass
        with store._transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS bridge_assets (
                project TEXT NOT NULL,asset_id TEXT NOT NULL,version TEXT NOT NULL,
                sha256 TEXT NOT NULL,kind TEXT NOT NULL,size INTEGER NOT NULL,
                status TEXT NOT NULL,created REAL NOT NULL,approved_actor TEXT,approved_at REAL,
                PRIMARY KEY(project,asset_id,version))''')

    @contextmanager
    def _directory(self):
        descriptor = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open('/', flags)
            for component in self.root.parts[1:]:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise AssetUnavailable('에셋 캐시는 소유자 전용 0700 폴더여야 해요.')
            yield descriptor
        except OSError:
            raise AssetUnavailable('에셋 캐시 위치를 확인하지 못했어요.') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _read(self, directory, sha256, expected_size=None):
        descriptor = None
        try:
            descriptor = os.open(sha256, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or not 1 <= info.st_size <= MAX_ASSET_BYTES
                    or (expected_size is not None and info.st_size != expected_size)):
                raise AssetUnavailable('에셋 파일 형식·크기·권한이 일치하지 않아요.')
            chunks, size = [], 0
            while True:
                chunk = os.read(descriptor, min(65536, MAX_ASSET_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_ASSET_BYTES:
                    raise AssetUnavailable('에셋 내용 크기 한도를 초과했어요.')
            content = b''.join(chunks)
            if len(content) != info.st_size or hashlib.sha256(content).hexdigest() != sha256:
                raise AssetUnavailable('에셋 원본이 변경돼 사용을 중지했어요.')
            return content
        except OSError:
            raise AssetUnavailable('지정한 에셋이 없거나 안전하게 읽을 수 없어요.') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _cache(self, directory, sha256, content):
        try:
            descriptor = os.open(sha256, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
        except FileExistsError:
            self._read(directory, sha256, len(content))
            return
        except OSError:
            raise AssetUnavailable('에셋 캐시를 생성하지 못했어요.') from None
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            # Leave the failed hash entry unavailable; never silently overwrite it.
            raise AssetUnavailable('에셋 캐시 저장을 확인하지 못했어요.') from None
        try:
            os.fsync(directory)
        except OSError:
            raise AssetUnavailable('에셋 캐시 보존 상태를 확인하지 못했어요.') from None

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ('project', 'asset_id', 'version', 'sha256', 'kind', 'size',
                                          'status', 'created', 'approved_actor', 'approved_at')}

    def register_draft(self, token, project, asset_id, version, content, kind='image'):
        _identity(asset_id, version)
        if (type(content) is not bytes or not 1 <= len(content) <= MAX_ASSET_BYTES
                or not isinstance(kind, str) or kind not in _KINDS):
            raise ValueError('허용된 종류와 1바이트~8MiB의 에셋 bytes가 필요해요.')
        sha256 = hashlib.sha256(content).hexdigest()
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            self.store._project_access(c, actor, project)
            if token.encode() in content or token in asset_id or token in version:
                raise ValueError('인증 정보를 에셋에 넣을 수 없어요.')
            old = c.execute('SELECT * FROM bridge_assets WHERE project=? AND asset_id=? AND version=?',
                            (project, asset_id, version)).fetchone()
            if old and (old['sha256'] != sha256 or old['kind'] != kind or old['size'] != len(content)):
                raise Conflict('같은 에셋 버전을 다른 원본으로 덮어쓰지 않았어요.')
            with self._directory() as directory:
                if old:
                    self._read(directory, sha256, len(content))
                else:
                    self._cache(directory, sha256, content)
            if old:
                return self._public(old)
            c.execute('INSERT INTO bridge_assets VALUES (?,?,?,?,?,?,\'draft\',?,NULL,NULL)',
                      (project, asset_id, version, sha256, kind, len(content), self.store._now()))
            return self._public(c.execute('SELECT * FROM bridge_assets WHERE project=? AND asset_id=? AND version=?',
                                         (project, asset_id, version)).fetchone())

    def approve(self, token, project, asset_id, version, expected_sha256):
        _identity(asset_id, version)
        _expected(expected_sha256)
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner',))
            self.store._project_access(c, actor, project)
            row = c.execute('SELECT * FROM bridge_assets WHERE project=? AND asset_id=? AND version=?',
                            (project, asset_id, version)).fetchone()
            if not row:
                raise NotFound('지정한 에셋 버전이 없어요.')
            if row['sha256'] != expected_sha256:
                raise Conflict('검토한 에셋 원본과 승인 SHA256이 달라요.')
            with self._directory() as directory:
                self._read(directory, expected_sha256, row['size'])
            if row['status'] != 'approved':
                c.execute("UPDATE bridge_assets SET status='approved',approved_actor=?,approved_at=? "
                          'WHERE project=? AND asset_id=? AND version=?',
                          (actor['subject'], self.store._now(), project, asset_id, version))
            return self._public(c.execute('SELECT * FROM bridge_assets WHERE project=? AND asset_id=? AND version=?',
                                         (project, asset_id, version)).fetchone())

    def resolve(self, token, project, asset_id, version, expected_sha256):
        _identity(asset_id, version)
        _expected(expected_sha256)
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager', 'worker'))
            self.store._project_access(c, actor, project)
            row = c.execute('SELECT * FROM bridge_assets WHERE project=? AND asset_id=? AND version=?',
                            (project, asset_id, version)).fetchone()
            if not row:
                raise NotFound('지정한 에셋 버전이 없어요. 다른 원본으로 대체하지 않았어요.')
            if row['status'] != 'approved' or row['sha256'] != expected_sha256:
                raise Conflict('승인된 정확한 에셋 버전과 SHA256이 필요해요.')
            with self._directory() as directory:
                content = self._read(directory, expected_sha256, row['size'])
            return dict(self._public(row), content=content)
