"""Native four-product verification process: mock PayApp, restart and backup/restore."""
import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlencode,urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import manage_data


def run(destination):
    root=Path(destination).resolve();root.mkdir(parents=True,exist_ok=False)
    data=root/'data';secret=secrets.token_hex(32);origin='https://isolated-verification.test'
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=dict(os.environ,TAESAN_DATA=str(data),TAESAN_PUBLIC_URL=origin,TAESAN_WEBHOOK_SECRET=secret,SHOP_VERIFICATION_MODE='1',SHOP_REQUIRE_PERSISTENT_DISK='0',SHOP_BACKUP_ENABLED='0',SHOP_BACKUP_DIRECTORY=str(root/'snapshots'),SHOP_VERIFICATION_REVIEW_KEY='local-isolated-review-test-key-00001',SHOP_BACKUP_ENCRYPTION_KEY='ab'*32,TAESAN_BIND='127.0.0.1',PORT=str(port))
    log=(root/'server.log').open('w')
    def call(path,fields=None,review=False,raw=None):
        c=http.client.HTTPConnection('127.0.0.1',port,timeout=10);headers={'Origin':origin}
        if review:headers['Authorization']='Bearer '+env['SHOP_VERIFICATION_REVIEW_KEY']
        if fields is not None:headers['Content-Type']='application/x-www-form-urlencoded'
        if raw is not None:headers['Content-Type']='application/octet-stream'
        c.request('POST' if fields is not None or raw is not None else 'GET',path,raw if raw is not None else urlencode(fields) if fields is not None else None,headers);r=c.getresponse();out=r.status,dict(r.getheaders()),r.read();c.close();return out
    def start():
        p=subprocess.Popen([sys.executable,'shop_verification.py'],env=env,stdout=log,stderr=log)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if p.poll() is not None:raise RuntimeError('verification_start_failed')
            try:
                if call('/healthz')[0]==200:return p
            except OSError:pass
            time.sleep(.1)
        p.terminate();raise RuntimeError('verification_start_timeout')
    process=None
    try:
        process=start();assert call('/verification/review')[0]==401;assert call('/verification/review',review=True)[0]==200;assert call('/shop')[2].count(b'class="card"')==4
        identity=hashlib.sha256(b'bellavita-product:1454214').hexdigest()[:32];source=json.loads(Path('assets/shop/catalog.json').read_text())['products'][0]
        fields=dict(attempt='b'*32,product_revision='1',sender='독립 검증 보내는 분',buyer='독립 검증 주문자',buyer_phone='01000000000',consent='yes',message='네 상품 운영 준비 검증',extra0='없음',venue='독립 검증 홀',address='실배송 없는 검증 장소',event_time=(dt.datetime.now()+dt.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M'))
        selects=[g for g in source['options'] if g['type']=='select']
        for i,g in enumerate(selects):fields['choice'+str(i)]=g['choices'][1 if i==0 else 0]['id']
        status,h,_=call('/shop/products/'+identity,fields);assert status==303,status
        payment=urlsplit(h['Location']).path;assert payment.startswith('/verification/pay/')
        for state in ('4','4','70','70','64','4'):assert call(payment,{'state':state})[0]==303
        def read():
            with sqlite3.connect(data/'state.sqlite3') as c:
                row=c.execute('SELECT id,state,details FROM invitation_orders').fetchone();count=c.execute("SELECT count(*) FROM payment_events WHERE kind='partial'").fetchone()[0]
                return row[0],row[1],json.loads(row[2]),count
        orderid,state,order,count=read();assert state=='cancelled' and order['amount']==165000 and count==1
        encrypted=call('/verification/review',{'action':'export_backup'},review=True);assert encrypted[0]==200 and encrypted[2].startswith(b'BVBACKUP1\n')
        decoded=call('/verification/restore-check',review=True,raw=encrypted[2]);assert decoded[0]==200 and json.loads(decoded[2])['restore_check']=='passed'
        assert call('/verification/restore-check',review=True,raw=encrypted[2][:-1]+b'X')[0]==422
        checked=call('/verification/review',{'action':'backup_restore'},review=True);assert checked[0]==200 and json.loads(checked[2])['backup_restore']=='passed'
        process.terminate();process.wait(timeout=10);process=None
        backup=root/'backup';restored=root/'restored';manage_data.transfer(data,backup);manage_data.transfer(backup,restored,restore=True);assert manage_data.inspect(data)['counts']==manage_data.inspect(restored)['counts']
        process=start();assert read()==(orderid,state,order,count);assert call('/shop')[2].count(b'class="card"')==4
        status=call('/verification');assert status[0]==200 and json.loads(status[2])['live_payment'] is False
        report={'products':4,'server_amount':165000,'mock_payment':True,'duplicate_paid_once':True,'duplicate_partial_once':True,'late_paid_does_not_undo_cancel':True,'process_restart_preserved':True,'backup_restore':'passed','live_payment':False,'live_messages':False,'live_receipts':False,'render_disk_verification':'not_run'}
        (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:
        if process:process.terminate();process.wait(timeout=10)
        log.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);run(p.parse_args().output)
