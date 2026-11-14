"""Task shortcuts, shared favorites, note preview, export and feature navigation."""
import json
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
from test_shop import ORIGIN
from test_admin_usability import AdminUsabilityTest
import agent


def main():
    fixture=AdminUsabilityTest('test_search_full_records_phone_filters_and_paging');fixture.setUp();fixture.seed_orders();reports=[];errors=[];posts=[]
    oid=f'{69:032x}'
    try:
        fixture.http(fixture.admin,dict(action='save_operator_task',order_id=oid,task_id='a'*32,task_version='0',task_title='빠른 처리 검증',task_owner='교대 검증 담당',task_due='2020-01-01T09:00',task_state='open',task_priority='high',task_note='직원 내부용 근거'))
        upstream='http://127.0.0.1:'+str(fixture.server.server_address[1])
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            for width in (375,1280):
                context=browser.new_context(viewport={'width':width,'height':900},accept_downloads=True)
                def route(request):
                    req=request.request
                    if not req.url.startswith(upstream+'/'):return request.abort()
                    headers=dict(req.headers)
                    if req.method=='POST':headers['origin']=ORIGIN;posts.append(req.url)
                    response=request.fetch(url=req.url,headers=headers,max_redirects=0);outgoing=dict(response.headers)
                    if 'location' in outgoing:outgoing['location']=urljoin(upstream,outgoing['location'].replace(ORIGIN,upstream))
                    request.fulfill(response=response,headers=outgoing)
                context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(upstream+fixture.admin+'?order='+oid+'#order-'+oid)
                with page.expect_navigation():page.locator('.task-board').get_by_role('button',name='업무 빠른 완료',exact=True).click()
                assert page.locator('.task-board').get_by_text('빠른 처리 검증',exact=True).count()==0
                row=page.locator('[data-shop-order]');row.locator('.order-tasks > summary').click();row.locator('.order-tasks > details > summary').first.click()
                with page.expect_navigation():row.get_by_role('button',name='업무 다시 열기',exact=True).click()
                assert page.locator('.task-board').get_by_text('빠른 처리 검증',exact=True).count()==1
                remove=page.locator('.response-favorites').get_by_role('button',name='즐겨찾기 해제 · 결제 확인 안내',exact=True)
                if remove.count():
                    with page.expect_navigation():remove.click()
                with page.expect_navigation():page.locator('.response-favorites').get_by_role('button',name='즐겨찾기 추가 · 결제 확인 안내',exact=True).click()
                page.goto(upstream+fixture.admin+'?order='+oid+'#order-'+oid);row=page.locator('[data-shop-order]');row.locator('.response-tools > summary').click();assert row.locator('.response-choice option').first.inner_text()=='★ 결제 확인 안내';assert row.locator('.response-text').input_value()==row.locator('.response-choice').input_value()
                product=page.locator('[data-admin-product]').first;product.locator('.product-note > summary').click();product.locator('[name=note_author]').fill('내부 담당');note='요약 검증 '+str(width)+' '+('긴 운영 메모 '*30);product.locator('[name=product_note]').fill(note)
                with page.expect_navigation():product.get_by_role('button',name='운영 메모 저장',exact=True).click()
                preview=page.locator('.product-note-preview').first;assert preview.is_visible();assert note[:120] in preview.inner_text() and note not in preview.inner_text();assert not page.locator('.product-note').first.get_attribute('open')
                before=len(posts);page.locator('#admin-feature-query').fill('상품 메모');page.get_by_role('button',name='상품 운영 메모 위치로 이동',exact=True).click();assert page.locator('.product-note').first.get_attribute('open') is not None;assert len(posts)==before
                page.locator('#admin-feature-query').fill('없는기능xyz');assert '일치하는 기능이 없습니다' in page.locator('#admin-feature-count').inner_text();assert page.locator('#admin-feature-results button').count()==0
                shift=page.locator('.shift-summary');shift.locator('[name=handover_owner]').fill('교대 검증 담당');shift.locator('[name=handover_until]').fill('2020-01-01')
                with page.expect_navigation():shift.get_by_role('button',name='교대 요약 갱신',exact=True).click()
                with page.expect_download() as waiting:page.get_by_role('link',name='교대 요약 파일 다운로드',exact=True).click()
                download=waiting.value;assert download.suggested_filename=='bellavita-shift-summary.txt';Path('test-artifacts').mkdir(exist_ok=True);target=Path('test-artifacts/shift-summary-'+str(width)+'.txt');download.save_as(target);text=target.read_text();assert '미처리 1건 중 1건' in text and '직원 내부용 근거' in text and oid in text and '01012345678' not in text
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path='test-artifacts/admin-shortcuts-'+str(width)+'.png',full_page=False)
                with agent.db() as c:assert c.execute('SELECT state FROM invitation_orders WHERE id=?',(oid,)).fetchone()[0]=='paid'
                reports.append({'width':width,'quick_done_and_reopen':True,'shared_favorite_sorted_and_prefilled':True,'note_preview_bounded_and_detail_closed':True,'feature_search_opens_detail_without_post':True,'unknown_feature_empty_result':True,'filtered_shift_file_download':True,'financial_state_preserved':True,'overflow':False});context.close()
            browser.close()
        assert not errors,errors
        report={'scenarios':reports,'javascript_errors':errors,'live_side_effects':False};Path('test-artifacts/admin-shortcuts.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()

if __name__=='__main__':main()
