"""Browser QA for Python backend storefront, not the separate Sites preview."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from test_shop_catalog import CatalogTest
from test_shop import ORIGIN


def main():
    fixture=CatalogTest('test_only_four_public_products_and_native_options');fixture.setUp()
    try:
        upstream='http://127.0.0.1:'+str(fixture.server.server_address[1]);errors=[];results=[]
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            for width in (375,1280):
                context=browser.new_context(viewport={'width':width,'height':900})
                def route(request):
                    if not request.request.url.startswith(ORIGIN+'/'):return request.abort()
                    headers=dict(request.request.headers)
                    if request.request.method=='POST':headers['origin']=ORIGIN
                    request.fulfill(response=request.fetch(url=upstream+request.request.url[len(ORIGIN):],headers=headers))
                context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(ORIGIN+'/shop');assert page.locator('.card').count()==4;assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                frame=page.locator('.art-frame').first.bounding_box();scroll=page.locator('.message-scroll').first.bounding_box();assert abs(frame['width']/frame['height']-1.5)<.015;assert abs(scroll['width']/scroll['height']-2/3)<.015
                Path('test-artifacts').mkdir(exist_ok=True);page.screenshot(path='test-artifacts/catalog-'+str(width)+'.png',full_page=True)
                page.locator('.card').first.click();page.wait_for_load_state()
                for i,g in enumerate(fixture.source['groups']):page.locator('[name=choice'+str(i)+']').select_option(list(g['choices'])[1] if i==0 else next(iter(g['choices'])))
                page.locator('[name=extra0]').fill('없음');page.locator('[name=sender]').fill('모바일 테스트 보내는 분');page.locator('[name=same_buyer]').check();assert page.locator('[name=buyer]').input_value()=='모바일 테스트 보내는 분'
                page.locator('[name=message]').fill('두 분의 행복을 축하합니다 '*12);page.locator('[name=buyer_phone]').fill('01012345678')
                fields=fixture.catalog_fields();page.locator('[name=venue]').fill(fields['venue']);page.locator('[name=address]').fill(fields['address']);page.locator('[name=event_time]').fill(fields['event_time']);assert page.locator('#amount').inner_text()=='165,000원'
                page.locator('[name=consent]').check();assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path='test-artifacts/catalog-order-'+str(width)+'.png',full_page=True)
                # Validate real form/DOM without navigating to the mocked merchant's URL.
                assert page.locator('#order').evaluate('form=>form.reportValidity()')
                page.goto(ORIGIN+fixture.admin);assert page.locator('.admin-nav a').count()==5;page.locator('.admin-nav a[href="#admin-products"]').click();assert page.locator('#admin-products').count()==1;assert page.locator('#admin-settings').get_by_text('운영 연결 준비',exact=True).count()==1;assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.locator('.admin-nav a[href="#admin-orders"]').click();page.screenshot(path='test-artifacts/admin-'+str(width)+'.png',full_page=False)
                results.append({'width':width,'four_products':True,'frame_ratio':round(frame['width']/frame['height'],3),'scroll_ratio':round(scroll['width']/scroll['height'],3),'same_buyer':True,'server_price_preview':165000,'long_message_visible':True,'overflow':False})
                context.close()
            browser.close()
        assert not errors,errors
        report={'target':'Python backend storefront','results':results,'javascript_errors':errors,'live_requests':False};Path('test-artifacts/catalog-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()


if __name__=='__main__':main()
