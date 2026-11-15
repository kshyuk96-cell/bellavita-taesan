"""Fail-closed startup for a separately reviewed manager host.

This module never bootstraps accounts, creates services, changes plans/disks, or
starts workers. Existing private state is checked read-only before server startup.
A declared persistent mount path is checked; restart persistence remains a host test.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
from urllib.parse import quote, urlsplit

from .control import secret
from .maintenance_registry import configuration
from .store import canonical_json, digest

PROJECTS = ('bellavita-erp', 'bellavita-shop', 'funeral-association', 'hope-sunrise')
DATABASES = ('maintenance.sqlite3', 'manager.sqlite3')


class HostRejected(ValueError):
    pass


def _path(value):
    path = Path(value).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise HostRejected('host path rejected')
    return path


def _private_file(path):
    path = _path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HostRejected('private bootstrap file required')
    return path


def _origin(value):
    if type(value) is not str:
        raise HostRejected('explicit HTTPS origin required')
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise HostRejected('invalid public origin')
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise HostRejected('invalid public origin') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or port is not None and not 1 <= port <= 65535):
        raise HostRejected('explicit HTTPS origin required')
    return value.rstrip('/')


def _source(root, expected):
    root = _path(root)
    if type(expected) is not str or not re.fullmatch(r'[a-f0-9]{40}', expected) or not root.is_dir():
        raise HostRejected('exact source commit required')
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_ALLOW_PROTOCOL': '', 'GIT_NO_LAZY_FETCH': '1'}
    prefix = ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
              '-c', 'core.untrackedCache=false', '-c', 'credential.helper=', '-C', str(root)]
    def git(*args):
        result = subprocess.run(prefix + list(args), capture_output=True, text=True,
                                env=env, timeout=10)
        if result.returncode or len(result.stdout) > 1024*1024:
            raise HostRejected('source observation unavailable')
        return result.stdout.strip()
    if git('rev-parse', '--show-toplevel') != str(root) or git('rev-parse', 'HEAD') != expected:
        raise HostRejected('source commit mismatch')
    if git('status', '--porcelain', '--untracked-files=all', '--ignore-submodules=all'):
        raise HostRejected('source checkout must be clean')


def prepare_host(data_dir, *, expected_commit, public_origin, source_root,
                 persistent_root='/var/data', database='maintenance.sqlite3',
                 projects=PROJECTS, clock=time.time):
    """Return metadata only. Do not create/copy state or start a listener.

    database is an explicit compatibility choice, never an automatic fallback.
    projects is trusted deployment configuration, not an HTTP request parameter.
    """
    origin = _origin(public_origin)
    _source(source_root, expected_commit)
    data = _path(data_dir); persistent = _path(persistent_root)
    if not persistent.is_dir() or not data.is_dir() or not data.is_relative_to(persistent):
        raise HostRejected('existing state under declared persistent root required')
    info = data.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HostRejected('private data directory required')
    if database not in DATABASES:
        raise HostRejected('explicit supported database name required')
    if type(projects) not in (tuple, list) or not projects or len(set(projects)) != len(projects):
        raise HostRejected('explicit project scope required')
    db_path = _private_file(data / database)
    owner_file = _private_file(data / 'owner.token')
    manager_file = _private_file(data / 'manager.token')
    credentials = {'owner': secret(owner_file), 'manager': secret(manager_file)}
    now = clock()
    if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        raise HostRejected('invalid host clock')
    # Read-only open prevents validation from creating tables, owners, or sidecar data.
    connection = sqlite3.connect('file:' + quote(str(db_path), safe='/') + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'bridge_projects', 'bridge_tokens', 'bridge_web_sessions', 'maintenance_projects'} <= tables:
            raise HostRejected('complete bootstrap schema required')
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise HostRejected('bootstrap database integrity required')
        for role, raw in credentials.items():
            hashed = hashlib.sha256(raw.encode()).hexdigest()
            row = connection.execute('SELECT * FROM bridge_tokens WHERE token_hash=?', (hashed,)).fetchone()
            if (not row or row['role'] != role or row['revoked'] is not None
                    or row['expires'] is not None and (not math.isfinite(row['expires']) or row['expires'] <= now)):
                raise HostRejected('active owner and manager profiles required')
            scopes = json.loads(row['projects'])
            if type(scopes) is not list or not scopes or any(type(p) is not str for p in scopes) or len(set(scopes)) != len(scopes):
                raise HostRejected('explicit profile scope list required')
            if role == 'owner':
                if scopes != ['*'] and not set(projects) <= set(scopes):
                    raise HostRejected('owner scope mismatch')
            elif set(scopes) != set(projects):
                raise HostRejected('exact manager project scope required')
            if connection.execute('SELECT 1 FROM bridge_web_sessions WHERE token_hash=?', (hashed,)).fetchone():
                raise HostRejected('bootstrap must use parent profiles, not browser sessions')
        for project in projects:
            row = connection.execute('SELECT * FROM maintenance_projects WHERE project=?', (project,)).fetchone()
            if row is None:
                raise HostRejected('maintenance project missing')
            config = configuration(json.loads(row['config']))
            if row['fingerprint'] != digest({'project': project, 'config': config, 'revision': row['revision']}):
                raise HostRejected('project configuration fingerprint mismatch')
            repository = config['source_ref'] if config['source_kind'] == 'github' else 'sites/' + config['source_ref']
            binding = connection.execute('SELECT repository FROM bridge_projects WHERE project=?', (project,)).fetchone()
            if not binding or binding['repository'] != repository:
                raise HostRejected('source binding mismatch')
            if config['maintenance_enabled']:
                raise HostRejected('review host must not activate project execution')
    finally:
        connection.close()
    return {'source_commit': expected_commit, 'database': database, 'projects': sorted(projects),
            'public_origin': origin, 'bootstrap_verified': True, 'source_verified': True,
            'persistent_path_verified': True, 'restart_persistence_verified': False,
            'execution_enabled': False, 'operational_readiness': False,
            'accounts_generated': False, 'services_created': False}


def open_host(data_dir, *, host='127.0.0.1', port=8791, **kwargs):
    """Explicit listener creation after all read-only gates pass; no serve loop."""
    plan = prepare_host(data_dir, **kwargs)
    from .store import Store
    from .server import make_server
    server = make_server(Store(_path(data_dir) / plan['database']), host, port,
                         public_origin=plan['public_origin'])
    server.manager_host_plan = plan
    # No source observers/transports/worker executors are installed by startup.
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='태산이 관리 호스트 검토/명시적 시작')
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--persistent-root', type=Path, default=Path('/var/data'))
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--public-origin', required=True)
    parser.add_argument('--database', choices=DATABASES, default='maintenance.sqlite3')
    parser.add_argument('--host', choices=('127.0.0.1','::1','localhost','0.0.0.0'), default='127.0.0.1')
    parser.add_argument('--port', type=int)
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args(argv)
    try:
        port=args.port if args.port is not None else int(os.environ.get('PORT','8791'))
        if not 0 <= port <= 65535:
            raise HostRejected('invalid listener port')
        kwargs = dict(expected_commit=args.expected_commit, public_origin=args.public_origin,
                      source_root=args.source_root, persistent_root=args.persistent_root,
                      database=args.database)
        if not args.serve:
            print(json.dumps(prepare_host(args.data, **kwargs), ensure_ascii=False))
            return 0
        server = open_host(args.data, host=args.host, port=port, **kwargs)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    except Exception:
        # Never echo paths, credential material, provider exceptions or SQLite rows.
        print(canonical_json({'ok': False, 'error': 'manager_host_not_ready',
                              'accounts_generated': False, 'services_created': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
