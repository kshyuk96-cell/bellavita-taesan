"""Mobile administrator flows for amendments, service, CSV and sales."""
import csv
import io
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from test_shop_growth import GrowthTest
from test_shop import ORIGIN


def main():
    fixture=GrowthTest('test_request_retries_stale_decisions_and_shipping_restriction');fixture.setUp()
    try:
        fixture.pay();upstream='http://127.0.0.1:'+str(fixture.server.server_address[1]);errors=[]
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox']);context=browser.new_context(viewport={'width':375,'height':900})
            def route(request):
                if not request.request.url.startswith(ORIGIN+'/'):return request.abort()
                headers=dict(request.request.headers)
                if request.request.method=='POST':headers['origin']=ORIGIN
                request.fulfill(response=request.fetch(url=upstream+request.request.url[len(ORIGIN):],headers=headers))
            context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            def order():
                row=page.locator('[data-shop-order]').filter(has_text='a'*32);details=row.locator('details').first
                if not details.get_attribute('open'):details.locator('summary').first.click()
                return row
            page.goto(ORIGIN+fixture.admin);row=order();row.locator('[name=new_sender]').fill('모바일 정정 보내는 분');row.locator('[name=new_message]').fill('모바일에서 승인한 정정 문구');row.locator('[name=amendment_note]').fill('고객 정정 요청 대조')
            row.get_by_role('button',name='정정 검토 요청',exact=True).click();page.wait_for_load_state();row=order()
            row.locator('[name=amendment_reviewer]').fill('검토 담당자');row.locator('[name=amendment_review_note]').fill('원문·정정 문구 대조');row.locator('[name=amendment_checked]').check();row.get_by_role('button',name='정정 결정 저장',exact=True).click();page.wait_for_load_state();row=order()
            row.locator('[name=width_mm]').fill('100');row.locator('[name=height_mm]').fill('300');row.get_by_role('button',name='제작 파일 생성',exact=True).click();page.wait_for_load_state();row=order()
            row.locator('[name=artifact_reviewer]').fill('제작 검수자');row.locator('[name=artifact_note]').fill('정정 문구 확인');row.locator('[name=artifact_compared]').check();row.get_by_role('button',name='제작 검수 저장',exact=True).click();page.wait_for_load_state();row=order()
            row.locator('[name=case_reason]').fill('AS 처리 기록 체험');row.get_by_role('button',name='AS·재배송 접수',exact=True).click();page.wait_for_load_state();row=order()
            case=row.locator('.shop-service-case');case.locator('summary').click();case.locator('[name=case_assignee]').select_option('staff');case.locator('[name=case_state]').select_option('assigned');case.locator('[name=case_note]').fill('AS 담당 지정');case.get_by_role('button',name='AS 처리 저장',exact=True).click();page.wait_for_load_state()
            when=json.loads(fixture.order()['details'])['event_time'];output=io.StringIO();writer=csv.writer(output);writer.writerow(['order_id','event_time','assignee','assignment_version','note']);writer.writerow(['a'*32,when,'staff','0','모바일 배정'])
            page.locator('input[type=file][name=csv]').set_input_files({'name':'assignments.csv','mimeType':'text/csv','buffer':output.getvalue().encode()});page.get_by_role('button',name='CSV 사전 검토',exact=True).click();page.wait_for_load_state()
            page.locator('details').filter(has_text='적용 가능').filter(has=page.locator('[name=batch_checked]')).first.locator('summary').first.click()
            page.locator('[name=batch_checked]').check();page.get_by_role('button',name='선택한 배정 행 적용',exact=True).click();page.wait_for_load_state()
            page.get_by_role('button',name='판매 집계 조회',exact=True).click();page.wait_for_load_state();assert '주문 정산 합계 일치' in page.locator('body').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');assert not errors,errors
            artifacts=Path('test-artifacts');artifacts.mkdir(exist_ok=True);page.screenshot(path=str(artifacts/'shop-growth-mobile.png'),full_page=True);browser.close()
        report={'mobile_width':375,'amendment_approval_and_artifact_review':True,'service_case_assignment':True,'csv_preview_and_apply':True,'sales_reconciliation':True,'overflow':False,'javascript_errors':errors,'external_side_effects':False}
        (Path('test-artifacts')/'shop-growth-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()


if __name__=='__main__':main()
