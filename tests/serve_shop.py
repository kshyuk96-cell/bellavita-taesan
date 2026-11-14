"""Isolated HTTPS demo. Synthetic data and PayApp stub; never real payment.

python tests/serve_shop.py --data /tmp/bellavita-demo --cert /tmp/test.crt --key /tmp/test.key
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import ssl
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--cert', required=True)
    parser.add_argument('--key', required=True)
    args = parser.parse_args()
    root = Path(args.data).resolve()
    root.mkdir(parents=True, exist_ok=True)
    secret_path = root / 'test-secret'
    if not secret_path.exists():
        secret_path.write_text(secrets.token_hex(32))
        secret_path.chmod(0o600)
    secret = secret_path.read_text()
    # Override any inherited production settings before importing the application.
    os.environ.update(TAESAN_DATA=str(root), TAESAN_PUBLIC_URL='https://localhost',
                      TAESAN_WEBHOOK_SECRET=secret, TAESAN_LIVE_WRITE='0', SHOP_CHECKOUT_ENABLED='1',
                      INVITATION_CHECKOUT_ENABLED='0', KAKAO_CLIENT_ID='',
                      SHOP_MESSAGES_ENABLED='0', SHOP_CASH_RECEIPT_ENABLED='0',
                      SHOP_HANDOFF_ENABLED='1', SHOP_HANDOFF_SECRET='isolated-demo-factory-handoff-'*2,
                      TAESAN_ADMIN_IDS='test-admin', TAESAN_STAFF_IDS='test-staff', OPENAI_API_KEY='')
    for key in ('USER_ID', 'LINK_KEY', 'LINK_VALUE', 'REQUEST_KEY'):
        os.environ['INVITATION_PAYAPP_' + key] = 'isolated-demo-' + key.lower()
    import agent
    import checkout
    import shop

    def mock_request(config, order_id, order, origin):
        number = str(int(order_id[:12], 16))
        # Stub returns the local receipt. No HTTP request reaches PayApp.
        return number, origin + '/invitation-orders/' + order_id + '/receipt'
    checkout.payapp_request = mock_request
    with agent.db() as connection:
        if not connection.execute('SELECT 1 FROM shop_products').fetchone():
            shop.save_product(connection, dict(title='검증용 쌀빛그림화환 · 모의 결제', price='93500',
                images='https://localhost/checkout-art/sunflower-easel.webp',
                options='쌀 10kg | 0\n쌀 20kg | 10000\n쌀 30kg | 20000',
                greetings='결혼을 진심으로 축하합니다\n행복한 출발을 축하합니다'))
            product = connection.execute('SELECT * FROM shop_products').fetchone()
            shop.issue_offers(connection, dict(product_id=product['id'], revision='1',
                event='독립 검증 예식', when=(dt.datetime.now(checkout.SEOUL) + dt.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M'),
                kind='wedding', venue='검증 홀', address='검증 전용 설치 주소', prices='93500,99000',
                wedding_photo='https://localhost/checkout-art/rice-10kg.webp'))
        offers = connection.execute('SELECT id,price FROM shop_offers ORDER BY price').fetchall()
    expiry = str(int(time.time()) + 600)
    links = {'admin': f'https://localhost/shop/admin/{expiry}/{shop.admin_signature(expiry, secret)}',
             'orders': {str(row['price']): f'https://localhost/shop/order/{row["id"]}' for row in offers},
             'payment': 'mock only', 'data': str(root)}
    (root / 'test-links.json').write_text(json.dumps(links, ensure_ascii=False, indent=2))
    print(json.dumps(links, ensure_ascii=False), flush=True)
    server = agent.ThreadingHTTPServer(('127.0.0.1', 443), agent.Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
