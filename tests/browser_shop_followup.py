"""Actual core Python administrator UI, isolated HTTP, no real providers."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from test_shop_followup import FollowupTest
from test_shop import ORIGIN


def main():
    fixture=FollowupTest('test_file_review_gate_download_integrity_and_one_factory_row');fixture.setUp()
    try:
        fixture.pay();upstream='http://127.0.0.1:'+str(fixture.server.server_address[1]);errors=[]
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            context=browser.new_context(viewport={'width':375,'height':900})
            def route(request):
                if not request.request.url.startswith(ORIGIN+'/'):return request.abort()
                headers=dict(request.request.headers)
                if request.request.method=='POST':headers['origin']=ORIGIN
                response=request.fetch(url=upstream+request.request.url[len(ORIGIN):],headers=headers);request.fulfill(response=response)
            context.route('**/*',route)
            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(ORIGIN+fixture.admin)
            page.get_by_role('button',name='제작 보류·검수 대기 1건',exact=True).click()
            row=page.locator('[data-shop-order]').filter(has_text='a'*32)
            self_details=row.locator('details').first;self_details.locator('summary').first.click()
            assert row.get_by_role('button',name='배송 중으로 변경',exact=True).count()==0
            row.locator('[name=width_mm]').fill('100');row.locator('[name=height_mm]').fill('300')
            row.get_by_role('button',name='제작 파일 생성',exact=True).click();page.wait_for_load_state()
            row=page.locator('[data-shop-order]').filter(has_text='a'*32);row.locator('summary').first.click()
            assert '검수 대기' in row.inner_text()
            link=row.get_by_role('link',name='버전 1 PNG 확인',exact=True).get_attribute('href')
            png=context.request.get(upstream+link);assert png.status==200 and png.body().startswith(b'\x89PNG')
            row.locator('[name=artifact_reviewer]').fill('모바일 검수 담당자')
            row.locator('[name=artifact_note]').fill('문구·보내는 분·쌀 중량·100×300mm 검수')
            row.locator('[name=artifact_compared]').check()
            row.get_by_role('button',name='제작 검수 저장',exact=True).click();page.wait_for_load_state()
            row=page.locator('[data-shop-order]').filter(has_text='a'*32);row.locator('summary').first.click()
            assert '검수 승인' in row.inner_text();assert row.get_by_role('button',name='배송 중으로 변경',exact=True).is_visible()
            page.locator('#shop-order-search').fill('없는주문');assert not row.is_visible()
            page.locator('#shop-order-search').fill('a'*32);assert row.is_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            artifacts=Path('test-artifacts');artifacts.mkdir(exist_ok=True)
            page.screenshot(path=str(artifacts/'followup-mobile-review.png'),full_page=True)
            assert not errors,errors;browser.close()
        result={'mobile_width':375,'production_generate_review':'passed','approved_version_png':True,'dashboard_filter_search':True,'overflow':False,'javascript_errors':errors,'actual_sends':False}
        (Path('test-artifacts')/'shop-followup-browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
    finally:fixture.tearDown()


if __name__=='__main__':main()
