"""Isolated core Python UI verification; all financial/provider traffic is mocked."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from test_partial_reviews import PartialReviewTest
from test_shop import ORIGIN
import agent
import manage_data


def main():
    fixture=PartialReviewTest('test_partial_resume_keeps_finances_and_releases_once')
    fixture.setUp()
    try:
        fixture.pay();fixture.cancel_part()
        upstream='http://127.0.0.1:'+str(fixture.server.server_address[1])
        origin=ORIGIN
        errors=[]
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            context=browser.new_context(viewport={'width':375,'height':900})
            def route(request):
                if not request.request.url.startswith(origin+'/'):return request.abort()
                headers=dict(request.request.headers)
                if request.request.method=='POST':headers['origin']=ORIGIN
                response=request.fetch(url=upstream+request.request.url[len(origin):],headers=headers)
                request.fulfill(response=response)
            context.route('**/*',route)
            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(origin+fixture.admin)
            row=page.locator('li').filter(has_text='a'*32);row.locator('summary').first.click()
            assert row.get_by_role('button',name='배송 중으로 변경').count()==0
            row.locator('[name=partial_remaining]').fill('83500')
            row.locator('[name=partial_reviewer]').fill('모바일 검증자')
            row.locator('[name=partial_note]').fill('페이앱 모의 결제번호 1234 잔액 83500원 대조')
            row.locator('[name=partial_decision]').select_option('resume')
            row.locator('[name=partial_compared]').check()
            row.get_by_role('button',name='대조 결과 저장',exact=True).click();page.wait_for_load_state()
            assert page.locator('li').filter(has_text='a'*32).count(), page.locator('body').inner_text()
            row=page.locator('li').filter(has_text='a'*32);row.locator('summary').first.click()
            row.get_by_role('button',name='배송 중으로 변경',exact=True).click();page.wait_for_load_state()
            assert page.locator('li').filter(has_text='a'*32).count(), page.locator('body').inner_text()
            row=page.locator('li').filter(has_text='a'*32);row.locator('summary').first.click()
            assert '주문·배송: 배송 중' in row.inner_text()
            assert '모바일 검증자' in row.inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            artifacts=Path('test-artifacts');artifacts.mkdir(exist_ok=True)
            page.screenshot(path=str(artifacts/'partial-review-mobile.png'),full_page=True)
            fixture.cancel_part('9002');page.goto(origin+fixture.admin)
            row=page.locator('li').filter(has_text='a'*32);row.locator('summary').first.click()
            assert row.get_by_role('button',name='배송 완료로 변경').count()==0
            assert not errors,errors
            browser.close()
        # Fresh copy and fresh DB connection simulate a process replacement.
        snapshot=fixture.root.parent/'partial-reviewed-backup';restored=fixture.root.parent/'partial-reviewed-restored'
        manage_data.transfer(fixture.root,snapshot);manage_data.transfer(snapshot,restored,restore=True)
        import sqlite3
        with sqlite3.connect(restored/'state.sqlite3') as c:
            assert c.execute('SELECT count(*) FROM shop_partial_reviews').fetchone()[0]==1
            assert c.execute('SELECT state FROM invitation_orders').fetchone()[0]=='partial_cancel'
        result={'mobile_width':375,'admin_partial_review':'passed','shipping_after_resume':True,'new_refund_holds':True,'audit_backup_restore':True,'javascript_errors':errors,'actual_payment':False,'actual_messages':False}
        (Path('test-artifacts')/'partial-review-browser-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False))
    finally:fixture.tearDown()


if __name__=='__main__':main()
