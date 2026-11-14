"""Non-sending configuration checks and isolated protocol simulations."""
from contextlib import contextmanager
from datetime import datetime,timezone
from html import escape
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import urlsplit


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS shop_diagnostic_runs (
        id INTEGER PRIMARY KEY, created TEXT NOT NULL, actor TEXT NOT NULL, report TEXT NOT NULL)''')


def configuration(root):
    import checkout
    env=os.environ
    def group(flag,keys):return {'enabled':env.get(flag)=='1','configured':all(bool(env.get(k)) for k in keys),'missing_fields':[k for k in keys if not env.get(k)],'live_validation':'not_run'}
    origin=urlsplit(env.get('TAESAN_PUBLIC_URL',''))
    return {'public_url_https':origin.scheme=='https' and bool(origin.hostname) and not origin.username and not origin.password and not origin.query and not origin.fragment,
        'webhook_secret_configured':len(env.get('TAESAN_WEBHOOK_SECRET',''))>=32,
        'payapp':group('SHOP_CHECKOUT_ENABLED',['INVITATION_PAYAPP_'+k for k in checkout.PAYAPP_FIELDS]),
        'mms':group('SHOP_MESSAGES_ENABLED',['SOLAPI_API_KEY','SOLAPI_API_SECRET','SOLAPI_SENDER']),
        'alimtalk':group('SHOP_ALIMTALK_ENABLED',['SOLAPI_API_KEY','SOLAPI_API_SECRET','SOLAPI_KAKAO_PFID','SHOP_ALIMTALK_TEMPLATE_ID']),
        'handoff':group('SHOP_HANDOFF_ENABLED',['SHOP_HANDOFF_SECRET']),
        'backup_enabled':env.get('SHOP_BACKUP_ENABLED')=='1',
        'data_under_var_data':Path(root).resolve().is_relative_to(Path('/var/data').resolve())}


def simulate():
    # Import lazily to avoid the existing checkout -> order_ops setup cycle.
    import checkout
    import shop
    import alimtalk
    checks=[]
    def require(condition,code):
        if not condition:raise ValueError(code)
    with tempfile.TemporaryDirectory(prefix='shop-diagnostic-') as temporary:
        path=Path(temporary)/'state.sqlite3'
        @contextmanager
        def db():
            c=sqlite3.connect(path);c.row_factory=sqlite3.Row
            try:
                with c:yield c
            finally:c.close()
        order_id='d'*32;offer='e'*32
        data={'title':'진단용 모의 상품','amount':93500,'options':[['상품 선택','쌀 10kg'],['축하 문구','축하합니다']],
              'sender':'모의 보내는 분','message':'진단용 문구','buyer':'모의 주문자','buyer_phone':'01000000000',
              'venue':'모의 행사장','address':'모의 주소','event_time':'2030-01-01T12:00:00+09:00','product_snapshot':{}}
        with db() as c:
            checkout.setup(c);shop.setup(c)
            c.execute("INSERT INTO shop_notification_settings VALUES (1,'')")
            c.execute('INSERT INTO shop_offers VALUES (?,?,1,93500,?,1)',(offer,'f'*32,json.dumps({})))
            c.execute('INSERT INTO invitation_orders VALUES (?,?,?,?,?,?,?,?)',(order_id,offer,93500,'2030-01-01T00:00:00+09:00',json.dumps(data),'waiting','1234',None))
        config={key:'diagnostic-mock-'+key for key in checkout.PAYAPP_FIELDS};when='20300101000000'
        base={'userid':config['USER_ID'],'linkkey':config['LINK_KEY'],'linkval':config['LINK_VALUE'],'responsedate':when,
              'hash':checkout.signature(config,when),'var1':order_id,'mul_no':'1234','price':'93500','pay_state':'4'}
        require(checkout.receive_payment(db,base,config),'mock_payment_callback')
        require(checkout.receive_payment(db,base,config),'mock_payment_callback')
        with db() as c:require(c.execute("SELECT count(*) FROM payment_events WHERE kind='paid'").fetchone()[0]==1,'mock_paid_duplicate')
        checks.append({'name':'payapp_paid_duplicate','result':'passed'})
        partial=dict(base,mul_no='9001',price='10000',orig_mul_no='1234',orig_price='93500',pay_state='70')
        require(checkout.receive_payment(db,partial,config),'mock_partial_callback');require(checkout.receive_payment(db,partial,config),'mock_partial_callback')
        with db() as c:
            require(c.execute('SELECT state FROM invitation_orders').fetchone()[0]=='partial_cancel','mock_partial_state')
            require(c.execute("SELECT sum(amount) FROM payment_events WHERE kind='partial'").fetchone()[0]==10000,'mock_partial_duplicate')
        checks.append({'name':'payapp_partial_duplicate','result':'passed'})
        require(checkout.receive_payment(db,dict(base,pay_state='9'),config),'mock_cancel_callback');require(checkout.receive_payment(db,base,config),'mock_payment_callback')
        with db() as c:require(c.execute('SELECT state FROM invitation_orders').fetchone()[0]=='cancelled','mock_cancel_state')
        checks.append({'name':'payapp_cancel_late_paid','result':'passed'})
        payload=alimtalk.message({'id':'mock-notice','order_id':order_id,'recipient':'01000000000','event':'confirmed','audience':'customer'},
                                {'id':order_id,'details':json.dumps(data)},settings={'TAESAN_PUBLIC_URL':'https://diagnostic.test','SOLAPI_KAKAO_PFID':'mock-pf','SHOP_ALIMTALK_TEMPLATE_ID':'mock-template'})
        require(payload['type']=='ATA' and payload['kakaoOptions']['disableSms'] is True,'mock_alimtalk_type')
        require(payload['kakaoOptions']['variables']['#{샘플링크}'].endswith('/receipt#sample'),'mock_alimtalk_sample_link')
        checks.append({'name':'alimtalk_template_sample_link','result':'passed'})
    return checks


def run(c,fields,root,actor='admin-link'):
    report={'scope':'isolated_local_mock','created':datetime.now(timezone.utc).isoformat(),'configuration':configuration(root),
            'checks':[],'external_requests':0,'live_payment':'not_run','live_messages':'not_run','live_cash_receipt':'not_run'}
    try:report['checks']=simulate();report['result']='passed'
    except Exception as error:report['result']='failed';report['error_type']=type(error).__name__
    c.execute('INSERT INTO shop_diagnostic_runs(created,actor,report) VALUES (?,?,?)',(report['created'],actor,json.dumps(report,ensure_ascii=False)))


def panel(c,path,root):
    config=configuration(root)
    result='<section><h2>외부 연동 진단</h2><p>키 값은 표시하지 않습니다. 설정 유무와 로컬 모의 규격 점검이며 실제 가맹점·단말 성공 여부는 별도입니다.</p>'
    for key in ('payapp','mms','alimtalk','handoff'):
        group=config[key];result+='<p>'+key+' · '+('설정 항목 있음' if group['configured'] else '설정 항목 확인 필요')+' · 실계정 검증 미실행</p>'
    result+=f'<form method="post" action="{escape(path)}"><input type="hidden" name="action" value="run_shop_diagnostics"><button>설정·모의 연동 진단</button></form>'
    row=c.execute('SELECT * FROM shop_diagnostic_runs ORDER BY id DESC LIMIT 1').fetchone()
    if row:
        report=json.loads(row['report']);result+='<p>'+escape(row['created'][:16]+' · '+('모의 검증 통과' if report['result']=='passed' else '모의 검증 확인 필요'))+'</p>'
        result+='<p><a href="'+escape(path)+'/diagnostics.json?run='+str(row['id'])+'">진단 리포트 JSON</a></p>'
    return result+'</section>'
