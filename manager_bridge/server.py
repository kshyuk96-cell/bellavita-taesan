"""Independent manager API. No business server imports, tool execution or schedules."""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .store import Store


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = 'TaesanManager/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *_):
        pass  # Never print task text, tokens, private evidence or request URLs.

    def respond(self, status, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def token(self):
        value = self.headers.get('Authorization', '')
        if not value.startswith('Bearer ') or len(value) > 1024:
            raise PermissionError('authentication required')
        token = value[7:]
        self.server.store.authenticate(token)
        return token

    def body(self):
        if self.headers.get_content_type() != 'application/json':
            raise ValueError('json required')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 131072:
            raise ValueError('invalid body length')
        result = json.loads(self.rfile.read(length), object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
        if not isinstance(result, dict):
            raise ValueError('object required')
        return result

    def do_GET(self):
        if self.path == '/healthz':
            return self.respond(200, {'status': 'ok'})
        self.handle_api('GET')

    def do_POST(self):
        self.handle_api('POST')

    def handle_api(self, method):
        try:
            token = self.token()
            store = self.server.store
            parsed = urlsplit(self.path)
            if method == 'GET' and parsed.path == '/v1/tasks':
                query = parse_qs(parsed.query, strict_parsing=True)
                if set(query) != {'project'} or len(query['project']) != 1:
                    raise ValueError('project required')
                return self.respond(200, {'ok': True, 'tasks': store.list_tasks(token, query['project'][0])})
            match = re.fullmatch(r'/v1/tasks/([a-f0-9]{24,64})', parsed.path)
            if method == 'GET' and match and not parsed.query:
                return self.respond(200, {'ok': True, 'task': store.get_task(token, match[1])})
            if method != 'POST' or parsed.query:
                return self.respond(404, {'ok': False, 'error': 'not_found'})
            data = self.body()
            if parsed.path == '/v1/tasks':
                result = store.submit(token, data['request_key'], data['manifest'])
            elif parsed.path == '/v1/worker/claim':
                result = store.claim(token, data['project'], lease_seconds=data.get('lease_seconds', 300))
            elif parsed.path == '/v1/worker/heartbeat':
                result = store.heartbeat(token, data['id'], data['lease_token'],
                                         lease_seconds=data.get('lease_seconds', 300))
                return self.respond(200, {'ok': True, 'heartbeat': result})
            elif parsed.path == '/v1/worker/report':
                result = store.report(token, data['id'], data['lease_token'], data['report'])
            else:
                match = re.fullmatch(r'/v1/tasks/([a-f0-9]{24,64})/(approve|review|cancel)', parsed.path)
                if not match:
                    return self.respond(404, {'ok': False, 'error': 'not_found'})
                task, action = match.groups()
                if action == 'approve':
                    result = store.approve(token, task, data['version'], data['digest'])
                elif action == 'review':
                    result = store.review(token, task, data['version'], data['digest'], data['evidence_reference'])
                else:
                    result = store.cancel(token, task)
            return self.respond(200, {'ok': True, 'task': result})
        except Exception as error:
            # Fixed categories only. Store and transport exception text is private.
            name = type(error).__name__
            if isinstance(error, PermissionError) or name == 'PermissionDenied':
                return self.respond(403, {'ok': False, 'error': 'forbidden'})
            if name == 'Conflict':
                return self.respond(409, {'ok': False, 'error': 'conflict'})
            if name == 'NotFound':
                return self.respond(404, {'ok': False, 'error': 'not_found'})
            if isinstance(error, (ValueError, TypeError, KeyError)):
                return self.respond(400, {'ok': False, 'error': 'invalid_request'})
            return self.respond(503, {'ok': False, 'error': 'unavailable'})


def make_server(store, host='127.0.0.1', port=8791):
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='태산이 독립 개발 작업 연결 서버')
    parser.add_argument('--data', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8791)
    parser.add_argument('--public-origin')
    args = parser.parse_args(argv)
    if args.host not in ('127.0.0.1', '::1', 'localhost'):
        origin = urlsplit(args.public_origin or '')
        if origin.scheme != 'https' or not origin.hostname or origin.username or origin.password:
            parser.error('외부 연결에는 HTTPS 역방향 프록시의 public-origin 설정이 필요합니다.')
    from pathlib import Path
    path = Path(args.data) / 'manager.sqlite3'
    if not path.is_file():
        parser.error('먼저 manager_bridge.control init으로 비공개 저장소를 준비해 주세요.')
    server = make_server(Store(path), args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
