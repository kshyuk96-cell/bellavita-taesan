"""Photo review -> authenticated factory receiver -> cancellation hold, test data only."""
import json
import os
from pathlib import Path
import sqlite3
import sys

from playwright.sync_api import sync_playwright
from browser_shop import ROOT, LINKS, ARTIFACTS, notify

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import shop_receiver


def main():
    target=Path(os.getenv('SHOP_RECEIVER_TEST_DATA','/tmp/bellavita-factory-browser'))
    key='isolated-demo-factory-handoff-'*2
    transport=lambda action,fields:shop_receiver.call('https://localhost',key,action,fields,'/tmp/bellavita-test.crt')
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        context=browser.new_context(ignore_https_errors=True,viewport={'width':375,'height':900})
        context.route('**/*',lambda route:route.continue_() if route.request.url.startswith('https://localhost/') else route.abort())
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(LINKS['orders']['93500'])
        for index,value in enumerate(('1','1','1')):
            page.locator('.mobile-choice').nth(index).locator('summary').click()
            page.locator(f'[name=choice{index}]').select_option(value)
        page.locator('[name=sender]').fill('후속 접수 검증 홍길동')
        page.locator('[name=same_buyer]').check()
        page.locator('[name=buyer_phone]').fill('01012345678')
        page.locator('[name=consent]').check();page.locator('button[type=submit]').click();page.wait_for_url('**/receipt')
        order_id=page.url.split('/')[-2];number=str(int(order_id[:12],16))
        notify(order_id,number,'4',93500)
        page.goto(LINKS['admin'])
        row=page.locator('li').filter(has_text=order_id);row.locator('summary').first.click()
        assert '사진 검토 후 제작 접수' in row.inner_text()
        review=row.locator('.shop-photo-review');review.locator('summary').click()
        review.locator('img').wait_for(state='visible')
        page.wait_for_function('document.querySelector(".shop-photo-review[open] img").naturalWidth>0')
        review.locator('[name=both_faces]').check()
        review.get_by_role('button',name='사진 검토 완료·제작 접수',exact=True).click();page.wait_for_load_state()
        assert shop_receiver.run_once(target,'factory-browser',transport)
        page.reload();row=page.locator('li').filter(has_text=order_id);row.locator('summary').first.click()
        assert '원장·제작 대기열 접수 확인' in row.inner_text()
        page.screenshot(path=str(ARTIFACTS/'handoff-admin-accepted.png'),full_page=True)
        with sqlite3.connect(target/'receiver.sqlite3') as c:
            assert c.execute('SELECT COUNT(*) FROM production_queue WHERE order_id=?',(order_id,)).fetchone()[0]==1
            assert c.execute('SELECT state FROM production_queue WHERE order_id=?',(order_id,)).fetchone()[0]=='pending'
        notify(order_id,number,'9',93500)
        assert shop_receiver.run_once(target,'factory-browser',transport)
        with sqlite3.connect(target/'receiver.sqlite3') as c:
            assert c.execute('SELECT state FROM production_queue WHERE order_id=?',(order_id,)).fetchone()[0]=='held'
            payment=json.loads(c.execute('SELECT payment FROM order_ledger WHERE order_id=?',(order_id,)).fetchone()[0])
            assert payment['refunded_amount']==93500 and payment['remaining_amount']==0
        assert not errors,errors
        result={'order_id':order_id,'photo_reviewed':True,'ledger_rows':1,'production_rows':1,
                'received_state':'pending','after_cancel':'held','remaining_amount':0,
                'actual_printing':False,'live_sheet_writes':False,'javascript_errors':errors}
        (ARTIFACTS/'handoff-browser-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False,indent=2));browser.close()


if __name__=='__main__':
    main()
