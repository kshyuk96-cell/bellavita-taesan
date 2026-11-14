"""Operator search, save context, fallback copy and in-app manual."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from test_admin_usability import AdminUsabilityTest
from test_shop import ORIGIN


def main():
    fixture=AdminUsabilityTest('test_search_full_records_phone_filters_and_paging');fixture.setUp();fixture.seed_orders()
    reports=[];errors=[]
    try:
        upstream='http://127.0.0.1:'+str(fixture.server.server_address[1])
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            for width in (375,1280):
                context=browser.new_context(viewport={'width':width,'height':900})
                def route(request):
                    if not request.request.url.startswith(upstream+'/'):return request.abort()
                    headers=dict(request.request.headers)
                    if request.request.method=='POST':headers['origin']=ORIGIN
                    response=request.fetch(url=request.request.url,headers=headers,max_redirects=0)
                    outgoing=dict(response.headers)
                    if 'location' in outgoing:
                        from urllib.parse import urljoin
                        outgoing['location']=urljoin(upstream,outgoing['location'].replace(ORIGIN,upstream))
                    request.fulfill(response=response,headers=outgoing)
                context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(upstream+fixture.admin);assert page.locator('[data-shop-order]').count()==30
                page.locator('[name=q]').fill('찾는 주문자');page.locator('.admin-search select[name=state]').select_option('paid');page.locator('.admin-search select[name=stage]').select_option('confirmed');page.locator('[name=from_date]').fill('2026-12-10');page.locator('[name=to_date]').fill('2026-12-10');page.locator('.admin-search').get_by_role('button',name='검색',exact=True).click();page.wait_for_load_state();assert page.locator('[data-shop-order]').count()==1;assert '전체 일치 1건' in page.locator('#shop-result-count').inner_text()
                row=page.locator('[data-shop-order]');assert row.locator('.order-next').is_visible();row.locator('details').first.locator('summary').first.click()
                # Force the unsupported clipboard path and verify a usable selected field.
                page.evaluate("Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true})")
                row.get_by_role('button',name='주문번호 복사',exact=True).click();assert row.locator('.copy-source').input_value()==f'{69:032x}'
                page.locator('.admin-nav a[href="#admin-settings"]').click();page.locator('[name=staff_phones]').fill('01012345678');
                with page.expect_navigation():page.get_by_role('button',name='직원 알림 번호 저장',exact=True).click()
                assert page.locator('.admin-saved').is_visible();assert 'q=' in page.url and '#admin-settings' in page.url, __import__('urllib.parse',fromlist=['urlsplit']).urlsplit(page.url)._replace(path='[admin]').geturl();assert page.locator('[name=q]').input_value()=='찾는 주문자';assert page.locator('[data-shop-order]').count()==1
                page.get_by_role('link',name='관리 매뉴얼',exact=True).click();page.wait_for_load_state();page.locator('#manual-search').fill('타임아웃');assert page.locator('article:visible').count()==1;assert page.get_by_role('link',name='매뉴얼 파일 받기').count()==1
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');Path('test-artifacts').mkdir(exist_ok=True);page.screenshot(path='test-artifacts/admin-manual-'+str(width)+'.png',full_page=False)
                reports.append({'width':width,'all_record_search':True,'combined_filters':True,'pagination_30':True,'next_action_visible':True,'clipboard_fallback':True,'saved_context_preserved':True,'manual_search':True,'overflow':False});context.close()
            browser.close()
        assert not errors,errors
        report={'scenarios':reports,'javascript_errors':errors,'live_side_effects':False};Path('test-artifacts/admin-usability.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()


if __name__=='__main__':main()
