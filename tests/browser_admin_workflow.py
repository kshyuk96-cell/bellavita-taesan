"""Durable saved views, handover and safe product change comparison."""
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
                page.locator('[name=search_name]').fill('배송-'+str(width))
                with page.expect_navigation():page.get_by_role('button',name='현재 검색 저장',exact=True).click()
                page.reload();assert page.get_by_role('link',name='배송-'+str(width),exact=True).count()==1
                with page.expect_navigation():page.get_by_role('link',name='배송-'+str(width),exact=True).click()
                assert page.locator('[data-shop-order]').count()==1
                assert page.locator('.backup-health').get_by_text('자동 외부 백업 검증 기록 없음',exact=False).count()==1
                row=page.locator('[data-shop-order]');assert row.locator('.order-next').is_visible();row.locator('details').first.locator('summary').first.click()
                # Force the unsupported clipboard path and verify a usable selected field.
                page.evaluate("Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true})")
                row.get_by_role('button',name='주문번호 복사',exact=True).click();assert row.locator('.copy-source').input_value()==f'{69:032x}'
                assert row.locator('.order-timeline > details').count()==4
                row.locator('.operator-handover > summary').click();row.locator('[name=handover_from]').fill('오전 담당');row.locator('[name=handover_to]').fill('오후 담당');row.locator('[name=check_payment]').check();row.locator('[name=handover_note]').fill('설치 주소 재확인')
                with page.expect_navigation():row.get_by_role('button',name='인수인계 기록 저장',exact=True).click()
                row=page.locator('[data-shop-order]');assert row.locator('.operator-handover > summary').inner_text().endswith('1/5 확인');row.locator('.operator-handover > summary').click();assert row.locator('[name=handover_note]').input_value()=='설치 주소 재확인'
                product=page.locator('[data-admin-product]').first;product.locator('details').first.locator('summary').first.click();product.locator('[name=title]').fill('화면 비교 '+str(width));product.locator('[name=price]').fill(str(120000+width));product.get_by_role('button',name='변경 전후 비교',exact=True).click();assert product.locator('.product-diff').is_visible();assert '아직 저장하지 않았습니다' in product.locator('.product-diff').inner_text()
                with page.expect_navigation():product.get_by_role('button',name='상품 수정 저장',exact=True).click()
                product=page.locator('[data-admin-product]').first;product.locator('.product-history > summary').click();assert '화면 비교 '+str(width) in product.locator('.product-history').inner_text();assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                Path('test-artifacts').mkdir(exist_ok=True);page.screenshot(path='test-artifacts/admin-workflow-'+str(width)+'.png',full_page=False)
                page.locator('.admin-nav a[href="#admin-settings"]').click();page.locator('[name=staff_phones]').fill('01012345678');
                with page.expect_navigation():page.get_by_role('button',name='직원 알림 번호 저장',exact=True).click()
                assert page.locator('.admin-saved').is_visible();assert 'q=' in page.url and '#admin-settings' in page.url, __import__('urllib.parse',fromlist=['urlsplit']).urlsplit(page.url)._replace(path='[admin]').geturl();assert page.locator('[name=q]').input_value()=='찾는 주문자';assert page.locator('[data-shop-order]').count()==1
                page.get_by_role('link',name='관리 매뉴얼',exact=True).click();page.wait_for_load_state();page.locator('#manual-search').fill('타임아웃');assert page.locator('article:visible').count()==1;assert page.get_by_role('link',name='매뉴얼 파일 받기').count()==1
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');Path('test-artifacts').mkdir(exist_ok=True);page.screenshot(path='test-artifacts/admin-manual-'+str(width)+'.png',full_page=False)
                reports.append({'width':width,'saved_search_persists':True,'handover_saved':True,'timeline_groups':4,'product_preview_and_audit':True,'backup_warning':True,'all_record_search':True,'combined_filters':True,'pagination_30':True,'next_action_visible':True,'clipboard_fallback':True,'saved_context_preserved':True,'manual_search':True,'overflow':False});context.close()
            browser.close()
        assert not errors,errors
        report={'scenarios':reports,'javascript_errors':errors,'live_side_effects':False};Path('test-artifacts/admin-workflow.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()


if __name__=='__main__':main()
