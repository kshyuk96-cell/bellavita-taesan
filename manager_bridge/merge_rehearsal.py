"""Fixed-SHA merge-tree rehearsal on explicitly registered local repositories.

No checkout, index update, branch/ref update, commit, push, or deployment occurs.
Git creates virtual merge trees/blobs only in a temporary bare object database,
using the approved repository objects as read-only alternates. A successful rehearsal is not an integrated or deployed change.
Executable Git config and include files are refused before reading the commits.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import stat
import tempfile


_SHA = re.compile(r'[a-f0-9]{40}')
MAX_OUTPUT = 1_000_000
MAX_FILE_BYTES = 8_000_000
MAX_WORKTREE_BYTES = 16_000_000
MAX_WORKTREE_FILES = 2000
_DANGEROUS_CONFIG = (r'^(include\.|includeif\.|filter\.|merge\..*\.driver$|core\.sshcommand$|core\.gitproxy$|'
                     r'extensions\.partialclone$|remote\..*\.(promisor|partialclonefilter|vcs)$|protocol\.)')


class RehearsalError(RuntimeError):
    pass


class MergeRehearsal:
    def __init__(self, projects):
        if not isinstance(projects, dict):
            raise RehearsalError('등록된 로컬 프로젝트 연결이 필요해요.')
        self.projects = {name: dict(config) for name, config in projects.items() if isinstance(config, dict)}
        if len(self.projects) != len(projects):
            raise RehearsalError('로컬 프로젝트 등록 형식을 확인해 주세요.')

    def _git(self, repo, home, *args, accepted=(0,)):
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'XDG_CONFIG_HOME': str(home),
               'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
               'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1',
               'GIT_ALLOW_PROTOCOL': '', 'GIT_PROTOCOL_FROM_USER': '0'}
        command = ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                   '-c', 'core.untrackedCache=false', '-c', 'core.attributesFile=/dev/null',
                   '-c', 'credential.helper=', '-c', 'merge.renormalize=false',
                   '-c', 'protocol.allow=never', '-c', 'protocol.ext.allow=never', '-C', str(repo), *args]
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                        stdout=output, stderr=subprocess.DEVNULL,
                                        timeout=30, check=False)
                size = os.fstat(output.fileno()).st_size
                if size > MAX_OUTPUT:
                    raise RehearsalError('예행 검증 출력 한도를 초과했어요.')
                output.seek(0)
                data = output.read(MAX_OUTPUT + 1)
            if result.returncode not in accepted:
                raise RehearsalError('고정된 Git 예행 검증 결과를 확인하지 못했어요.')
            return result.returncode, data
        except (OSError, subprocess.TimeoutExpired):
            raise RehearsalError('Git 예행 검증 실행을 확인하지 못했어요.') from None

    def _file_digest(self, root_fd, name, remaining):
        """Hash beneath an already-open root; never follow a symlink target.

        Missing tracked files are legitimate dirty state. FIFOs, directories
        (including submodules), hard links and oversized reads fail closed.
        Contents are used only for hashing and never returned in a result.
        """
        parts = name.split('/')
        directory = os.dup(root_fd)
        file_fd = None
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            try:
                info = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return b'missing', 0
            if stat.S_ISLNK(info.st_mode):
                content = os.fsencode(os.readlink(parts[-1], dir_fd=directory))
                if len(content) > min(MAX_FILE_BYTES, remaining):
                    raise RehearsalError('작업폴더 파일 크기 한도를 초과했어요.')
                after = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                if (info.st_ino, info.st_mtime_ns, info.st_ctime_ns) != (after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
                    raise RehearsalError('파일 관측 중 링크가 바뀌었어요.')
                return b'symlink:' + hashlib.sha256(content).digest(), len(content)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > min(MAX_FILE_BYTES, remaining)):
                raise RehearsalError('작업폴더 파일 형식·링크·크기 범위를 확인해 주세요.')
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            opened = os.fstat(file_fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or opened.st_ino != info.st_ino or opened.st_dev != info.st_dev):
                raise RehearsalError('파일 관측 중 대상이 바뀌었어요.')
            fingerprint, total = hashlib.sha256(), 0
            while True:
                chunk = os.read(file_fd, min(65536, MAX_FILE_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > min(MAX_FILE_BYTES, remaining):
                    raise RehearsalError('작업폴더 파일 크기 한도를 초과했어요.')
                fingerprint.update(chunk)
            after = os.fstat(file_fd)
            if ((opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or total != opened.st_size):
                raise RehearsalError('파일 관측 중 내용이 바뀌었어요.')
            return b'file:' + str(stat.S_IMODE(after.st_mode)).encode() + b':' + fingerprint.digest(), total
        except FileNotFoundError:
            return b'missing', 0
        except OSError:
            raise RehearsalError('작업폴더 파일을 안전하게 관측하지 못했어요.') from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(directory)

    def _snapshot(self, repo, home):
        observations = []
        for args, accepted in ((('rev-parse', 'HEAD'), (0,)),
                (('symbolic-ref', '-q', 'HEAD'), (0, 1)),
                (('show-ref',), (0, 1)),
                (('ls-files', '--stage', '-z'), (0,)),
                (('status', '--porcelain=v1', '-z', '--untracked-files=all'), (0,))):
            code, raw = self._git(repo, home, *args, accepted=accepted)
            observations.append(str(code).encode() + b'\0' + raw)
        clean = not bool(observations[-1][2:])
        _, binary_diff = self._git(repo, home, 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--')
        observations.append(binary_diff)
        _, tracked = self._git(repo, home, 'ls-files', '--cached', '-z')
        # Include ignored new files as well; an exclusion rule must not conceal
        # mutation at an existing untracked path.
        _, untracked = self._git(repo, home, 'ls-files', '--others', '-z')
        names = sorted(set(name for name in (tracked + untracked).split(b'\0') if name))
        if len(names) > MAX_WORKTREE_FILES:
            raise RehearsalError('작업폴더 파일 수 관측 한도를 초과했어요.')
        root_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            consumed = 0
            for raw_name in names:
                try:
                    name = raw_name.decode('utf-8')
                except UnicodeError:
                    raise RehearsalError('작업폴더 파일명 형식을 확인하지 못했어요.') from None
                if (len(name) > 500 or name.startswith('/') or '\\' in name or
                        any(part in ('', '.', '..', '.git') for part in name.split('/')) or
                        any(ord(char) < 32 or ord(char) == 127 for char in name)):
                    raise RehearsalError('작업폴더 파일명 범위를 확인하지 못했어요.')
                fingerprint, size = self._file_digest(root_fd, name, MAX_WORKTREE_BYTES - consumed)
                consumed += size
                observations.append(raw_name + b'\0' + fingerprint)
        finally:
            os.close(root_fd)
        return hashlib.sha256(b'\0'.join(observations)).hexdigest(), clean

    def run(self, project, repository, base_sha, candidate_sha):
        configured = self.projects.get(project)
        if (not configured or configured.get('repository') != repository
                or not isinstance(configured.get('path'), (str, Path))
                or any(not isinstance(sha, str) or not _SHA.fullmatch(sha) for sha in (base_sha, candidate_sha))):
            raise RehearsalError('등록된 저장소와 전체 40자리 기준·후보 SHA가 필요해요.')
        supplied = Path(configured['path'])
        if not supplied.is_absolute() or supplied.is_symlink():
            raise RehearsalError('등록된 절대 경로 저장소가 필요해요.')
        try:
            repo = supplied.resolve(strict=True)
        except OSError:
            raise RehearsalError('등록된 저장소를 찾지 못했어요.') from None
        with tempfile.TemporaryDirectory(prefix='taesan-merge-rehearsal-') as folder:
            home = Path(folder)
            # --no-includes avoids opening arbitrary included config during the
            # check. Query both common and worktree files before merge-tree.
            for location in ('--local', '--worktree'):
                code, keys = self._git(repo, home, 'config', location, '--no-includes', '--name-only',
                                       '--get-regexp', _DANGEROUS_CONFIG, accepted=(0, 1))
                if code == 0 or keys:
                    raise RehearsalError('외부 도구·드라이버·include Git 설정이 있어 예행 검증하지 않았어요.')
                # A normal HTTPS/SSH origin may stay, but no transport is allowed
                # in any invocation. Refuse helper URLs before object lookup.
                _, urls = self._git(repo, home, 'config', location, '--no-includes', '--null',
                                    '--get-regexp', r'^remote\..*\.(url|pushurl)$', accepted=(0, 1))
                for record in urls.split(b'\0'):
                    if not record:
                        continue
                    _, separator, value = record.partition(b'\n')
                    if not separator or re.match(rb'^[A-Za-z0-9_+.-]+::', value.lstrip()):
                        raise RehearsalError('외부 원격 도구 URL은 예행 검증에 사용할 수 없어요.')
            _, top = self._git(repo, home, 'rev-parse', '--show-toplevel')
            if top.decode().strip() != str(repo):
                raise RehearsalError('등록된 저장소 루트와 실제 루트가 달라요.')
            for sha in (base_sha, candidate_sha):
                _, kind = self._git(repo, home, 'cat-file', '-t', sha)
                if kind.strip() != b'commit':
                    raise RehearsalError('기준과 후보는 실제 커밋이어야 해요.')
            before, clean = self._snapshot(repo, home)
            _, objects = self._git(repo, home, 'rev-parse', '--path-format=absolute', '--git-path', 'objects')
            object_path = objects.decode().strip()
            if not Path(object_path).is_absolute() or any(ord(char) < 32 for char in object_path):
                raise RehearsalError('원본 객체 저장소 경로를 확인하지 못했어요.')
            # Never inherit the source repository's merge config. Only object
            # reads use the source; all virtual merge writes are temporary.
            empty_template = home / 'empty-template'
            empty_template.mkdir()
            bare = home / 'rehearsal.git'
            self._git(home, home, 'init', '--bare', '--quiet', '--object-format=sha1',
                      '--template=' + str(empty_template), str(bare))
            (bare / 'objects' / 'info' / 'alternates').write_text(object_path + '\n')
            code, raw = self._git(bare, home, 'merge-tree', '--write-tree', '--messages', '--name-only', '-z',
                                  base_sha, candidate_sha, accepted=(0, 1))
            after, _ = self._snapshot(repo, home)
            if before != after:
                raise RehearsalError('예행 검증 중 작업폴더·인덱스·참조 상태가 바뀌었어요.')
            fields = raw.split(b'\0')
            if not fields or not _SHA.fullmatch(fields[0].decode('ascii', errors='replace')):
                raise RehearsalError('가상 통합 트리를 확인하지 못했어요.')
            conflicts = []
            for field in fields[1:]:
                if not field:
                    break  # The remaining sections are source-bearing messages.
                try:
                    name = field.decode('utf-8')
                except UnicodeError:
                    raise RehearsalError('충돌 파일명 형식을 확인하지 못했어요.') from None
                if (len(name) > 500 or name.startswith('/') or '\\' in name or
                        any(part in ('', '.', '..', '.git') for part in name.split('/')) or
                        any(ord(char) < 32 or ord(char) == 127 for char in name)):
                    raise RehearsalError('충돌 파일명 범위를 확인하지 못했어요.')
                conflicts.append(name)
                if len(conflicts) > 100:
                    raise RehearsalError('충돌 파일이 100개를 넘어 별도 검토가 필요해요.')
            if code == 0 and conflicts or code == 1 and not conflicts:
                raise RehearsalError('충돌 종료 상태와 파일 목록이 일치하지 않아요.')
            return {'project': project, 'repository': repository, 'base_sha': base_sha,
                    'candidate_sha': candidate_sha, 'status': 'conflicts' if code else 'clean_rehearsal',
                    'conflict_paths': sorted(set(conflicts)), 'worktree_clean_before': clean,
                    'worktree_index_refs_unchanged': True,
                    'snapshot_scope': 'refs_index_diff_and_tracked_all_untracked_bytes', 'git_objects_may_have_been_created': True,
                    'object_storage_scope': 'temporary_rehearsal_repository',
                    'merge_policy': 'builtin_default_no_source_config',
                    'source_object_database_modified': False,
                    'network_fetch_allowed': False,
                    'production_merge_performed': False, 'deployment_executed': False}
