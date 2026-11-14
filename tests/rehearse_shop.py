"""One-command empty-environment business rehearsal. No production side effects."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import sys
import tempfile
from unittest.mock import patch

# Ignore inherited production paths and contact settings even during imports.
_IMPORT_ROOT=tempfile.TemporaryDirectory(prefix='bellavita-rehearsal-import-')
os.environ.update(TAESAN_DATA=_IMPORT_ROOT.name,TAESAN_LIVE_WRITE='0',SHOP_MESSAGES_ENABLED='0',
                  SHOP_CASH_RECEIPT_ENABLED='0',SHOP_BACKUP_ENABLED='0',SHOP_HANDOFF_ENABLED='0',
                  INVITATION_CHECKOUT_ENABLED='0',SHOP_STAFF_PHONES='',SHOP_MESSAGE_CHANNEL='mms',
                  OPENAI_API_KEY='',KAKAO_CLIENT_ID='',TAESAN_STOCK_WRITERS='')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_shop_growth import GrowthTest,photo_bytes
from test_shop import notification,SECRET
import agent
import assignment_batches
import cash_receipts
import delivery_photos
import manage_data
import order_amendments
import order_messages
import order_ops
import scheduled_backups
import shop_dispatch
import shop_receiver
import shop_sales
import shop_settlement


def run(destination):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('리허설은 기존 데이터를 덮어쓰지 않는 새 출력 경로만 사용합니다.')
    destination.mkdir(mode=0o700,parents=True)
    fixture=GrowthTest('test_amendment_keeps_original_money_requires_new_review_and_receiver_snapshot');fixture.setUp()
    evidence={};root=destination/'factory'
    guard=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('outbound network forbidden in rehearsal'))
    env=patch.dict(os.environ,{'TAESAN_LIVE_WRITE':'0','SHOP_MESSAGES_ENABLED':'0','SHOP_CASH_RECEIPT_ENABLED':'0','SHOP_BACKUP_ENABLED':'0','SHOP_HANDOFF_ENABLED':'1','SHOP_HANDOFF_SECRET':'growth-key-'*4})
    guard.start();env.start()
    try:
        # Product registration and two-price offers are created through real local
        # HTTP admin handlers by the fixture, in its new private SQLite directory.
        with agent.db() as c:
            evidence['products']=c.execute('SELECT count(*) FROM shop_products').fetchone()[0]
            evidence['offer_prices']=[r[0] for r in c.execute('SELECT price FROM shop_offers ORDER BY price')]
            # The sample merchant explicitly declares the synthetic tax category.
            for row in c.execute('SELECT id,details FROM shop_offers').fetchall():
                data=json.loads(row['details']);data['cash_tax_type']='TG02';c.execute('UPDATE shop_offers SET details=? WHERE id=?',(json.dumps(data),row['id']))
        fixture.pay(cash_receipt_type='personal');original=fixture.order()['details']
        from datetime import datetime
        fixture.notify(notification('a'*32,pay_type='6',pay_date=datetime.now(order_ops.SEOUL).strftime('%Y%m%d%H%M%S')))
        with patch.dict(os.environ,{'SHOP_CASH_RECEIPT_ENABLED':'1','SHOP_CASH_RECEIPT_OWNER':'shop'}),patch.object(cash_receipts,'request',return_value={'state':'1','cashstno':'900001','receipt_no':'MOCK-APPROVAL','cashsturl':'https://payapp.kr/mock'}):
            for _ in range(3):cash_receipts.process_one(agent.db)
        with agent.db() as c:
            receipt=c.execute('SELECT state FROM shop_cash_receipts WHERE order_id=?',('a'*32,)).fetchone()
            if not receipt or receipt['state']!='issued':raise ValueError('mock_cash_receipt_not_issued')
        evidence['mock_cash_receipt']='issued'
        fixture.artifact();fixture.drain(root)
        fixture.request();fixture.decide();fixture.artifact();fixture.drain(root)
        with agent.db() as c:
            effective=json.loads(order_amendments.effective(c,fixture.order())['details'])
            if effective['message']!='승인한 새 축하 문구' or fixture.order()['details']!=original:raise ValueError('original_snapshot_changed')
        evidence['amendment_original_preserved']=True;evidence['new_artifact_version']=2
        first_offer=fixture.offer;fixture.offer=fixture.offers[1]['id'];fixture.pay('b'*32,'1235');fixture.offer=first_offer
        when=json.loads(fixture.order()['details'])['event_time']
        fixture.csv([['a'*32,when,'staff','0','리허설 배정'],['b'*32,when,'staff','0','리허설 두 번째 가격']],key='f'*32)
        fixture.post_admin(action='apply_assignment_csv',batch_id='f'*32,batch_checked='yes',batch_row_1='yes',batch_row_2='yes')
        with agent.db() as c:staff=shop_dispatch.link(c,'a'*32,SECRET)
        if fixture.http(staff,dict(action='accept_assignment',order_id='a'*32,assignment_version='1',handover_note='리허설 인수',handover_checked='yes'))[0]!=303:raise ValueError('handover_failed')
        fixture.post_admin(action='change_stage',order_id='a'*32,stage='shipping',version='1');fixture.post_admin(action='change_stage',order_id='a'*32,stage='delivered',version='2')
        status,_=fixture.multipart(dict(action='upload_delivery_photo',order_id='a'*32,photo_note='합성 리허설 사진'),'photo',photo_bytes())
        if status!=303:raise ValueError('photo_upload_failed')
        with agent.db() as c:photo=c.execute('SELECT * FROM shop_delivery_photos').fetchone()
        fixture.post_admin(action='publish_delivery_photo',order_id='a'*32,photo_id=photo['id'],photo_version='1',photo_state='public',photo_review_note='리허설 공개 검수',photo_checked='yes')
        page=fixture.http('/invitation-orders/'+('a'*32)+'/receipt')[2]
        import re
        fields={name:re.search('name="'+name+'" value="([^"]+)"',page)[1] for name in ('photo_id','photo_version','photo_expiry','photo_signature')};fields['photo_confirmed']='yes'
        if fixture.http('/invitation-orders/'+('a'*32)+'/delivery-confirm',fields)[0]!=303:raise ValueError('customer_confirm_failed')
        fixture.post_admin(action='create_service_case',order_id='a'*32,case_attempt='e'*32,case_kind='as',case_reason='리허설 AS 기록')
        for version,state in [(1,'assigned'),(2,'in_progress'),(3,'resolved'),(4,'closed')]:
            if version==3 and fixture.multipart(dict(action='upload_service_photo',order_id='a'*32,case_id='e'*32,photo_note='AS 합성 사진'),'photo',photo_bytes())[0]!=303:raise ValueError('service_photo_failed')
            fixture.post_admin(action='update_service_case',order_id='a'*32,case_id='e'*32,case_version=str(version),case_state=state,case_assignee='staff',case_note='리허설 처리 근거')
        fixture.notify(notification('b'*32,'9001','70',10000,orig_mul_no='1235',orig_price='99000'))
        fixture.notify(notification('b'*32,'9001','70',10000,orig_mul_no='1235',orig_price='99000'))
        with agent.db() as c:
            sales=shop_sales.aggregate(c,{});settlement,filename=shop_settlement.workbook(c,{})
            if sales['totals'][4:7]!=[192500,10000,182500]:raise ValueError('sales_totals_mismatch')
            (destination/filename).write_bytes(settlement);sales_bytes,sales_name=shop_sales.workbook(c,{});(destination/sales_name).write_bytes(sales_bytes)
            (destination/'assignment-results.csv').write_bytes(assignment_batches.report(c,'f'*32))
        fixture.post_admin(action='run_shop_diagnostics')
        # Exercise the real outbox adapter with an in-memory provider response.
        sent=[]
        def mock_messages(path,body=None):
            if '/send-' in path:
                item=body['messages'][0];sent.append(item);return {'groupInfo':{'groupId':'MOCK-GROUP'},'messageList':[{'messageId':'MOCK-'+str(len(sent))}]}
            return {'messageList':{}}
        with patch.dict(os.environ,{'SHOP_MESSAGES_ENABLED':'1','SOLAPI_API_KEY':'MOCK','SOLAPI_API_SECRET':'MOCK','SOLAPI_SENDER':'0212345678'}),patch.object(order_messages,'api',side_effect=mock_messages):
            def api(path,body=None):return {'fileId':'MOCK_IMAGE'} if path.startswith('/storage/') else mock_messages(path,body)
            with patch.object(order_messages,'api',side_effect=api):
                for _ in range(20):order_messages.process_one(agent.db,fixture.root)
        if len(sent)<3:raise ValueError('mock_notifications_missing')
        evidence['mock_notifications_accepted']=len(sent)
        evidence['sales_totals']=sales['totals'];evidence['assignment_rows_applied']=2;evidence['customer_photo_confirmed']=True;evidence['service_case']='closed'
        scheduled_backups.snapshot(fixture.root,destination/'snapshot');manage_data.transfer(destination/'snapshot',destination/'restored',restore=True)
        report=manage_data.inspect(destination/'restored');evidence['restore_integrity']=report['integrity_check'];evidence['restored_counts']=report['counts']
        shop_receiver.backup(root,destination/'factory-snapshot.sqlite3')
        restored_factory=destination/'factory-restored';shutil.copytree(root,restored_factory)
        shutil.copy2(destination/'factory-snapshot.sqlite3',restored_factory/'receiver.sqlite3')
        # Resume pending cancellation holds from a freshly opened restored source
        # and restored receiver, without repeating the already ACKed production.
        with patch.object(agent,'ROOT',destination/'restored'):
            fixture.drain(restored_factory)
        with sqlite3.connect(restored_factory/'receiver.sqlite3') as c:
            count,unique=c.execute('SELECT count(*),count(DISTINCT order_id) FROM order_ledger').fetchone()
            first=c.execute('SELECT state FROM production_queue WHERE order_id=?',('a'*32,)).fetchone()[0]
            second=c.execute('SELECT state FROM production_queue WHERE order_id=?',('b'*32,)).fetchone()[0]
            if count!=2 or unique!=2 or first!='pending' or second!='held':raise ValueError('restored_factory_replay_mismatch')
        evidence['restored_factory_orders']=2;evidence['restored_factory_duplicate_orders']=0;evidence['pending_hold_recovered']=True

        with sqlite3.connect(destination/'restored'/'state.sqlite3') as c:
            if c.execute('SELECT details FROM invitation_orders WHERE id=?',('a'*32,)).fetchone()[0]!=original:raise ValueError('restored_original_changed')
            if c.execute('SELECT count(*) FROM shop_delivery_confirms').fetchone()[0]!=1:raise ValueError('restored_confirmation_missing')
        evidence.update(result='passed',outbound_network_requests=0,actual_payment=False,actual_messages=False,actual_printing=False,live_sheet_writes=False,production_deployed=False)
        (destination/'rehearsal.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2));return evidence
    finally:env.stop();guard.stop();fixture.tearDown()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True);args=parser.parse_args()
    print(json.dumps(run(args.output),ensure_ascii=False,indent=2))
