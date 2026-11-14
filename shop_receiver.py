"""Factory-side order ledger copy and production queue. Does not print or write live Sheets."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import ssl
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from order_handoff import canonical


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class AckConflict(Exception):
    def __init__(self, code):
        self.code = code


def database(root):
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (root / 'state.sqlite3').exists():
        raise ValueError('수신기는 태산이 서버 데이터와 다른 전용 디렉터리를 사용해야 합니다.')
    c = sqlite3.connect(root / 'receiver.sqlite3', timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS received_events (
        event_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, kind TEXT NOT NULL,
        digest TEXT NOT NULL, ack_json TEXT, acked INTEGER NOT NULL DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS order_ledger (
        order_id TEXT PRIMARY KEY, details TEXT NOT NULL, state TEXT NOT NULL, payment TEXT NOT NULL DEFAULT '{}')''')
    if 'payment' not in {row[1] for row in c.execute('PRAGMA table_info(order_ledger)')}:
        c.execute("ALTER TABLE order_ledger ADD COLUMN payment TEXT NOT NULL DEFAULT '{}'")
    c.execute('''CREATE TABLE IF NOT EXISTS production_queue (
        order_id TEXT PRIMARY KEY, details TEXT NOT NULL, state TEXT NOT NULL)''')
    for table in ('production_queue', 'received_events'):
        if 'guard' not in {row[1] for row in c.execute('PRAGMA table_info('+table+')')}:
            c.execute("ALTER TABLE "+table+" ADD COLUMN guard TEXT NOT NULL DEFAULT '[0,0]'")
    return c


def ingest(root, job):
    payload = job['payload']
    if (payload.get('schema_version') != (2 if job.get('kind') == 'resume' else 1) or payload.get('kind') != job.get('kind')
            or job.get('kind') not in ('ready', 'hold', 'resume') or payload['order'].get('id') != job.get('order_id')
            or hashlib.sha256(canonical(payload).encode()).hexdigest() != job.get('sha256')):
        raise ValueError('handoff_payload_mismatch')
    if payload['kind'] == 'ready' and payload.get('payment', {}).get('state') != 'paid':
        raise ValueError('payment_not_verified')
    payment = payload.get('payment', {})
    total, refunded, remaining = (payment.get(key) for key in ('original_amount','refunded_amount','remaining_amount'))
    if (any(type(value) is not int for value in (total,refunded,remaining)) or total != payload['order']['amount']
            or not 0 <= refunded <= total or remaining != total-refunded):
        raise ValueError('payment_totals_mismatch')
    revision = payload.get('review_revision', 0)
    if type(revision) is not int or revision < 0:
        raise ValueError('invalid_review_revision')
    if payload['kind'] == 'resume':
        approval = payload.get('approval', {})
        if (payment['state'] != 'partial_cancel' or remaining <= 0 or approval.get('decision') != 'resume'
                or approval.get('id') != revision or revision <= 0 or approval.get('remaining') != remaining
                or approval.get('refunded') != refunded or not isinstance(approval.get('fingerprint'), str)
                or len(approval['fingerprint']) != 64):
            raise ValueError('partial_approval_required')
    guard = [refunded, revision]
    order_id, kind = job['order_id'], job['kind']
    ack = {'event_id': job['event_id'], 'lease_token': job['lease_token'], 'consumer': job['consumer'],
           'references': {'ledger': order_id, 'production': order_id}}
    c = database(root)
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM received_events WHERE event_id=?', (job['event_id'],)).fetchone()
            if old and (old['digest'] != job['sha256'] or old['order_id'] != order_id or old['kind'] != kind):
                raise ValueError('event_reused_with_different_payload')
            order = canonical(payload['order'])
            existing = c.execute('SELECT * FROM order_ledger WHERE order_id=?', (order_id,)).fetchone()
            if existing and existing['details'] != order:
                raise ValueError('order_snapshot_mismatch')
            state = 'held' if kind == 'hold' else 'awaiting_ack'
            c.execute('INSERT OR IGNORE INTO order_ledger VALUES (?,?,?,?)', (order_id, order, state, canonical(payment)))
            previous = json.loads(existing['payment']) if existing else {}
            # A delayed partial hold/ready can never reduce an already recorded refund.
            if refunded >= previous.get('refunded_amount',0):
                c.execute('UPDATE order_ledger SET payment=? WHERE order_id=?', (canonical(payment),order_id))
            c.execute('INSERT OR IGNORE INTO production_queue(order_id,details,state,guard) VALUES (?,?,?,?)', (order_id, canonical(payload['production']), state, canonical(guard)))
            current_guard = json.loads(c.execute('SELECT guard FROM production_queue WHERE order_id=?', (order_id,)).fetchone()[0])
            if kind == 'hold' and guard >= current_guard:
                c.execute("UPDATE order_ledger SET state='held' WHERE order_id=?", (order_id,))
                c.execute("UPDATE production_queue SET state='held' WHERE order_id=?", (order_id,))
            if kind == 'resume' and guard > current_guard:
                c.execute("UPDATE order_ledger SET state='awaiting_ack' WHERE order_id=?", (order_id,))
                c.execute("UPDATE production_queue SET state='awaiting_ack' WHERE order_id=?", (order_id,))
            if guard >= current_guard:
                c.execute('UPDATE production_queue SET guard=? WHERE order_id=?', (canonical(guard), order_id))
            c.execute('''INSERT INTO received_events(event_id,order_id,kind,digest,ack_json,guard) VALUES (?,?,?,?,?,?)
                         ON CONFLICT(event_id) DO UPDATE SET ack_json=excluded.ack_json''',
                      (job['event_id'], order_id, kind, job['sha256'], canonical(ack), canonical(guard)))
    finally:
        c.close()
    return ack


def finish(root, event_id, *, payment_changed=False):
    c = database(root)
    try:
        with c:
            row = c.execute('SELECT * FROM received_events WHERE event_id=?', (event_id,)).fetchone()
            if not row:
                raise ValueError('unknown_local_event')
            current_guard = c.execute('SELECT guard FROM production_queue WHERE order_id=?', (row['order_id'],)).fetchone()[0]
            if payment_changed:
                if json.loads(row['guard']) < json.loads(current_guard):
                    c.execute('UPDATE received_events SET ack_json=NULL WHERE event_id=?', (event_id,))
                    return
                c.execute("UPDATE order_ledger SET state='held' WHERE order_id=?", (row['order_id'],))
                c.execute("UPDATE production_queue SET state='held' WHERE order_id=?", (row['order_id'],))
                c.execute('UPDATE received_events SET ack_json=NULL WHERE event_id=?', (event_id,))
                return
            c.execute('UPDATE received_events SET acked=1,ack_json=NULL WHERE event_id=?', (event_id,))
            if row['kind'] in ('ready','resume') and row['guard'] == current_guard:
                c.execute("UPDATE order_ledger SET state='received' WHERE order_id=? AND state!='held'", (row['order_id'],))
                c.execute("UPDATE production_queue SET state='pending' WHERE order_id=? AND state!='held'", (row['order_id'],))
    finally:
        c.close()


def call(server, secret, action, fields, ca_file=None):
    parsed = urlsplit(server)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('수신 주소는 HTTPS 서버 주소여야 합니다.')
    if len(secret) < 32:
        raise ValueError('SHOP_HANDOFF_SECRET에 32자 이상의 별도 연결 키를 설정하세요.')
    context = ssl.create_default_context(cafile=ca_file)
    request = Request(server.rstrip('/') + '/shop/handoff/' + action,
                      canonical(fields).encode(), {'Authorization': 'Bearer '+secret, 'Content-Type':'application/json'})
    try:
        with build_opener(NoRedirect, HTTPSHandler(context=context)).open(request, timeout=15) as response:
            return json.loads(response.read(1_000_000).decode())
    except HTTPError as error:
        if error.code == 409:
            raise AckConflict(json.loads(error.read(4096)).get('error')) from None
        raise


def acknowledge_pending(root, transport):
    c = database(root)
    try:
        rows = c.execute('SELECT event_id,ack_json FROM received_events WHERE ack_json IS NOT NULL ORDER BY event_id').fetchall()
    finally:
        c.close()
    for row in rows:
        try:
            result = transport('ack', json.loads(row['ack_json']))
            if result.get('ok') is not True:
                raise ValueError('invalid_ack_response')
            finish(root, row['event_id'])
        except AckConflict as error:
            if error.code == 'payment_changed':
                finish(root, row['event_id'], payment_changed=True)
            else:
                # The source will lease the event again. Keep local queue unreleased
                # and replace its ACK token on redelivery, without another ledger row.
                c = database(root)
                try:
                    with c:
                        c.execute('UPDATE received_events SET ack_json=NULL WHERE event_id=?', (row['event_id'],))
                finally:
                    c.close()


def run_once(root, consumer, transport):
    acknowledge_pending(root, transport)
    result = transport('claim', {'consumer': consumer, 'capabilities':['partial-resume-v1']})
    job = result.get('job')
    if not job:
        return False
    ingest(root, job)
    acknowledge_pending(root, transport)
    return True


def export_csv(root, destination):
    c = database(root)
    try:
        rows = c.execute('SELECT * FROM order_ledger ORDER BY order_id').fetchall()
    finally:
        c.close()
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('CSV는 기존 파일을 덮어쓰지 않는 새 경로를 지정해 주세요.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    def cell(value):
        text = str(value)
        return "'"+text if text.lstrip().startswith(('=','+','-','@')) or text.startswith(('\t','\r','\n')) else text
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='', dir=destination.parent, delete=False) as output:
        writer = csv.writer(output)
        writer.writerow(['주문번호','상태','행사·상품','주문자','전화번호','보내는 분','문구','장소','주소','행사 시각','원결제금액','취소누계','잔액'])
        for row in rows:
            data = json.loads(row['details'])
            payment = json.loads(row['payment'])
            writer.writerow([cell(value) for value in (row['order_id'],row['state'],data['title'],data['buyer'],data['buyer_phone'],
                data['sender'],data['message'] or dict(data['options']).get('축하 문구',''),data['venue'],data['address'],data['event_time'],
                data['amount'],payment.get('refunded_amount',0),payment.get('remaining_amount',data['amount']))])
        temporary = Path(output.name)
    temporary.replace(destination)


def backup(root, destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('백업은 기존 파일을 덮어쓰지 않는 새 경로를 지정해 주세요.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
        path = Path(temporary.name)
    source, target = database(root), sqlite3.connect(path)
    try:
        source.backup(target)
        if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('receiver_backup_integrity_failed')
    finally:
        source.close();target.close()
    path.replace(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server')
    parser.add_argument('--data', required=True)
    parser.add_argument('--consumer', default='factory')
    parser.add_argument('--ca-file')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--export-csv')
    parser.add_argument('--backup')
    args = parser.parse_args()
    if args.backup:
        backup(args.data, args.backup)
        raise SystemExit(0)
    if not args.server:
        parser.error('--server가 필요합니다.')
    if args.watch and args.export_csv:
        parser.error('--export-csv는 단일 수신 실행에서 새 파일 경로로 사용하세요.')
    transport = lambda action, fields: call(args.server, os.getenv('SHOP_HANDOFF_SECRET',''), action, fields, args.ca_file)
    while True:
        try:
            received = run_once(args.data, args.consumer, transport)
            print('접수 확인' if received else '대기 주문 없음', flush=True)
            if args.export_csv:
                export_csv(args.data, args.export_csv)
        except Exception as error:
            print('수신 확인 필요:', type(error).__name__, flush=True)
            if not args.watch:
                raise SystemExit(1)
        if not args.watch:
            break
        time.sleep(3)
