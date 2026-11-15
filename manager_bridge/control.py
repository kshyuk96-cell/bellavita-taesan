"""Bootstrap private credentials and call the manager API without printing tokens."""
import argparse
import json
import os
import re
import stat
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def secret(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1024:
            raise ValueError('비공개 인증 파일 권한을 확인해 주세요.')
        value = os.read(descriptor, 1025).decode().strip()
        if len(value) < 32 or any(char.isspace() for char in value):
            raise ValueError('인증 파일 형식을 확인해 주세요.')
        return value
    finally:
        os.close(descriptor)


def write_secret(path, token):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        output.write(token + '\n')
        output.flush()
        os.fsync(output.fileno())


def validate_url(value):
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/') or
            not (parsed.scheme == 'https' or (parsed.scheme == 'http' and
                 parsed.hostname in ('127.0.0.1', '::1', 'localhost')))):
        raise ValueError('HTTPS 서버 주소 또는 로컬 주소를 지정해 주세요.')
    return value.rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def request(base, token, method, path, payload=None):
    base = validate_url(base)
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload is not None else None
    query = Request(base + path, data=data, method=method,
                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(query, timeout=20) as response:
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError('응답 범위를 초과했어요.')
            return json.loads(raw)
    except HTTPError as error:
        # Never print URLs, headers, provider messages or request content.
        return {'ok': False, 'error': 'request_rejected', 'status': error.code}
    except OSError:
        return {'ok': False, 'error': 'connection_unverified', 'retry_performed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description='태산이 작업 연결 관리')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--data', required=True, type=Path)
    init.add_argument('--project', required=True)
    init.add_argument('--repository', required=True)
    worker = commands.add_parser('issue-worker')
    worker.add_argument('--data', required=True, type=Path)
    worker.add_argument('--owner-token-file', required=True, type=Path)
    worker.add_argument('--project', required=True)
    worker.add_argument('--name', required=True)
    project = commands.add_parser('register-project')
    project.add_argument('--data', required=True, type=Path)
    project.add_argument('--owner-token-file', required=True, type=Path)
    project.add_argument('--project', required=True)
    project.add_argument('--repository', required=True)
    manager = commands.add_parser('issue-manager')
    manager.add_argument('--data', required=True, type=Path)
    manager.add_argument('--owner-token-file', required=True, type=Path)
    manager.add_argument('--projects', required=True)
    manager.add_argument('--name', required=True)
    for action in ('submit', 'approve', 'review', 'cancel', 'list', 'overview', 'get'):
        sub = commands.add_parser(action)
        sub.add_argument('--url', default='http://127.0.0.1:8791')
        sub.add_argument('--token-file', required=True, type=Path)
        if action == 'submit':
            sub.add_argument('--manifest', required=True, type=Path)
            sub.add_argument('--request-key', required=True)
        elif action in ('list', 'overview'):
            sub.add_argument('--project', required=True)
        else:
            sub.add_argument('--task', required=True)
        if action in ('approve', 'review'):
            sub.add_argument('--version', type=int, required=True)
            sub.add_argument('--digest', required=True)
        if action == 'approve':
            sub.add_argument('--policy-digest')
        if action == 'review':
            sub.add_argument('--evidence-reference', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            from .store import Store
            if (args.data / 'manager.sqlite3').exists():
                raise ValueError('이미 준비된 저장소는 덮어쓰지 않아요.')
            args.data.mkdir(parents=True, exist_ok=True, mode=0o700)
            if args.data.is_symlink() or args.data.stat().st_mode & 0o077:
                raise ValueError('저장소 폴더는 소유자 전용 권한이어야 해요.')
            store = Store(args.data / 'manager.sqlite3')
            owner = store.add_token('owner', 'owner', ['*'])
            write_secret(args.data / 'owner.token', owner)
            store.register_project(owner, args.project, args.repository)
            manager = store.add_token('taesan', 'manager', [args.project])
            write_secret(args.data / 'manager.token', manager)
            result = {'ok': True, 'initialized': True, 'project': args.project,
                      'owner_token_file': str(args.data / 'owner.token'),
                      'manager_token_file': str(args.data / 'manager.token')}
        elif args.command in ('issue-worker', 'issue-manager', 'register-project'):
            from .store import Store
            store = Store(args.data / 'manager.sqlite3')
            actor = store.authenticate(secret(args.owner_token_file))
            if actor['role'] != 'owner':
                raise PermissionError('owner registration required')
            if args.command == 'register-project':
                store.register_project(secret(args.owner_token_file), args.project, args.repository)
                result = {'ok': True, 'project': args.project, 'registered': True}
            else:
                if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', args.name):
                    raise ValueError('invalid connection name')
                role = 'manager' if args.command == 'issue-manager' else 'worker'
                projects = [part.strip() for part in args.projects.split(',')] if role == 'manager' else [args.project]
                if '*' not in actor['projects'] and not set(projects).issubset(actor['projects']):
                    raise PermissionError('owner project scope required')
                destination = args.data / (role + '-' + args.name + '.token')
                if os.path.lexists(destination):
                    raise ValueError('기존 연결 인증 파일은 덮어쓰지 않아요.')
                token = store.add_token(args.name, role, projects)
                write_secret(destination, token)
                result = {'ok': True, 'projects': projects, role + '_token_file': str(destination)}
        else:
            token = secret(args.token_file)
            action = args.command
            if action == 'submit':
                payload = {'request_key': args.request_key,
                           'manifest': json.loads(args.manifest.read_text())}
                path, method = '/v1/tasks', 'POST'
            elif action in ('list', 'overview'):
                path, method, payload = ('/v1/overview' if action == 'overview' else '/v1/tasks') + '?project=' + quote(args.project, safe=''), 'GET', None
            else:
                if not re.fullmatch(r'[a-f0-9]{24,64}', args.task):
                    raise ValueError('작업 식별값을 확인해 주세요.')
                path = '/v1/tasks/' + args.task
                payload = {}
                method = 'GET' if action == 'get' else 'POST'
                if action != 'get':
                    path += '/' + action
                if action in ('approve', 'review'):
                    payload.update(version=args.version, digest=args.digest)
                if action == 'approve' and args.policy_digest:
                    payload['policy_digest'] = args.policy_digest
                if action == 'review':
                    payload['evidence_reference'] = args.evidence_reference
                if method == 'GET':
                    payload = None
            result = request(args.url, token, method, path, payload)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result.get('ok') else 2
    except Exception:
        print(json.dumps({'ok': False, 'error': 'setup_or_request_failed'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
