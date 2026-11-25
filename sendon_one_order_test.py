"""One supervised test from the Render shell. No web endpoint or queue drain."""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import sendon_order_queue as queue


def run(db, order_id, audience, *, send=False, result=False, env=None, open_request=None):
    env = os.environ if env is None else env
    if queue.mode(env) != 'dry_run':
        raise ValueError('테스트는 dry_run 모드에서만 가능합니다.')
    if not re.fullmatch('[a-f0-9]{32}', order_id):
        raise ValueError('테스트 주문 ID를 확인하세요.')
    template, suffix = {'staff': (queue.STAFF, '9938'), 'customer': (queue.CUSTOMER, '1730')}[audience]
    allowed = env.get('SENDON_TEST_' + audience.upper() + '_PHONE', '')
    if not re.fullmatch(r'01[016789][0-9]{7,8}', allowed) or not allowed.endswith(suffix):
        raise ValueError('지정한 테스트 전체 번호를 환경변수에서 확인하세요.')
    live_env = dict(env, SENDON_MODE='live')
    if not queue.live_enabled(live_env):
        raise ValueError('Sendon API 인증 설정이 필요합니다.')
    with db:
        db.execute('''CREATE TABLE IF NOT EXISTS sendon_supervised_test (
            template TEXT PRIMARY KEY, order_id TEXT NOT NULL, api_state TEXT NOT NULL,
            request_id TEXT, api_code TEXT, final_state TEXT, final_code TEXT
        )''')
    previous = db.execute('SELECT order_id,api_state,request_id,api_code,final_state,final_code FROM sendon_supervised_test WHERE template=?', (template,)).fetchone()
    if previous and previous[0] != order_id:
        raise ValueError('이미 다른 주문에 이 테스트를 사용했습니다.')
    if result:
        if not previous:
            raise ValueError('확인할 발송 기록이 없습니다.')
        if previous[1] == 'accepted' and not previous[4]:
            queue.reconcile(db, order_id, template, previous[2], allowed, live_env,
                            open_request=open_request, supervised=True)
            previous = db.execute('SELECT order_id,api_state,request_id,api_code,final_state,final_code FROM sendon_supervised_test WHERE template=?',(template,)).fetchone()
        return dict(zip(('order_id','api_state','request_id','api_code','final_state','final_code'),previous))
    order = db.execute('SELECT price,created,details,state FROM invitation_orders WHERE id=?',(order_id,)).fetchone()
    if not order or order[0] != 1000 or json.loads(order[2]).get('payment_method') != 'bank_transfer':
        raise ValueError('1,000원 무통장 테스트 주문만 사용할 수 있습니다.')
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(order[1])
    if not dt.timedelta(0) <= age <= dt.timedelta(hours=2):
        raise ValueError('최근 2시간 이내에 생성된 주문을 사용하세요.')
    if order[3] != ('bank_pending' if audience == 'staff' else 'paid'):
        raise ValueError('접수·입금 단계가 템플릿과 맞지 않습니다.')
    staff = db.execute('SELECT order_id,api_state FROM sendon_supervised_test WHERE template=?',(queue.STAFF,)).fetchone()
    if audience == 'customer' and (not staff or tuple(staff) != (order_id,'accepted')):
        raise ValueError('같은 주문의 직원 테스트 접수를 먼저 확인하세요.')
    item = db.execute('SELECT recipient,variables FROM sendon_order_queue WHERE order_id=? AND template=?',(order_id,template)).fetchone()
    if not item or item[0] != allowed:
        raise ValueError('큐에 기록된 수신번호가 지정된 테스트 번호와 다릅니다.')
    if previous:
        raise ValueError('이미 발송을 시도했습니다. 결과 조회를 사용하세요.')
    if not send:
        return {'order_id':order_id,'template':template,'recipient_last4':suffix,'ready':True}
    with db:
        claim = db.execute("INSERT OR IGNORE INTO sendon_supervised_test(template,order_id,api_state) VALUES(?,?,'attempting')",(template,order_id))
        if claim.rowcount != 1:
            raise ValueError('이미 발송을 시도했습니다.')
    status, request_id, code, _ = queue.transmit(template, allowed, json.loads(item[1]),env=live_env,open_request=open_request)
    with db:
        db.execute('UPDATE sendon_supervised_test SET api_state=?,request_id=?,api_code=? WHERE template=?',(status,request_id,code,template))
    return {'api_state':status,'request_id':request_id,'api_code':code}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('database', type=Path)
    parser.add_argument('order_id')
    parser.add_argument('audience', choices=('staff','customer'))
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument('--send', action='store_true')
    flags.add_argument('--result', action='store_true')
    args = parser.parse_args()
    with closing(sqlite3.connect(args.database.resolve().as_uri()+'?mode=rw',uri=True,timeout=10)) as db:
        print(json.dumps(run(db,args.order_id,args.audience,send=args.send,result=args.result),ensure_ascii=False))
