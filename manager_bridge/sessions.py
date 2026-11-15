"""Short-lived one-time pairing codes; no Google OAuth or external identity changes.

Pairing/session values are returned once. Only their hashes are persisted. A web
session clones its issuing identity's exact scope and cannot mint another code.
Revoking or expiring the parent immediately invalidates its sessions.
"""
import hashlib
import json
import secrets

from .store import PermissionDenied, canonical_json, token_hash


class Sessions:
    def __init__(self,store):
        self.store=store

    @staticmethod
    def create_tables(c):
        c.executescript('''
            CREATE TABLE IF NOT EXISTS bridge_pairing (
                code_hash TEXT PRIMARY KEY,parent_hash TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_web_sessions (
                token_hash TEXT PRIMARY KEY,parent_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_pairing_attempts (
                client_hash TEXT PRIMARY KEY,window REAL NOT NULL,attempts INTEGER NOT NULL);
        ''')

    def issue(self,token):
        with self.store._transaction() as c:
            actor=self.store._authenticate(c,token)
            self.store._role(actor,('owner','manager'))
            if c.execute('SELECT 1 FROM bridge_web_sessions WHERE token_hash=?',(actor['token_hash'],)).fetchone():
                raise PermissionDenied('일회용 코드는 원래 관리 연결에서만 발급해요.')
            code=secrets.token_hex(6).upper()
            expires=self.store._now()+120
            c.execute('INSERT INTO bridge_pairing VALUES (?,?,?,0)',
                (hashlib.sha256(code.encode()).hexdigest(),actor['token_hash'],expires))
            return {'code':code,'expires_at':expires,'one_time':True}

    def redeem(self,code,client_id):
        # Count even malformed attempts, and commit counters before rejection.
        now=self.store._now()
        with self.store._transaction() as c:
            limited=False
            for client in (client_id,'global'):
                key=hashlib.sha256(('pairing:'+client).encode()).hexdigest()
                row=c.execute('SELECT * FROM bridge_pairing_attempts WHERE client_hash=?',(key,)).fetchone()
                count=row['attempts']+1 if row and row['window']>now-60 else 1
                window=row['window'] if row and row['window']>now-60 else now
                c.execute('INSERT OR REPLACE INTO bridge_pairing_attempts VALUES (?,?,?)',(key,window,count))
                limited=limited or count>(60 if client=='global' else 5)
        if limited or not isinstance(code,str) or len(code)!=12:
            raise PermissionDenied('연결 코드를 확인하거나 잠시 후 다시 시도해 주세요.')
        code=code.upper()
        with self.store._transaction() as c:
            now=self.store._now()
            pair=c.execute('SELECT * FROM bridge_pairing WHERE code_hash=?',
                           (hashlib.sha256(code.encode()).hexdigest(),)).fetchone()
            if not pair or pair['used'] or pair['expires']<=now:
                raise PermissionDenied('연결 코드가 없거나 만료·사용됐어요.')
            parent=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(pair['parent_hash'],)).fetchone()
            if not parent or parent['revoked'] is not None or (parent['expires'] is not None and parent['expires']<=now):
                raise PermissionDenied('원래 관리 연결이 만료·해제됐어요.')
            raw=secrets.token_urlsafe(32)
            expires=min(now+900,parent['expires']) if parent['expires'] is not None else now+900
            c.execute('INSERT INTO bridge_tokens VALUES (?,?,?,?,?,?,NULL,?)',
                (token_hash(raw),parent['subject'],parent['role'],parent['projects'],parent['capabilities'],expires,now))
            c.execute('INSERT INTO bridge_web_sessions VALUES (?,?)',(token_hash(raw),pair['parent_hash']))
            c.execute('UPDATE bridge_pairing SET used=1 WHERE code_hash=?',(pair['code_hash'],))
            return {'token':raw,'expires_at':expires}

    def cookie_token(self,raw):
        actor=self.store.authenticate(raw)
        with self.store._transaction() as c:
            if not c.execute('SELECT 1 FROM bridge_web_sessions WHERE token_hash=?',(actor['token_hash'],)).fetchone():
                raise PermissionDenied('웹 연결 세션이 필요해요.')
        return raw

    def logout(self,raw):
        actor=self.store.authenticate(raw)
        with self.store._transaction() as c:
            if not c.execute('SELECT 1 FROM bridge_web_sessions WHERE token_hash=?',(actor['token_hash'],)).fetchone():
                raise PermissionDenied('웹 연결 세션만 종료할 수 있어요.')
            c.execute('UPDATE bridge_tokens SET revoked=? WHERE token_hash=?',(self.store._now(),actor['token_hash']))
        return {'logged_out':True}


def main(argv=None):
    import argparse
    from pathlib import Path
    from .store import Store
    from .control import secret
    parser=argparse.ArgumentParser(description='관리 화면 일회용 연결 코드 발급')
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--token-file',type=Path,required=True)
    args=parser.parse_args(argv)
    result=Sessions(Store(args.data/'manager.sqlite3')).issue(secret(args.token_file))
    print(json.dumps(result))


if __name__=='__main__':main()
