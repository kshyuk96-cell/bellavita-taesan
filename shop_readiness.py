"""Read-only merchant launch preparation; never sends to providers."""
from html import escape
import json
import os
import order_ops
import external_backups


def report(c):
    import checkout
    config=checkout.payment_config();mock=any(v.startswith(('isolated-','diagnostic-','test-')) for v in config.values())
    live_merchant=all(config.values()) and not mock
    products=[]
    for row in c.execute('SELECT id,details FROM shop_products'):
        p=json.loads(row['details'])
        if p.get('public'):products.append({'id':row['id'],'title':p['title'],'tax_type':p.get('cash_tax_type','')})
    missing_tax=[p['title'] for p in products if p['tax_type'] not in ('TG01','TG02')]
    alimtalk_fields=['SOLAPI_API_KEY','SOLAPI_API_SECRET','SOLAPI_KAKAO_PFID','SHOP_ALIMTALK_TEMPLATE_ID']
    origin=os.getenv('TAESAN_PUBLIC_URL','').rstrip('/')
    latest=c.execute('SELECT verified,mode FROM shop_external_backups ORDER BY verified DESC LIMIT 1').fetchone()
    return {'external_backup_last_verified':dict(latest) if latest else None,'scope':'configuration_only','live_validation':'not_run','payapp':{'merchant_keys_present':bool(live_merchant),'mock_adapter':mock,'enabled':os.getenv('SHOP_CHECKOUT_ENABLED')=='1','callback_path':'/invitation-orders/payapp','steps':['가맹점 제공 테스트 수단 확인','주문번호·원금액·결제번호 대조','승인·중복·부분취소·전체취소·타임아웃 대조']},
        'alimtalk':{'enabled':os.getenv('SHOP_MESSAGES_ENABLED')=='1' and os.getenv('SHOP_ALIMTALK_ENABLED')=='1','missing_fields':[k for k in alimtalk_fields if not os.getenv(k)],'channel':os.getenv('SHOP_MESSAGE_CHANNEL','mms'),'template_approval_validation':'not_run','route_selected':os.getenv('SHOP_MESSAGE_CHANNEL')=='alimtalk','templates':['confirmed','shipping','delivered','refund','cancel'],'variables':['#{주문번호}','#{상태}','#{상품명}','#{샘플링크}','#{주문링크}'],'sample_delivery':'주문별 이미지 확인 링크','sms_fallback':False,'staff_recipients_configured':bool(order_ops.phones(c))},
        'cash_receipt':{'enabled':os.getenv('SHOP_CASH_RECEIPT_ENABLED')=='1','owner_configured':os.getenv('SHOP_CASH_RECEIPT_OWNER')=='shop','products_missing_tax_type':missing_tax,'eligible_payment_types':['6','7'],'duplicate_owner_warning':'페이앱·쇼핑몰 중 발급 주체 하나만 설정','old_order_policy':'주문 당시 과세 구분 보존, 기존 주문은 별도 대조'},'external_backup':external_backups.configuration(),'secrets_exposed':False,'external_requests':0}


def panel(c):
    r=report(c)
    rows=[('페이앱 가맹점','실가맹점 키 항목 있음' if r['payapp']['merchant_keys_present'] else '실가맹점 설정 필요 · 현재 모의 검증'),('알림톡','발송 꺼짐 / 필수 항목 '+str(len(r['alimtalk']['missing_fields']))+'개 확인 필요' if not r['alimtalk']['enabled'] else '활성 설정 · 실제 수신 검증 별도'),('문구 샘플','알림에 주문별 이미지 확인 링크 포함 · 문자 대체 발송 꺼짐'),('현금영수증','과세·면세 확인 필요 상품 '+str(len(r['cash_receipt']['products_missing_tax_type']))+'개 · 발급 주체 대조 필요'),('외부 자동 백업','서버 자동 업로드 설정 있음' if r['external_backup']['configured'] else '서버 자동 업로드 인증 설정 필요')]
    external_note='<p>외부 백업 최근 검증: '+escape(r['external_backup_last_verified']['verified'][:16])+' · '+('수동 저장·복구 검증' if r['external_backup_last_verified']['mode']=='manual_verified' else '서버 업로드·복구 검증')+'</p>' if r['external_backup_last_verified'] else '<p>외부 저장·복구 검증 이력 없음</p>'
    return '<section><h2>운영 연결 준비</h2><p>설정 확인 화면입니다. 실제 결제·발송·발급 성공을 의미하지 않습니다. 비밀키 값은 표시하지 않습니다.</p><dl>'+''.join('<dt>'+escape(k)+'</dt><dd>'+escape(v)+'</dd>' for k,v in rows)+'</dl>'+external_note+'<p>현금영수증은 계좌이체·가상계좌 등 대상 결제수단과 과세 구분을 확인합니다. 카드·결제사 발급과 중복 발급하지 않습니다.</p></section>'
