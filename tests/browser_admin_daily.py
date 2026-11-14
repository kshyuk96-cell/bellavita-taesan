"""Native admin daily tasks, customer response, input guidance and safe training."""
import datetime as dt
import json
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,urljoin
from playwright.sync_api import sync_playwright
from test_shop import ORIGIN
from test_admin_usability import AdminUsabilityTest
import agent
import shop_admin_daily as daily
import shop_admin_training as training


def main():
    fixture=AdminUsabilityTest('test_search_full_records_phone_filters_and_paging');fixture.setUp();fixture.seed_orders();reports=[];errors=[];posts=[]
    try:
        upstream='http://127.0.0.1:'+str(fixture.server.server_address[1])
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            for width in (375,1280):
                context=browser.new_context(viewport={'width':width,'height':900})
                def route(request):
                    req=request.request
                    if not req.url.startswith(upstream+'/'):return request.abort()
                    headers=dict(req.headers)
                    if req.method=='POST':headers['origin']=ORIGIN;posts.append(req.url)
                    response=request.fetch(url=req.url,headers=headers,max_redirects=0);outgoing=dict(response.headers)
                    if 'location' in outgoing:outgoing['location']=urljoin(upstream,outgoing['location'].replace(ORIGIN,upstream))
                    request.fulfill(response=response,headers=outgoing)
                context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(upstream+fixture.admin)
                with page.expect_navigation():page.get_by_role('link',name='오늘 행사',exact=False).click()
                query=parse_qs(urlsplit(page.url).query);assert query['from_date']==[daily.now().date().isoformat()];assert query['to_date']==query['from_date']
                oid=f'{69:032x}';page.goto(upstream+fixture.admin+'?order='+oid+'#order-'+oid);row=page.locator('[data-shop-order]')
                # Force unsupported clipboard; the customer-safe summary stays selectable.
                page.evaluate("Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true})")
                row.get_by_role('button',name='고객 응대용 요약 복사',exact=True).click();summary=row.locator('.summary-source').input_value();assert oid in summary and '고객' not in summary.splitlines()[0];assert '/shop/admin/' not in summary
                row.locator('.order-tasks > summary').click();form=row.locator('input[name=action][value=save_operator_task]').last.locator('..')
                form.locator('[name=task_title]').fill('행사장 전화 확인 '+str(width));form.locator('[name=task_owner]').fill('교육 담당 '+str(width));form.locator('[name=task_due]').fill('2020-01-01T09:00');form.locator('[name=task_note]').fill('내부 전용 · 고객에게 전달 금지')
                with page.expect_navigation():form.get_by_role('button',name='업무 등록',exact=True).click()
                assert page.locator('.task-board').get_by_text('행사장 전화 확인 '+str(width),exact=False).count()==1
                page.goto(upstream+fixture.admin+'?task=overdue&task_owner='+str(width));assert page.locator('[data-shop-order]').count()==1
                with agent.db() as c:assert c.execute('SELECT state FROM invitation_orders WHERE id=?',(oid,)).fetchone()[0]=='paid'
                row=page.locator('[data-shop-order]');row.locator('details').first.locator('summary').first.click();assert '내부 전용' not in row.locator('.summary-source').input_value()
                row.locator('.order-tasks > summary').click();edit=row.locator('input[name=action][value=save_operator_task]').first.locator('..');edit.locator('..').locator('summary').first.click();edit.locator('[name=task_state]').select_option('done')
                with page.expect_navigation():edit.get_by_role('button',name='업무 변경 저장',exact=True).click()
                assert page.locator('[data-shop-order]').count()==0;assert page.locator('.admin-saved').get_by_role('link',name='변경한 주문 바로 열기',exact=False).count()==1
                product=page.locator('[data-admin-product]').first;product.locator('details').first.locator('summary').first.click();options=product.locator('[name=options]');original=options.input_value();options.fill('쌀 10kg | 0\n쌀 10kg | 1000');assert '중복' in product.locator('.product-guidance').inner_text();assert not options.evaluate('(el)=>el.checkValidity()');options.fill('쌀 | -1');assert not options.evaluate('(el)=>el.checkValidity()');options.fill(original);assert options.evaluate('(el)=>el.checkValidity()')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');Path('test-artifacts').mkdir(exist_ok=True);page.screenshot(path='test-artifacts/admin-daily-'+str(width)+'.png',full_page=False)
                with agent.db() as c:count=c.execute('SELECT count(*) FROM invitation_orders').fetchone()[0]
                with page.expect_navigation():page.get_by_role('link',name='직원 교육 시작',exact=True).click()
                before=len(posts);page.get_by_role('button',name='선택 결과 확인',exact=True).click();assert '먼저 선택' in page.locator('#training-result').inner_text()
                for case in training.SCENARIOS:
                    page.get_by_role('button',name=case['title'],exact=True).click();page.locator('[name=practice-answer]').nth((case['correct']+1)%3).check();page.get_by_role('button',name='선택 결과 확인',exact=True).click();assert '다시 확인' in page.locator('#training-result').inner_text();page.locator('[name=practice-answer]').nth(case['correct']).check();page.get_by_role('button',name='선택 결과 확인',exact=True).click();assert '정답' in page.locator('#training-result').inner_text()
                assert page.locator('#training-score').inner_text()=='완료 5/5';assert len(posts)==before
                with agent.db() as c:assert c.execute('SELECT count(*) FROM invitation_orders').fetchone()[0]==count
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path='test-artifacts/admin-training-'+str(width)+'.png',full_page=False)
                page.get_by_role('button',name='교육 처음부터',exact=True).click();assert page.locator('#training-score').inner_text()=='완료 0/5'
                reports.append({'width':width,'seoul_today_link':True,'task_create_and_complete':True,'overdue_owner_filter':True,'payment_state_preserved':True,'customer_summary_excludes_internal_note':True,'clipboard_fallback':True,'duplicate_and_negative_option_validation':True,'training_five_cases_and_reset':True,'training_posts':0,'overflow':False});context.close()
            browser.close()
        assert not errors,errors
        report={'scenarios':reports,'javascript_errors':errors,'live_side_effects':False};Path('test-artifacts/admin-daily.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()

if __name__=='__main__':main()
