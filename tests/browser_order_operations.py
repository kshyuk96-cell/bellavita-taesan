"""Browser verification of staff delivery links and customer order tracking.

Only use the isolated serve_shop.py environment; it disables real sends/issuance.
"""
import json
from pathlib import Path
import sqlite3

from playwright.sync_api import sync_playwright
from browser_shop import ROOT, LINKS, ARTIFACTS, notify


def main():
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        context=browser.new_context(ignore_https_errors=True,viewport={'width':375,'height':900})
        context.route('**/*',lambda route:route.continue_() if route.request.url.startswith('https://localhost/') else route.abort())
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(LINKS['admin'])
        page.locator('textarea[name=staff_phones]').fill('01022223333')
        page.get_by_role('button',name='직원 알림 번호 저장',exact=True).click()
        page.wait_for_load_state()
        page.goto(LINKS['orders']['93500'])
        for index,value in enumerate(('2','custom','1')):
            page.locator('.mobile-choice').nth(index).locator('summary').click()
            page.locator(f'[name=choice{index}]').select_option(value)
        page.locator('[name=message]').fill('두 분의 행복한 결혼을 축하합니다\n오래오래 행복하세요')
        page.locator('[name=sender]').fill('벨라비타 대표 홍길동')
        page.locator('[name=same_buyer]').check()
        page.locator('[name=buyer_phone]').fill('01012345678')
        page.locator('[name=consent]').check()
        page.locator('[name=cash_receipt_type]').select_option('business')
        assert page.locator('[name=cash_receipt_identifier]').get_attribute('required') is not None
        page.locator('[name=cash_receipt_identifier]').fill('1234567890')
        page.locator('button[type=submit]').click();page.wait_for_url('**/receipt')
        receipt=page.url;order_id=receipt.split('/')[-2];number=str(int(order_id[:12],16))
        import datetime as dt
        notify(order_id,number,'4',103500,pay_type='6',pay_date=dt.datetime.now().strftime('%Y%m%d%H%M%S'))
        page.reload();assert '주문 확인' in page.locator('main').inner_text()
        page.wait_for_function('document.querySelector("img").naturalWidth===900')
        page.screenshot(path=str(ARTIFACTS/'customer-order-status.png'),full_page=True)
        page.goto(LINKS['admin'])
        row=page.locator('li').filter(has_text=order_id)
        row.locator('summary').first.click()
        link=row.get_by_role('link',name='직원용 주문·배송 링크 (24시간)',exact=True).get_attribute('href')
        page.goto('https://localhost'+link)
        page.wait_for_function('document.querySelector("img").naturalWidth===900')
        page.get_by_role('button',name='배송 중으로 변경',exact=True).click()
        page.wait_for_load_state()
        page.get_by_role('button',name='배송 완료로 변경',exact=True).click()
        page.wait_for_load_state()
        assert '주문·배송: 배송 완료' in page.locator('main').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(ARTIFACTS/'staff-delivery-completed.png'),full_page=True)
        page.goto(receipt)
        assert '배송 완료' in page.locator('h2').inner_text()
        assert '현금영수증' in page.locator('main').inner_text()
        with sqlite3.connect(ROOT/'state.sqlite3') as c:
            count=c.execute('SELECT COUNT(*) FROM shop_messages WHERE order_id=?',(order_id,)).fetchone()[0]
            assert count==6,count
            stage=c.execute('SELECT stage FROM shop_fulfillment WHERE order_id=?',(order_id,)).fetchone()[0]
            assert stage=='delivered'
            saved=json.loads(c.execute('SELECT details FROM invitation_orders WHERE id=?',(order_id,)).fetchone()[0])
            assert saved['cash_receipt']=={'kind':'business','identifier':'1234567890'}
        assert not errors,errors
        results={'order_id':order_id,'stage':stage,'staff_and_buyer_notifications_queued':count,
                 'private_sample_image_size':[900,1200],'cash_receipt_request_saved':True,
                 'actual_messages_sent':False,'actual_receipts_issued':False,'javascript_errors':errors}
        (ARTIFACTS/'order-operations-browser-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(json.dumps(results,ensure_ascii=False,indent=2));browser.close()


if __name__=='__main__':
    main()
