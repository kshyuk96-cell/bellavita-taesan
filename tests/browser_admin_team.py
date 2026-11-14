"""Team admin conveniences across mobile and desktop."""
import json
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
from test_shop import ORIGIN
from test_admin_usability import AdminUsabilityTest
import agent


def main():
    fixture=AdminUsabilityTest('test_search_full_records_phone_filters_and_paging');fixture.setUp();fixture.seed_orders();reports=[];errors=[]
    try:
        fixture.http(fixture.admin,dict(action='save_operator_task',order_id=f'{68:032x}',task_id='e'*32,task_version='0',task_title='낮은 우선순위 업무',task_owner='다른 담당',task_due='2019-01-01T09:00',task_state='open',task_priority='low'))
        upstream='http://127.0.0.1:'+str(fixture.server.server_address[1])
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            for width in (375,1280):
                context=browser.new_context(viewport={'width':width,'height':900})
                def route(request):
                    req=request.request
                    if not req.url.startswith(upstream+'/'):return request.abort()
                    headers=dict(req.headers)
                    if req.method=='POST':headers['origin']=ORIGIN
                    response=request.fetch(url=req.url,headers=headers,max_redirects=0);outgoing=dict(response.headers)
                    if 'location' in outgoing:outgoing['location']=urljoin(upstream,outgoing['location'].replace(ORIGIN,upstream))
                    request.fulfill(response=response,headers=outgoing)
                context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                oid=f'{69:032x}';page.goto(upstream+fixture.admin+'?order='+oid+'#order-'+oid);row=page.locator('[data-shop-order]')
                assert row.locator('.order-focus').is_visible();assert not row.locator('.order-reference').get_attribute('open');assert not row.locator('.order-reference dd').first.is_visible()
                row.locator('.order-reference > summary').click();assert row.locator('.order-reference dd').first.is_visible();row.locator('.order-reference > summary').click()
                row.locator('.order-tasks > summary').click();task=row.locator('input[name=action][value=save_operator_task]').last.locator('..');task.locator('[name=task_title]').fill('높은 우선순위 업무 '+str(width));task.locator('[name=task_owner]').fill('교대 담당 '+str(width));task.locator('[name=task_due]').fill('2020-01-01T09:00');task.locator('[name=task_priority]').select_option('high');task.locator('[name=task_note]').fill('직원 내부 근거 '+str(width))
                with page.expect_navigation():task.get_by_role('button',name='업무 등록',exact=True).click()
                titles=page.locator('.task-board a').all_text_contents();assert titles.index('낮은 우선순위 업무')>titles.index('높은 우선순위 업무 '+str(width))
                page.goto(upstream+fixture.admin+'?task_priority=high&task_owner='+str(width));assert page.locator('[data-shop-order]').count()==1
                settings=page.locator('.response-template-settings');settings.get_by_text('새 템플릿 등록',exact=True).click();new=settings.locator('input[name=action][value=save_response_template]').last.locator('..');new.locator('[name=template_name]').fill('상태 안내 '+str(width));new.locator('[name=template_body]').fill('고객님, 주문 {주문번호}: {결제상태}\n{문구}')
                with page.expect_navigation():new.get_by_role('button',name='템플릿 등록',exact=True).click()
                product=page.locator('[data-admin-product]').first
                with agent.db() as c:before=tuple(c.execute('SELECT revision,details FROM shop_products WHERE id=?',(fixture.product_id,)).fetchone())
                product.locator('.product-note > summary').click();product.locator('[name=note_author]').fill('운영 담당');product.locator('[name=product_note]').fill('상품 내부 메모 '+str(width))
                with page.expect_navigation():product.get_by_role('button',name='운영 메모 저장',exact=True).click()
                with agent.db() as c:assert tuple(c.execute('SELECT revision,details FROM shop_products WHERE id=?',(fixture.product_id,)).fetchone())==before
                page.goto(upstream+fixture.admin+'?order='+oid+'#order-'+oid);row=page.locator('[data-shop-order]');row.locator('.response-tools > summary').click();row.locator('.response-choice').select_option(label='상태 안내 '+str(width));response=row.locator('.response-text').input_value();assert oid in response and '결제 완료' in response;assert '{주문번호}' not in response and '내부' not in response
                page.evaluate("Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true})");row.get_by_role('button',name='응대 문구 복사',exact=True).click();assert row.locator('.response-text').evaluate('(el)=>el.selectionEnd-el.selectionStart')==len(response)
                shift=page.locator('.shift-summary');shift.locator('[name=handover_owner]').fill('교대 담당 '+str(width));shift.locator('[name=handover_until]').fill('2020-01-01')
                with page.expect_navigation():shift.get_by_role('button',name='교대 요약 갱신',exact=True).click()
                shift=page.locator('.shift-summary');summary=shift.locator('.shift-text').input_value();assert '미처리 1건 중 1건' in summary and oid in summary and '직원 내부 근거 '+str(width) in summary;assert '낮은 우선순위 업무' not in summary;assert '01012345678' not in summary
                page.evaluate("Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true})");shift.get_by_role('button',name='교대 요약 복사',exact=True).click();assert shift.locator('.shift-text').evaluate('(el)=>el.selectionEnd-el.selectionStart')==len(summary)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');Path('test-artifacts').mkdir(exist_ok=True);page.screenshot(path='test-artifacts/admin-team-'+str(width)+'.png',full_page=False)
                with agent.db() as c:assert c.execute('SELECT state FROM invitation_orders WHERE id=?',(oid,)).fetchone()[0]=='paid'
                reports.append({'width':width,'priority_order_and_filter':True,'response_template_save_and_substitute':True,'product_note_preserves_catalog':True,'focus_and_original_toggle':True,'shift_owner_deadline_filter':True,'internal_and_customer_copy_fallback':True,'payment_state_preserved':True,'overflow':False});context.close()
            browser.close()
        assert not errors,errors
        report={'scenarios':reports,'javascript_errors':errors,'live_side_effects':False};Path('test-artifacts/admin-team.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()

if __name__=='__main__':main()
