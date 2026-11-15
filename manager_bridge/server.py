"""Independent manager API. No business server imports, tool execution or schedules."""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .store import Store
from .coordination import Coordination
from .workbench import Workbench
from .trials import TrialRunner
from .sessions import Sessions
from .workflow import Workflow
from .operations import Operations
from .connections import Connections
from .release_preflight import ReleasePreflight


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

    def respond(self, status, value, *, cookie=None):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        if cookie is not None:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def _browser_request(self):
        if self.headers.get('X-Taesan-Client')!='dashboard':
            raise PermissionError('browser client required')
        if self.headers.get('Sec-Fetch-Site')=='cross-site':
            raise PermissionError('cross-site request rejected')
        origin=self.headers.get('Origin')
        allowed=getattr(self.server,'public_origin',None) or 'http://'+self.headers.get('Host','')
        if origin and origin!=allowed:
            raise PermissionError('origin mismatch')

    def _cookie_value(self):
        from http.cookies import SimpleCookie
        parsed=SimpleCookie();parsed.load(self.headers.get('Cookie',''))
        return parsed['taesan_session'].value if 'taesan_session' in parsed else None

    def _session_cookie(self,value,age):
        secure='; Secure' if str(getattr(self.server,'public_origin','')).startswith('https://') else ''
        return 'taesan_session='+value+'; HttpOnly; SameSite=Strict; Path=/; Max-Age='+str(age)+secure

    def token(self):
        value=self.headers.get('Authorization','')
        if value:
            if not value.startswith('Bearer ') or len(value)>1024:
                raise PermissionError('authentication required')
            token=value[7:]
            self.server.store.authenticate(token)
            return token
        raw=self._cookie_value()
        if raw:
            if self.command=='POST':
                self._browser_request()
            return Sessions(self.server.store).cookie_token(raw)
        raise PermissionError('authentication required')

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
        static = {'/dashboard': ('index.html', 'text/html; charset=utf-8'),
                  '/dashboard.js': ('dashboard.js', 'application/javascript; charset=utf-8'),
                  '/dashboard.css': ('dashboard.css', 'text/css; charset=utf-8')}
        if self.path in static:
            from pathlib import Path
            name, mime = static[self.path]
            payload = (Path(__file__).parent / 'ui' / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == '/healthz':
            return self.respond(200, {'status': 'ok'})
        self.handle_api('GET')

    def do_POST(self):
        self.handle_api('POST')

    def handle_api(self, method):
        try:
            store = self.server.store
            parsed = urlsplit(self.path)
            if method=='POST' and self.path in ('/v1/auth/redeem','/v1/auth/logout'):
                self._browser_request()
                data=self.body()
                if self.path.endswith('/redeem'):
                    session=Sessions(store).redeem(data.get('code'),self.client_address[0])
                    return self.respond(200,{'ok':True,'expires_at':session['expires_at']},
                        cookie=self._session_cookie(session['token'],900))
                raw=self._cookie_value()
                if raw:
                    try:
                        Sessions(store).logout(raw)
                    except PermissionError:
                        pass
                return self.respond(200,{'ok':True,'logged_out':True},cookie=self._session_cookie('',0))
            token = self.token()
            if parsed.path.startswith('/v1/representative/'):
                from .representative_api import dispatch
                data=self.body() if method=='POST' else None
                if dispatch(self,method,self.path,token,data):
                    return
                return self.respond(404,{'ok':False,'error':'not_found'})
            if method == 'GET' and parsed.path in ('/v1/tasks', '/v1/overview', '/v1/scorecard', '/v1/dashboard', '/v1/trials', '/v1/digest', '/v1/connections'):
                query = parse_qs(parsed.query, strict_parsing=True)
                if set(query) != {'project'} or len(query['project']) != 1:
                    raise ValueError('project required')
                if parsed.path == '/v1/connections':
                    return self.respond(200,{'ok':True,'connections':Connections(store).latest(token,query['project'][0])})
                if parsed.path == '/v1/digest':
                    return self.respond(200, {'ok': True, 'digest': Workflow(store).preview_digest(token,query['project'][0])})
                if parsed.path == '/v1/trials':
                    return self.respond(200, {'ok': True, 'trials': TrialRunner(store).status(token, query['project'][0])})
                if parsed.path == '/v1/dashboard':
                    return self.respond(200, {'ok': True, 'dashboard': Workbench(store).dashboard(token, query['project'][0])})
                if parsed.path == '/v1/scorecard':
                    return self.respond(200, {'ok': True, 'scorecard': Coordination(store).scorecard(token, query['project'][0])})
                if parsed.path == '/v1/overview':
                    return self.respond(200, {'ok': True, 'overview': store.overview(token, query['project'][0])})
                return self.respond(200, {'ok': True, 'tasks': store.list_tasks(token, query['project'][0])})
            if method == 'GET' and parsed.path == '/v1/projects' and not parsed.query:
                return self.respond(200, {'ok': True, 'projects': Workbench(store).projects(token)})
            detail_match=re.fullmatch(r'/v1/tasks/([a-f0-9]{32})/(timings|review-diff|preflight)',parsed.path)
            if method=='GET' and detail_match:
                task_id,kind=detail_match.groups()
                if kind=='review-diff':
                    query=parse_qs(parsed.query,strict_parsing=True)
                    if set(query)!={'from_revision'} or len(query['from_revision'])!=1:raise ValueError('revision required')
                    result=Workbench(store).card_diff(token,task_id,int(query['from_revision'][0]))
                elif parsed.query:
                    raise ValueError('query not supported')
                elif kind=='timings':result=Operations(store).timings(token,task_id)
                else:result=ReleasePreflight(store).latest(token,task_id)
                return self.respond(200,{'ok':True,'detail':result})
            card_match = re.fullmatch(r'/v1/tasks/([a-f0-9]{32})/review-card', parsed.path)
            if method == 'GET' and card_match and not parsed.query:
                return self.respond(200, {'ok': True, 'card': Workbench(store).card(token, card_match[1])})
            match = re.fullmatch(r'/v1/tasks/([a-f0-9]{24,64})', parsed.path)
            if method == 'GET' and match and not parsed.query:
                return self.respond(200, {'ok': True, 'task': store.get_task(token, match[1])})
            if method != 'POST' or parsed.query:
                return self.respond(404, {'ok': False, 'error': 'not_found'})
            data = self.body()
            if parsed.path=='/v1/capacity':
                return self.respond(200,{'ok':True,'capacity':Operations(store).set_capacity(token,data.get('project'),
                    max_active=data['max_active'],max_review=data['max_review'])})
            if parsed.path=='/v1/connections/inspect':
                return self.respond(200,{'ok':True,'connections':Connections(store,getattr(self.server,'connection_probes',{})).inspect(token,data['project'])})
            if parsed.path=='/v1/digest/acknowledge':
                return self.respond(200,{'ok':True,'receipt':Workflow(store).acknowledge_digest(token,data['project'],data['fingerprint'])})
            workflow_match=re.fullmatch(r'/v1/tasks/([a-f0-9]{32})/(planning|change)',parsed.path)
            if workflow_match:
                workflow=Workflow(store)
                if workflow_match[2]=='planning':
                    result=workflow.plan(token,workflow_match[1],priority=data['priority'],due_at=data['due_at'])
                else:
                    result=workflow.propose_change(token,workflow_match[1],request_key=data['request_key'],
                        manifest=data['manifest'],reason=data['reason'])
                return self.respond(200,{'ok':True,'workflow':result})
            card_match = re.fullmatch(r'/v1/tasks/([a-f0-9]{32})/review-card(/acknowledge)?', parsed.path)
            if card_match:
                workbench=Workbench(store)
                if card_match[2]:
                    result=workbench.acknowledge(token,card_match[1],data['revision'],data['package_digest'])
                else:
                    result=workbench.put_package(token,card_match[1],data)
                return self.respond(200, {'ok':True,'review_card':result})
            if parsed.path == '/v1/tasks':
                result = store.submit(token, data['request_key'], data['manifest'])
            elif parsed.path == '/v1/worker/claim':
                result = store.claim(token, data['project'], lease_seconds=data.get('lease_seconds', 300), task_id=data.get('task_id'))
            elif parsed.path == '/v1/worker/heartbeat':
                result = store.heartbeat(token, data['id'], data['lease_token'],
                                         lease_seconds=data.get('lease_seconds', 300))
                return self.respond(200, {'ok': True, 'heartbeat': result})
            elif parsed.path == '/v1/worker/report':
                result = store.report(token, data['id'], data['lease_token'], data['report'])
            elif parsed.path == '/v1/manager/readiness':
                result = Coordination(store).readiness(token, data['worker_hash'], ready=data['ready'],
                                                       reason=data['reason'], ttl=data.get('ttl', 60))
                return self.respond(200, {'ok': True, 'readiness': result})
            else:
                match = re.fullmatch(r'/v1/tasks/([a-f0-9]{24,64})/(approve|review|cancel|policy|route|failure|evaluation|stopped)', parsed.path)
                if not match:
                    return self.respond(404, {'ok': False, 'error': 'not_found'})
                task, action = match.groups()
                if action == 'policy':
                    result = Coordination(store).set_policy(token, task, data['version'], data['digest'],
                        allowed_paths=data['allowed_paths'], dependencies=data['dependencies'])
                    return self.respond(200, {'ok': True, 'policy': result})
                elif action == 'route':
                    return self.respond(200, {'ok': True, 'route': Coordination(store).route(token, task)})
                elif action == 'failure':
                    return self.respond(200, {'ok': True, 'failure': Coordination(store).record_failure(token, task, data['code'])})
                elif action == 'evaluation':
                    result = Coordination(store).record_evaluation(token, task, case_id=data['case_id'],
                        dimension=data['dimension'], source=data['source'], passed=data['passed'],
                        critical=data['critical'], evidence=data['evidence'])
                    return self.respond(200, {'ok': True, 'evaluation': result})
                elif action == 'stopped':
                    result = Coordination(store).confirm_stopped(token, task, data['version'], data['digest'], data['evidence_reference'])
                    return self.respond(200, {'ok': True, 'stopped': result})
                elif action == 'approve':
                    result = store.approve(token, task, data['version'], data['digest'], policy_digest=data.get('policy_digest'))
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


def make_server(store, host='127.0.0.1', port=8791, *, public_origin=None, connection_probes=None):
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store
    server.public_origin = public_origin
    server.connection_probes = dict(connection_probes or {})
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
    server = make_server(Store(path), args.host, args.port, public_origin=args.public_origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
