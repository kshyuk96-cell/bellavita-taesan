"""Independent Render verification entrypoint. No merchant or messaging requests."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit


def configure():
    if os.getenv('SHOP_VERIFICATION_MODE')!='1':raise RuntimeError('검증 전용 실행은 SHOP_VERIFICATION_MODE=1이 필요합니다.')
    if not os.getenv('TAESAN_PUBLIC_URL'):os.environ['TAESAN_PUBLIC_URL']=os.getenv('RENDER_EXTERNAL_URL','')
    root=Path(os.environ['TAESAN_DATA']).resolve()
    if os.getenv('SHOP_REQUIRE_PERSISTENT_DISK')=='1' and (not root.is_relative_to(Path('/var/data')) or not os.path.ismount('/var/data')):
        raise RuntimeError('검증 데이터의 /var/data 영구 디스크 마운트를 확인하세요.')
    if len(os.getenv('TAESAN_WEBHOOK_SECRET',''))<32:raise RuntimeError('32자 이상 검증용 비밀키가 필요합니다.')
    # Override inherited live settings before application imports. Never copy production credentials.
    os.environ.update(TAESAN_LIVE_WRITE='0',SHOP_MESSAGES_ENABLED='0',SHOP_ALIMTALK_ENABLED='0',
        SHOP_CASH_RECEIPT_ENABLED='0',SHOP_HANDOFF_ENABLED='0',SHOP_CHECKOUT_ENABLED='1',
        INVITATION_CHECKOUT_ENABLED='0',OPENAI_API_KEY='',KAKAO_CLIENT_ID='',
        TAESAN_STOCK_WRITERS='',SHOP_STAFF_PHONES='')
    for key in ('USER_ID','LINK_KEY','LINK_VALUE','REQUEST_KEY'):os.environ['INVITATION_PAYAPP_'+key]='isolated-verification-'+key.lower()
    import agent
    import checkout
    import shop
    import shop_catalog
    def mock_pay(config,order_id,order,origin):
        number=str(int(order_id[:12],16))
        return number,origin+'/verification/pay/'+order_id+'/'+token(order_id)
    checkout.payapp_request=mock_pay
    with agent.db() as c:shop_catalog.seed(c)
    return agent,checkout,shop


def token(order_id):return hmac.new(os.environ['TAESAN_WEBHOOK_SECRET'].encode(),('verification-payment:'+order_id).encode(),hashlib.sha256).hexdigest()


def handler(agent,checkout):
    import datetime as dt
    import re
    class VerificationHandler(agent.Handler):
        def do_GET(self):
            path=urlsplit(self.path).path
            if path=='/verification':return self.send_json({'mode':'isolated_mock','live_payment':False,'live_messages':False,'live_receipts':False,'storefront':'/shop','data_under_var_data':agent.ROOT.is_relative_to(Path('/var/data')),'mount_verified':os.path.ismount('/var/data')})
            match=re.fullmatch(r'/verification/pay/([a-f0-9]{32})/([a-f0-9]{64})',path)
            if match:
                if not hmac.compare_digest(token(match[1]),match[2]):return self.order_message('검증 링크가 올바르지 않습니다.',403)
                return self.send_order_html('<!doctype html><html lang="ko"><meta name="viewport" content="width=device-width,initial-scale=1"><h1>페이앱 모의 결제</h1><p>실제 결제·영수증·메시지 전송이 없습니다.</p><form method="post"><button name="state" value="4">모의 결제 완료</button><button name="state" value="70">모의 부분취소 10,000원</button><button name="state" value="64">모의 전체취소</button></form><p><a href="/invitation-orders/'+match[1]+'/receipt">주문 상태 확인</a></p></html>')
            return super().do_GET()
        def do_POST(self):
            match=re.fullmatch(r'/verification/pay/([a-f0-9]{32})/([a-f0-9]{64})',urlsplit(self.path).path)
            if not match:return super().do_POST()
            origin=os.getenv('TAESAN_PUBLIC_URL','').rstrip('/')
            if not hmac.compare_digest(token(match[1]),match[2]) or self.headers.get('Origin')!=origin:return self.order_message('검증 링크를 확인하세요.',403)
            fields=self.read_order_form();state=fields.get('state');config=checkout.payment_config()
            if state not in ('4','70','64'):return self.order_message('검증 동작을 확인하세요.',422)
            with agent.db() as c:row=c.execute('SELECT * FROM invitation_orders WHERE id=?',(match[1],)).fetchone()
            if not row or not row['payment_number']:return self.order_message('검증 주문이 없습니다.',404)
            order=json.loads(row['details']);date=dt.datetime.now(checkout.SEOUL).strftime('%Y%m%d%H%M%S')
            event=dict(userid=config['USER_ID'],linkkey=config['LINK_KEY'],linkval=config['LINK_VALUE'],hash=checkout.signature(config,date),responsedate=date,var1=row['id'],mul_no=row['payment_number'],price=str(order['amount']),pay_state=state)
            if state=='70':event.update(mul_no=str(int(row['payment_number'])+1),price='10000',orig_mul_no=row['payment_number'],orig_price=str(order['amount']))
            if not checkout.receive_payment(agent.db,event,config):return self.order_message('모의 통보 대조 실패',422)
            self.send_response(303);self.send_header('Location','/invitation-orders/'+row['id']+'/receipt');self.send_header('Cache-Control','no-store');self.end_headers()
    return VerificationHandler


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--admin-link',action='store_true');args=parser.parse_args()
    agent,checkout,shop=configure()
    if args.admin_link:
        expiry=str(int(time.time())+600)
        print(os.environ['TAESAN_PUBLIC_URL'].rstrip('/')+'/shop/admin/'+expiry+'/'+shop.admin_signature(expiry,os.environ['TAESAN_WEBHOOK_SECRET']))
        return
    # Local backup snapshots; external off-service backup storage remains a separate operational setting.
    agent.order_ops.start(agent.db,agent.ROOT)
    agent.ThreadingHTTPServer((os.getenv('TAESAN_BIND','0.0.0.0'),int(os.getenv('PORT','10000'))),handler(agent,checkout)).serve_forever()


if __name__=='__main__':main()
