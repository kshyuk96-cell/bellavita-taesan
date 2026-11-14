"""Completed/due work, feature pins, frozen handovers and launch notes."""
import datetime as dt
import json
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
from test_shop import ORIGIN
from test_admin_usability import AdminUsabilityTest
import agent
import shop_admin_daily as daily


def main():
    fixture=AdminUsabilityTest('test_search_full_records_phone_filters_and_paging');fixture.setUp();fixture.seed_orders();reports=[];errors=[];posts=[];oid=f'{69:032x}'
    try:
        due=(daily.now()+dt.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')
        fixture.http(fixture.admin,dict(action='save_operator_task',order_id=oid,task_id='a'*32,task_version='0',task_title='임박 업무',task_owner='검증 담당',task_due=due,task_state='open',task_priority='high',task_note='보관된 내부 근거'))
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
                page.goto(upstream+fixture.admin+'?task=soon');assert page.locator('[data-shop-order]').count()==1;assert '24시간 이내 1건' in page.locator('.task-board').inner_text()
                with page.expect_navigation():page.locator('.task-board').get_by_role('button',name='업무 빠른 완료',exact=True).click()
                page.goto(upstream+fixture.admin+'?task=done&task_owner=검증&task_priority=high');assert page.locator('[data-shop-order]').count()==1
                row=page.locator('[data-shop-order]');row.locator('details').first.locator('summary').first.click();row.locator('.order-tasks > summary').click();row.locator('.order-tasks > details > summary').first.click()
                with page.expect_navigation():row.get_by_role('button',name='업무 다시 열기',exact=True).click()
                assert page.locator('[data-shop-order]').count()==0
                page.goto(upstream+fixture.admin+'?task=soon');settings=page.locator('.feature-favorite-settings');settings.locator('details > summary').click();remove=settings.get_by_role('button',name='기능 즐겨찾기 해제 · 운영 전 점검',exact=True)
                if remove.count():
                    with page.expect_navigation():remove.click()
                    settings=page.locator('.feature-favorite-settings');settings.locator('details > summary').click()
                with page.expect_navigation():settings.get_by_role('button',name='기능 즐겨찾기 추가 · 운영 전 점검',exact=True).click()
                page.reload();before=len(posts);page.locator('.feature-pinned').get_by_role('button',name='운영 전 점검 위치로 이동',exact=True).click();assert len(posts)==before;assert page.locator('.launch-check').evaluate('(el)=>el===document.activeElement')
                panel=page.locator('.launch-check');assert '실가맹점 키·결제 설정 확인 필요' in panel.inner_text();panel.locator('[name=launch_author]').fill('점검 담당');panel.locator('[name=launch_note]').fill('모의 확인 · 실제 가맹점 검증은 별도');panel.locator('[name=launch_restore]').check()
                with page.expect_navigation():panel.get_by_role('button',name='운영 점검 기록 저장',exact=True).click()
                assert '실가맹점 키·결제 설정 확인 필요' in page.locator('.launch-check').inner_text()
                shift=page.locator('.shift-summary');shift.locator('[name=handover_owner]').fill('검증 담당');shift.locator('[name=handover_until]').fill(due[:10])
                with page.expect_navigation():shift.get_by_role('button',name='교대 요약 갱신',exact=True).click()
                shift=page.locator('.shift-summary');shift.locator('.shift-snapshot-form > summary').click();shift.locator('[name=snapshot_title]').fill('교대 보관 '+str(width));shift.locator('[name=snapshot_author]').fill('교대 담당')
                with page.expect_navigation():shift.get_by_role('button',name='교대 요약 보관',exact=True).click()
                # Later task completion must not rewrite the stored handover.
                with page.expect_navigation():page.locator('.task-board').get_by_role('button',name='업무 빠른 완료',exact=True).click()
                history=page.locator('.shift-history > details').first;history.locator('summary').click()
                with page.expect_download() as waiting:history.get_by_role('link',name='보관 요약 다운로드',exact=True).click()
                Path('test-artifacts').mkdir(exist_ok=True);target=Path('test-artifacts/shift-history-'+str(width)+'.txt');waiting.value.save_as(target);saved=target.read_text();assert '미처리 1건 중 1건' in saved and '보관된 내부 근거' in saved
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path='test-artifacts/admin-launch-'+str(width)+'.png',full_page=False)
                with agent.db() as c:
                    task=c.execute('SELECT * FROM shop_operator_tasks WHERE id=?',('a'*32,)).fetchone();assert c.execute('SELECT state FROM invitation_orders WHERE id=?',(oid,)).fetchone()[0]=='paid'
                fixture.http(fixture.admin,dict(action='quick_operator_task',order_id=oid,task_id='a'*32,task_version=str(task['version']),task_target='open'))
                reports.append({'width':width,'done_owner_priority_search':True,'due_soon_count_and_filter':True,'feature_favorite_persists_and_navigates_without_post':True,'launch_note_does_not_claim_merchant_ready':True,'snapshot_save_and_frozen_download':True,'financial_state_preserved':True,'overflow':False});context.close()
            browser.close()
        assert not errors,errors
        report={'scenarios':reports,'javascript_errors':errors,'live_side_effects':False};Path('test-artifacts/admin-launch.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()

if __name__=='__main__':main()
