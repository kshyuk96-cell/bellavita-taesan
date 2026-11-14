"""SOLAPI ATA template adapter. Dynamic order proofs are linked, not template images."""
import json
import os
from urllib.parse import urlsplit


def configured():
    return os.getenv('SHOP_ALIMTALK_ENABLED')=='1' and all(os.getenv(k) for k in ('SOLAPI_KAKAO_PFID','SHOP_ALIMTALK_TEMPLATE_ID'))


def message(job,order):
    import order_ops
    origin=os.getenv('TAESAN_PUBLIC_URL','').rstrip('/')
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('알림톡에는 HTTPS 주문 상태 주소가 필요합니다.')
    data=json.loads(order['details'])
    receipt=origin+'/invitation-orders/'+order['id']+'/receipt'
    event=job['event'];group=event.split(':')[0]
    template=os.getenv('SHOP_ALIMTALK_TEMPLATE_'+group.upper(),os.environ['SHOP_ALIMTALK_TEMPLATE_ID'])
    status=order_ops.STAGES.get(event,'잔액 대조 후 재개' if event.startswith('resumed:') else '결제 일부 취소' if event.startswith('refund:') else '결제 취소')
    tracking=(origin+order_ops.signed_path('delivery',order['id'],os.environ['TAESAN_WEBHOOK_SECRET'],86400)
              if job['audience']=='staff' else receipt)
    return {'to':job['recipient'],'type':'ATA','kakaoOptions':{
        'pfId':os.environ['SOLAPI_KAKAO_PFID'],'templateId':template,'disableSms':True,
        'variables':{'#{주문번호}':order['id'],'#{상태}':status,'#{상품명}':data['title'],
                     '#{샘플링크}':receipt+'#sample','#{주문링크}':tracking}},
        'customFields':{'orderId':job['order_id'],'notificationId':job['id']}}
