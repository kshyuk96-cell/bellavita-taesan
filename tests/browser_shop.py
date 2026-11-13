"""Run against serve_shop.py only: Chromium UI + mock signed notifications."""
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(os.getenv('SHOP_TEST_DATA', '/tmp/bellavita-shop-demo'))
ARTIFACTS = Path(__file__).resolve().parents[1] / 'test-artifacts'
ARTIFACTS.mkdir(exist_ok=True)
LINKS = json.loads((ROOT / 'test-links.json').read_text())
CONTEXT = ssl.create_default_context(cafile='/tmp/bellavita-test.crt')


def notify(order_id, number, state, amount, **extra):
    config = {key: 'isolated-demo-' + key.lower() for key in ('USER_ID', 'LINK_KEY', 'LINK_VALUE', 'REQUEST_KEY')}
    when = dt.datetime.now().strftime('%Y%m%d%H%M%S')
    fields = dict(userid=config['USER_ID'], linkkey=config['LINK_KEY'], linkval=config['LINK_VALUE'],
                  hash=hashlib.sha256((config['USER_ID'] + when + config['REQUEST_KEY']).encode()).hexdigest(),
                  responsedate=when, var1=order_id, mul_no=number, pay_state=state, price=str(amount), **extra)
    client = http.client.HTTPSConnection('localhost', context=CONTEXT)
    client.request('POST', '/invitation-orders/payapp', urlencode(fields),
                   {'Content-Type': 'application/x-www-form-urlencoded'})
    response = client.getresponse()
    assert response.status == 200, response.read()
    assert response.read() == b'SUCCESS'
    client.close()


def run():
    results = {'viewports': [], 'orders': [], 'errors': []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                                            args=['--no-sandbox'])
        context = browser.new_context(ignore_https_errors=True)
        # Keep the UI test independent of external photos, font CDNs, and payment sites.
        context.route('**/*', lambda route: route.continue_() if urlsplit(route.request.url).hostname == 'localhost'
                      else route.abort())
        page = context.new_page()
        page.on('pageerror', lambda error: results['errors'].append(str(error)))
        for width in (320, 375, 430, 1024):
            page.set_viewport_size({'width': width, 'height': 900})
            page.goto(LINKS['orders']['93500'])
            page.wait_for_function('document.querySelector(".scroll-couple").naturalWidth > 0')
            for index, value in enumerate(('3', 'custom', '1')):
                if width < 671:
                    page.locator('.mobile-choice').nth(index).locator('summary').click()
                page.locator(f'[name=choice{index}]').select_option(value)
            assert page.locator('#amount').inner_text() == '113,500원'
            page.locator('[name=buyer]').fill('별도 주문자')
            sender = '벨라비타 행복한 출발을 응원하는 전국모임 대표 홍길동'
            page.locator('[name=sender]').fill(sender)
            page.locator('[name=same_buyer]').check()
            assert page.locator('[name=buyer]').input_value() == sender
            page.locator('[name=sender]').fill(sender + ' 일동')
            assert page.locator('[name=buyer]').input_value() == sender + ' 일동'
            page.locator('[name=same_buyer]').uncheck()
            assert page.locator('[name=buyer]').input_value() == '별도 주문자'
            long_message = ('행복한결혼을진심으로축하합니다' * 14)[:200]
            page.locator('[name=message]').fill(long_message)
            page.wait_for_timeout(350)
            if width < 671:
                page.locator('#open-mobile-example').click()
            page.wait_for_function('document.querySelector("#scroll-weight").alt === "쌀 30kg"')
            measurements = page.evaluate('''() => {
              const box = el => { const r=el.getBoundingClientRect(); return {left:r.left,right:r.right,top:r.top,bottom:r.bottom}; };
              const inside = (a,b) => a.left >= b.left-1 && a.right <= b.right+1 && a.top >= b.top-1 && a.bottom <= b.bottom+1;
              const greeting = document.querySelector('#scroll-greeting');
              const sender = document.querySelector('#scroll-sender');
              const rows = Array.from(greeting.querySelectorAll('svg text, span'));
              const canvas = greeting.querySelector('canvas');
              let canvasFits = false;
              if (canvas) {
                const pixels = canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
                let painted = false, clipped = false;
                for (let y=0;y<canvas.height;y++) for(let x=0;x<canvas.width;x++) {
                  if (pixels[(y*canvas.width+x)*4+3]) {
                    painted=true;
                    if(x<3 || x>=canvas.width-3 || y<3 || y>=canvas.height-3) clipped=true;
                  }
                }
                canvasFits = painted && !clipped && inside(box(canvas),box(greeting)) &&
                  canvas.getAttribute('aria-label').length === 200;
              }
              return {overflow:document.documentElement.scrollWidth > innerWidth,
                greetingFits: canvasFits || (rows.length > 0 && rows.every(row=>inside(box(row),box(greeting)))),
                senderFits:Array.from(sender.children).every(row=>{const r=document.createRange();r.selectNodeContents(row);return inside(r.getBoundingClientRect(),box(sender));}),
                greeting:box(greeting), rows:rows.map(box)};
            }''')
            page.screenshot(path=str(ARTIFACTS / f'checkout-{width}.png'), full_page=True)
            assert not measurements['overflow'], measurements
            assert measurements['greetingFits'], measurements
            assert measurements['senderFits'], measurements
            results['viewports'].append({'width': width, 'long_message_characters': len(long_message),
                                         'greeting_fits': True, 'sender_fits': True, 'no_horizontal_overflow': True})
            if width < 671:
                page.locator('#mobile-example form button').click()

        # A broken wedding photo must leave the ordinary product gallery usable.
        page.route('**/rice-10kg.webp', lambda route: route.abort())
        page.goto(LINKS['orders']['93500'])
        page.wait_for_function('document.querySelector("#wreath-preview").hidden')
        assert page.locator('#wreath-fallback').is_visible()
        page.unroute('**/rice-10kg.webp')
        results['broken_photo_fallback'] = True

        page.set_viewport_size({'width': 1024, 'height': 900})
        for price in ('93500', '99000'):
            page.goto(LINKS['orders'][price])
            for index, value in enumerate(('1', '1', '1')):
                page.locator(f'[name=choice{index}]').select_option(value)
            page.locator('[name=sender]').fill('검증용 보내는 분')
            page.locator('[name=same_buyer]').check()
            page.locator('[name=buyer_phone]').fill('01012345678')
            page.locator('[name=consent]').check()
            page.locator('button[type=submit]').click()
            page.wait_for_url('**/receipt')
            order_id = page.url.split('/')[-2]
            number = str(int(order_id[:12], 16))
            notify(order_id, number, '4', price)
            if price == '93500':
                for state in ('70', '71', '70'):
                    notify(order_id, '9999001', state, 10000, orig_mul_no=number, orig_price=price)
                notify(order_id, number, '4', price)
            else:
                notify(order_id, number, '9', price)
                notify(order_id, number, '4', price)
            page.reload()
            expected = '일부가 취소' if price == '93500' else '결제가 취소'
            assert expected in page.locator('main').inner_text()
            results['orders'].append({'price': int(price), 'order_id': order_id, 'receipt': page.url,
                                      'state': 'partial_cancel' if price == '93500' else 'cancelled'})
        page.goto(LINKS['admin'])
        for order in results['orders']:
            assert order['order_id'] in page.locator('main').inner_text()
        for item in page.locator('section:last-child li > details').all():
            item.locator('summary').click()
        assert '10,000원' in page.locator('main').inner_text()
        assert '83,500원' in page.locator('main').inner_text()
        page.screenshot(path=str(ARTIFACTS / 'shop-admin.png'), full_page=True)
        assert not results['errors'], results['errors']
        browser.close()
    (ARTIFACTS / 'browser-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    run()
