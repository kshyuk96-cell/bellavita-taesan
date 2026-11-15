"""Ten admin search improvements on synthetic local orders only."""
import csv
import datetime as dt
import json
from pathlib import Path
from urllib.parse import urlencode,urlsplit,parse_qs
from playwright.sync_api import sync_playwright,expect
from test_admin_usability import AdminUsabilityTest
from test_shop import ORIGIN
import agent
import shop_admin_queries as queries


def main():
    fixture=AdminUsabilityTest('test_search_full_records_phone_filters_and_paging');fixture.setUp();fixture.seed_orders()
    errors=[];results=[];oid=f'{69:032x}';cancelled=f'{65:032x}';partial=f'{64:032x}'
    try:
        with agent.db() as c:
            data=json.loads(c.execute('SELECT details FROM invitation_orders WHERE id=?',(oid,)).fetchone()[0]);data['event_time']=(dt.datetime.now(queries.ZoneInfo('Asia/Seoul'))+dt.timedelta(hours=2)).isoformat()
            c.execute('UPDATE invitation_orders SET details=?,created=? WHERE id=?',(json.dumps(data),'2020-01-01',oid))
            c.execute("UPDATE invitation_orders SET state='cancelled' WHERE id=?",(cancelled,));c.execute("UPDATE invitation_orders SET state='partial_cancel' WHERE id=?",(partial,))
            before=[tuple(row) for row in c.execute('SELECT id,state,details FROM invitation_orders ORDER BY id')]
        upstream='http://127.0.0.1:'+str(fixture.server.server_address[1])
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            for width in (390,1280):
                context=browser.new_context(viewport={'width':width,'height':900},accept_downloads=True)
                def route(request):
                    req=request.request
                    if not req.url.startswith(upstream+'/'):return request.abort()
                    headers=dict(req.headers)
                    if req.method=='POST':headers['origin']=ORIGIN
                    response=request.fetch(headers=headers,max_redirects=0);outgoing=dict(response.headers)
                    if 'location' in outgoing:
                        from urllib.parse import urljoin
                        outgoing['location']=urljoin(upstream,outgoing['location'].replace(ORIGIN,upstream))
                    request.fulfill(response=response,headers=outgoing)
                context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(upstream+fixture.admin+'?'+urlencode({'q':'찾는 주문자','state':'paid','sort':'event'}))
                expect(page.locator('[data-shop-order]')).to_have_count(1)
                assert page.locator('mark.order-match').first.inner_text()=='찾는 주문자'
                page.get_by_role('navigation',name='적용 중인 검색 조건').get_by_role('link',name='결제 상태 해제',exact=True).click();page.wait_for_load_state()
                f=parse_qs(urlsplit(page.url).query);assert 'state' not in f and f['sort']==['event']
                row=page.locator('[data-shop-order]');row.locator('details > summary').first.click();row.locator('.order-reference > summary').click()
                phone=row.locator('.phone-value');expect(phone).to_have_text('010-****-5678')
                row.get_by_role('button',name='전화번호 확인',exact=True).click();expect(phone).to_have_text('01012345678')
                row.locator('.order-reference > summary').click();expect(phone).to_have_text('010-****-5678')
                page.evaluate("Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true})")
                row.get_by_role('button',name='설치 정보 복사',exact=True).click()
                copied=row.locator('.installation-source').input_value();assert oid in copied and fixture.product['venue'] in copied
                assert '01012345678' not in copied and '/shop/admin/' not in copied
                page.goto(upstream+fixture.admin+'?'+urlencode({'q':'행복한'}))
                row=page.locator('[data-shop-order]').first;row.locator('details > summary').first.click();assert row.locator('.order-focus mark').count()>0
                page.goto(upstream+fixture.admin+'?sort=event');expect(page.locator('[data-shop-order]').first).to_have_attribute('data-shop-order',oid)
                page.locator('.admin-search [name=sort]').select_option('newest')
                with page.expect_navigation():page.locator('.admin-search').get_by_role('button',name='검색',exact=True).click()
                assert page.locator('[data-shop-order]').first.get_attribute('data-shop-order')!=oid
                page.goto(upstream+fixture.admin+'?page=3&sort=newest')
                page.get_by_role('link',name='검색 결과 전체 CSV 받기').click()
                with page.expect_download() as download:page.get_by_role('dialog').get_by_role('link',name='확인 후 CSV 받기').click()
                page.get_by_role('dialog').get_by_role('button',name='취소',exact=True).click()
                Path('test-artifacts').mkdir(exist_ok=True);target=Path(f'test-artifacts/search-export-{width}.csv');download.value.save_as(target)
                records=list(csv.reader(target.open(encoding='utf-8-sig')));assert len(records)==71
                assert all('****' in r[5] for r in records[1:])
                page.goto(upstream+fixture.admin+'?'+urlencode({'q':'없는 주문','from_date':'2026-12-01','to_date':'2026-12-02'}))
                assert page.locator('.empty-order-help').is_visible()
                page.get_by_role('link',name='행사일 조건 없이 다시 찾기',exact=True).click();page.wait_for_load_state();f=parse_qs(urlsplit(page.url).query);assert 'from_date' not in f and 'to_date' not in f
                for identity,label in ((cancelled,'취소 · 제작·배송 보류'),(partial,'부분취소 · 잔액·재개 조건 확인')):
                    page.goto(upstream+fixture.admin+'?order='+identity);assert label in page.locator('.order-badge').inner_text()
                page.goto(upstream+fixture.admin+'?order='+oid+'#order-'+oid)
                row=page.locator('[data-shop-order]');row.locator('.order-reference > summary').click();row.locator('.order-tasks > summary').click()
                form=row.locator('input[name=action][value=save_operator_task]').last.locator('..')
                form.locator('[name=task_title]').fill('상세 복귀 '+str(width));form.locator('[name=task_owner]').fill('검증 담당');form.locator('[name=task_due]').fill('2026-12-10T10:00');form.locator('[name=task_note]').fill('독립 검증 메모')
                with page.expect_navigation():form.get_by_role('button',name='업무 등록',exact=True).click()
                row=page.locator('[data-shop-order]');assert row.locator('.order-reference').evaluate('el=>el.open');assert row.locator('.order-tasks').evaluate('el=>el.open')
                assert 'expanded' in parse_qs(urlsplit(page.url).query)
                page.locator('.order-search-help > summary').click();assert '날짜는 접수일이 아닌 행사일' in page.locator('.order-search-help').inner_text()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=f'test-artifacts/admin-review-batch02-{width}.png',full_page=False)
                results.append({'width':width,'active_filter_remove':True,'safe_match_highlight':True,'sort_modes':True,'masked_phone_reveal_and_collapse':True,'all_pages_filtered_csv':True,'empty_result_recovery':True,'nested_details_after_save':True,'installation_copy_fallback':True,'cancel_hold_badges':True,'search_examples':True,'horizontal_overflow':False});context.close()
            browser.close()
        assert not errors,errors
        with agent.db() as c:assert [tuple(row) for row in c.execute('SELECT id,state,details FROM invitation_orders ORDER BY id')]==before
        report={'results':results,'javascript_errors':errors,'financial_state_and_order_details_preserved':True,'personal_browser_storage_used':False,'actual_payments':False,'actual_messages':False}
        Path('test-artifacts/admin-review-batch02.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()

if __name__=='__main__':main()
