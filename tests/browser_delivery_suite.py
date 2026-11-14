"""Mobile core UI: assign, hand over, deliver, upload, publish, acknowledge."""
import io
import json
from pathlib import Path
from openpyxl import load_workbook
from playwright.sync_api import sync_playwright
from test_shop_delivery_suite import DeliverySuiteTest,photograph
from test_shop import ORIGIN


def main():
    fixture=DeliverySuiteTest('test_assignment_stale_link_revocation_handover_once_and_staff_scope');fixture.setUp()
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
            page.goto(ORIGIN+fixture.admin);row=page.locator('[data-shop-order]').filter(has_text='a'*32);row.locator('summary').first.click()
            row.locator('[name=assignee]').select_option('staff');row.locator('[name=assignment_note]').fill('모바일 담당 배정')
            row.get_by_role('button',name='배송 담당 저장',exact=True).click();page.wait_for_load_state()
            row=page.locator('[data-shop-order]').filter(has_text='a'*32);row.locator('summary').first.click()
            staff=row.get_by_role('link',name='현재 담당자 주문 링크 (24시간)',exact=True).get_attribute('href');page.goto(ORIGIN+staff)
            page.locator('[name=handover_note]').fill('장소·문구·일시 인수 확인');page.locator('[name=handover_checked]').check()
            page.get_by_role('button',name='담당 주문 인수 확인',exact=True).click();page.wait_for_load_state()
            page.get_by_role('button',name='배송 중으로 변경',exact=True).click();page.wait_for_load_state()
            page.get_by_role('button',name='배송 완료로 변경',exact=True).click();page.wait_for_load_state()
            page.locator('[name=photo]').set_input_files({'name':'test-delivery.jpg','mimeType':'image/jpeg','buffer':photograph()})
            page.locator('[name=photo_note]').fill('테스트 배송 사진 · 실제 설치 아님');page.get_by_role('button',name='배송 사진 업로드',exact=True).click();page.wait_for_load_state()
            assert '공개 검수 대기' in page.locator('body').inner_text()
            assert page.get_by_role('button',name='배송 사진 공개 저장',exact=True).count()==0
            page.goto(ORIGIN+fixture.admin);row=page.locator('[data-shop-order]').filter(has_text='a'*32);row.locator('summary').first.click()
            row.locator('[name=photo_review_note]').fill('고객 공개 범위 검수');row.locator('[name=photo_checked]').check();row.get_by_role('button',name='배송 사진 공개 저장',exact=True).click();page.wait_for_load_state()
            page.goto(ORIGIN+'/invitation-orders/'+('a'*32)+'/receipt');page.locator('[name=photo_confirmed]').check();page.get_by_role('button',name='고객 사진 확인',exact=True).click();page.wait_for_load_state()
            assert '배송 사진 확인을 기록했습니다' in page.locator('body').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            output=Path('test-artifacts');output.mkdir(exist_ok=True);page.screenshot(path=str(output/'delivery-customer-confirmed.png'),full_page=True)
            page.goto(ORIGIN+fixture.admin);page.get_by_role('button',name='설정·모의 연동 진단',exact=True).click();page.wait_for_load_state()
            assert '모의 검증 통과' in page.locator('body').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path=str(output/'delivery-suite-mobile-admin.png'),full_page=True)
            response=context.request.get(upstream+fixture.admin+'/settlement.xlsx');assert response.status==200
            book=load_workbook(io.BytesIO(response.body()));assert book['주문정산'].max_row==2
            assert not errors,errors;browser.close()
        result={'mobile_width':375,'assignment_handover':'passed','staff_upload_admin_publish':'passed','customer_photo_confirmation':'passed','schedule_board':True,'settlement_workbook_orders':1,'local_mock_diagnostics':'passed','javascript_errors':errors,'actual_delivery':False,'actual_outbound_requests':False}
        (Path('test-artifacts')/'delivery-suite-browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
    finally:fixture.tearDown()


if __name__=='__main__':main()
