import json
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request,build_opener,HTTPCookieProcessor,ProxyHandler

from manager_bridge.sessions import Sessions
from manager_bridge.store import Store,PermissionDenied,token_hash
from manager_bridge.server import make_server


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now=1000
        self.store=Store(self.root/'state.db',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.store.register_project(self.owner,'a','fixture/a')
        self.manager=self.store.add_token('manager','manager',['a'])
        self.worker=self.store.add_token('worker','worker',['a'])
        self.sessions=Sessions(self.store)

    def test_one_time_code_clones_only_parent_scope_and_never_persists_raw_secrets(self):
        code=self.sessions.issue(self.manager)['code']
        session=self.sessions.redeem(code,'fixture-client')['token']
        actor=self.store.authenticate(session)
        self.assertEqual(actor['role'],'manager');self.assertEqual(actor['projects'],['a'])
        with self.assertRaises(PermissionDenied):self.sessions.redeem(code,'fixture-client')
        raw=(self.root/'state.db').read_bytes()
        for secret in (code,session,self.manager):self.assertNotIn(secret.encode(),raw)
        with self.assertRaises(PermissionDenied):self.sessions.issue(session)
        with self.assertRaises(PermissionDenied):self.sessions.issue(self.worker)

    def test_parent_revocation_expiry_and_logout_invalidate_web_session(self):
        session=self.sessions.redeem(self.sessions.issue(self.manager)['code'],'client')['token']
        self.store.revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(PermissionDenied):self.store.authenticate(session)
        owner_session=self.sessions.redeem(self.sessions.issue(self.owner)['code'],'client')['token']
        self.sessions.logout(owner_session)
        with self.assertRaises(PermissionDenied):self.store.authenticate(owner_session)
        with self.assertRaises(PermissionDenied):self.sessions.logout(self.owner)

    def test_expired_and_throttled_codes_are_rejected_and_count_is_durable(self):
        code=self.sessions.issue(self.manager)['code'];self.now+=121
        with self.assertRaises(PermissionDenied):self.sessions.redeem(code,'client')
        valid=self.sessions.issue(self.manager)['code']
        for _ in range(5):
            with self.assertRaises(PermissionDenied):self.sessions.redeem('invalid','brute-client')
        restarted=Sessions(Store(self.root/'state.db',clock=lambda:self.now))
        with self.assertRaises(PermissionDenied):restarted.redeem(valid,'brute-client')
        self.now+=61
        session=restarted.redeem(valid,'brute-client')['token']
        self.now+=901
        with self.assertRaises(PermissionDenied):self.store.authenticate(session)

    def test_cookie_cannot_be_a_root_bearer_and_bad_post_origin_is_rejected(self):
        with self.assertRaises(PermissionDenied):self.sessions.cookie_token(self.owner)
        server=make_server(self.store,port=0)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base='http://127.0.0.1:'+str(server.server_address[1])
        jar=CookieJar();client=build_opener(ProxyHandler({}),HTTPCookieProcessor(jar))
        code=self.sessions.issue(self.manager)['code']
        def post(path,body,headers=None):
            return client.open(Request(base+path,data=json.dumps(body).encode(),method='POST',headers={
                'Content-Type':'application/json','X-Taesan-Client':'dashboard',**(headers or {})}))
        with post('/v1/auth/redeem',{'code':code}) as response:
            result=json.load(response);cookie=response.headers['Set-Cookie']
        self.assertTrue(result['ok']);self.assertNotIn('token',result)
        self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
        with client.open(base+'/v1/projects') as response:self.assertEqual(json.load(response)['projects'][0]['project'],'a')
        with self.assertRaises(HTTPError) as error:post('/v1/auth/logout',{}, {'Origin':'https://attacker.invalid'})
        self.assertEqual(error.exception.code,403)
        with client.open(base+'/v1/projects') as response:self.assertTrue(json.load(response)['ok'])
        with post('/v1/auth/logout',{}) as response:self.assertTrue(json.load(response)['logged_out'])
        with self.assertRaises(HTTPError):client.open(base+'/v1/projects')


if __name__=='__main__':unittest.main()
