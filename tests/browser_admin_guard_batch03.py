"""Admin save/navigation/expiry/CSV UI checks on local synthetic data."""
import csv
import json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from test_admin_usability import AdminUsabilityTest
from test_shop import ORIGIN
import agent


def main():
    fixture=AdminUsabilityTest('test_search_full_records_phone_filters_and_paging');fixture.setUp();fixture.seed_orders()
    results=[];errors=[];dialogs=[];posts=[];oid=f'{69:032x}'
    try:
        with agent.db() as c:before=[tuple(row) for row in c.execute('SELECT id,state,details FROM invitation_orders ORDER BY id')]
        upstream='http://127.0.0.1:'+str(fixture.server.server_address[1])
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            for width in (390,1280):
                context=browser.new_context(viewport={'width':width,'height':900},accept_downloads=True)
                def route(request):
                    req=request.request
                    if not req.url.startswith(upstream+'/'):return request.abort()
                    headers=dict(req.headers)
                    if req.method=='POST':headers['origin']=ORIGIN;posts.append(req.url)
                    response=request.fetch(headers=headers,max_redirects=0);outgoing=dict(response.headers)
                    if 'location' in outgoing:
                        from urllib.parse import urljoin
                        outgoing['location']=urljoin(upstream,outgoing['location'].replace(ORIGIN,upstream))
                    request.fulfill(response=response,headers=outgoing)
                context.route('**/*',route);page=context.new_page();page.clock.install()
                page.on('pageerror',lambda e:errors.append(str(e)))
                def dialog(d):dialogs.append(d.type);d.accept()
                page.on('dialog',dialog)
                base=upstream+fixture.admin
                page.goto(base+'?order='+oid+'#order-'+oid)
                assert '남은 시간' in page.locator('.admin-session-time').inner_text()
                assert page.locator('.context-manual').count()==5
                page.keyboard.press('Control+k');assert page.locator('.admin-search [name=q]').evaluate('el=>el===document.activeElement')
                row=page.locator('[data-shop-order]');row.get_by_role('navigation',name='주문 상세 빠른 이동').get_by_role('button',name='접수·결제 기록',exact=True).click()
                assert row.locator('.order-reference').evaluate('el=>el.open')
                box=row.locator('.admin-phone');box.get_by_role('button',name='전화번호 확인',exact=True).click()
                expect(box.locator('.phone-value')).to_have_text('01012345678')
                page.clock.fast_forward(61000)
                expect(box.locator('.phone-value')).to_have_text('010-****-5678')
                page.goto(base)
                search=page.locator('.admin-search [name=q]');search.fill('적용 전 검색')
                assert '아직 적용되지' in page.locator('.search-unapplied').inner_text()
                page.get_by_role('link',name='검색 결과 전체 CSV 받기').click()
                assert not page.locator('.admin-export-dialog').is_visible()
                assert '먼저 적용' in page.locator('.search-unapplied').inner_text()
                search.fill('')
                page.get_by_role('link',name='검색 결과 전체 CSV 받기').click()
                modal=page.get_by_role('dialog');assert '70건' in modal.inner_text()
                for checkbox in modal.locator('input[type=checkbox]').all():
                    if checkbox.input_value() not in ('order_id','venue','state'):checkbox.uncheck()
                with page.expect_download() as d:modal.get_by_role('link',name='확인 후 CSV 받기').click()
                Path('test-artifacts').mkdir(exist_ok=True);file=Path(f'test-artifacts/admin-guard-export-{width}.csv');d.value.save_as(file)
                rows=list(csv.reader(file.open(encoding='utf-8-sig')));assert len(rows)==71 and rows[0]==['주문번호','행사장','결제 상태']
                modal.get_by_role('button',name='취소',exact=True).click()
                staff=page.locator('[name=staff_phones]');original=staff.input_value();target_phone='01011112222' if width==390 else '01033334444';staff.fill(target_phone)
                assert staff.locator('..').locator('..').locator('.admin-form-status').is_visible()
                count=len(dialogs);page.reload();assert dialogs[count:]==['beforeunload']
                staff=page.locator('[name=staff_phones]');assert staff.input_value()==original
                staff.fill(target_phone);form=staff.locator('..').locator('..')
                count=len(posts);warn=len(dialogs)
                with page.expect_navigation():form.evaluate('f=>{f.requestSubmit();f.requestSubmit();}')
                assert len(posts)==count+1 and len(dialogs)==warn
                page.goto(base+'?order='+oid+'#order-'+oid)
                row=page.locator('[data-shop-order]');row.get_by_role('navigation',name='주문 상세 빠른 이동').get_by_role('button',name='담당 업무',exact=True).click()
                form=row.locator('input[name=action][value=save_operator_task]').last.locator('..')
                count=len(posts);form.evaluate('f=>f.requestSubmit()');assert len(posts)==count
                assert not page.get_by_role('button',name='직원 알림 번호 저장',exact=True).is_disabled()
                pending=page.locator('[data-admin-expiry]').evaluate('el=>(Number(el.dataset.adminExpiry)-Number(el.dataset.adminNow))*1000')
                page.clock.fast_forward(max(0,int(pending)-110000))
                assert page.locator('.admin-session').evaluate('el=>el.classList.contains("soon")')
                assert '새 관리자 링크' in page.locator('.admin-session-notice').inner_text()
                form.locator('[name=task_title]').fill('만료 뒤 보존할 입력')
                page.clock.fast_forward(120000)
                assert '만료' in page.locator('.admin-session-time').inner_text()
                assert form.locator('[name=task_title]').input_value()=='만료 뒤 보존할 입력'
                count=len(posts)
                form.locator('[name=task_owner]').fill('담당');form.locator('[name=task_due]').fill('2026-12-10T10:00');form.locator('[name=task_note]').fill('만료 보호 확인')
                assert form.evaluate('f=>f.reportValidity()')
                form.evaluate('f=>f.requestSubmit()');assert len(posts)==count
                assert page.get_by_role('button',name='직원 알림 번호 저장',exact=True).is_disabled()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.evaluate('localStorage.length===0&&sessionStorage.length===0')
                page.screenshot(path=f'test-artifacts/admin-guard-batch03-{width}.png',full_page=False)
                results.append({'width':width,'dirty_exit_warning':True,'double_submit_single_post':True,'invalid_form_does_not_lock':True,'expiry_countdown_and_warning':True,'expired_inputs_preserved_and_saves_blocked':True,'unapplied_search_blocks_export_preview':True,'csv_count_preview':70,'selected_columns_export':True,'phone_remasked_after_60s':True,'keyboard_search_shortcut':True,'context_manual_links':5,'nested_order_navigation':True,'horizontal_overflow':False})
                context.close()
            browser.close()
        assert not errors,errors
        with agent.db() as c:
            assert before==[tuple(row) for row in c.execute('SELECT id,state,details FROM invitation_orders ORDER BY id')]
            assert c.execute('SELECT count(*) FROM shop_operator_tasks').fetchone()[0]==0
        report={'results':results,'javascript_errors':errors,'order_state_and_details_unchanged':True,'actual_provider_calls':0,'browser_personal_storage_used':False}
        Path('test-artifacts/admin-guard-batch03.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()

if __name__=='__main__':main()
