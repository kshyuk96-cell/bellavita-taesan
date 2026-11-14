"""Authenticated, test-only PC bridge. Never dispatches orders or writes Sheets."""
import hmac
import re

PATH = '/pc/planning-probe'
ROOM = '기획'
REQUEST = re.compile(r'태산이 수신 테스트 ([a-f0-9]{24})')


def authorized(authorization, token):
    if not isinstance(token,str) or len(token)<32 or not isinstance(authorization,str):
        return False
    return hmac.compare_digest(authorization.encode(), ('Bearer '+token).encode())


def respond(connection, authorization, token, payload):
    if not authorized(authorization,token):
        return 401, {'error':'probe_not_authorized'}
    if not isinstance(payload,dict) or set(payload)!={'schema','room','text'}:
        return 422, {'error':'invalid_probe'}
    if payload['schema']!='planning-probe-v1' or payload['room']!=ROOM or not isinstance(payload['text'],str):
        return 422, {'error':'invalid_probe'}
    match=REQUEST.fullmatch(payload['text'])
    if not match:return 422, {'error':'only_exact_test_marker_allowed'}
    nonce=match[1]
    connection.execute('CREATE TABLE IF NOT EXISTS planning_probe_nonces (nonce TEXT PRIMARY KEY)')
    with connection:
        new=connection.execute('INSERT OR IGNORE INTO planning_probe_nonces VALUES (?)',(nonce,)).rowcount==1
    return 200, {'schema':'planning-probe-reply-v1','room':ROOM,'nonce':nonce,
                 'reply':f'[태산이 연결 확인 {nonce}] 기획방 수신 확인 / 시트 변경 없음',
                 'duplicate':not new,'sheet_written':False}
