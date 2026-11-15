"""Ten pre-payment assistance scenarios on local data, never submit an order."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from test_shop_catalog import CatalogTest
from test_shop import ORIGIN
import agent


def main():
    fixture=CatalogTest('test_only_four_public_products_and_native_options');fixture.setUp()
    results=[];errors=[];dialogs=[];writes=[];invitation_results=[];preview_requests=[];handoffs=[]
    allow_handoff={'enabled':False}
    try:
        upstream='http://127.0.0.1:'+str(fixture.server.server_address[1])
        job='e'*32
        folder=fixture.root/'invitations'/job;folder.mkdir(parents=True)
        product=dict(fixture.product,wedding_photo=ORIGIN+'/checkout-art/rice-10kg.webp')
        product.pop('cash_tax_type',None)
        (folder/'93500.order.json').write_text(json.dumps(product,ensure_ascii=False))
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            for width in (390,1280):
                context=browser.new_context(viewport={'width':width,'height':900})
                def route(request):
                    req=request.request
                    if not req.url.startswith(ORIGIN+'/'):return request.abort()
                    if req.url==ORIGIN+'/checkout-sender':
                        preview_requests.append(req.url)
                        return request.fulfill(status=200,content_type='application/json',body=json.dumps({'group':['초대장 보내는 분']}))
                    if req.method!='GET':
                        if allow_handoff['enabled'] and req.method=='POST' and req.url==ORIGIN+'/shop/products/'+fixture.identity:
                            handoffs.append(req.post_data)
                            return request.fulfill(status=200,content_type='text/html',body='<p>Isolated prepayment handoff captured</p>')
                        writes.append(req.url)
                        return request.abort()
                    request.fulfill(response=request.fetch(url=upstream+req.url[len(ORIGIN):]))
                context.route('**/*',route)
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                def handle_dialog(dialog):
                    dialogs.append(dialog.type);dialog.accept()
                page.on('dialog',handle_dialog)
                url=ORIGIN+'/shop/products/'+fixture.identity
                page.goto(url);assert page.locator('#order-review').count()==1
                attempt=page.locator('[name=attempt]').input_value()
                assert page.evaluate('localStorage.length===0 && sessionStorage.length===0')
                page.locator('[name=buyer_phone]').fill('123')
                page.locator('#order').evaluate('form=>form.reportValidity()')
                page.wait_for_timeout(50)
                assert page.locator('#order-errors').is_visible()
                page.locator('#order-errors').get_by_role('button',name='보내시는 분:',exact=False).click()
                assert page.locator('[name=sender]').evaluate('el=>el===document.activeElement')
                for i,g in enumerate(fixture.source['groups']):
                    page.locator('[name=choice'+str(i)+']').select_option(list(g['choices'])[1] if i==0 else next(iter(g['choices'])))
                page.locator('[name=extra0]').fill('없음')
                page.locator('[name=sender]').fill('테스트 보내는 분')
                page.locator('[name=same_buyer]').check()
                text='<img src=x onerror=alert(1)> 행복을 축하합니다'
                page.locator('[name=message]').fill(text)
                assert page.locator('#message-count').inner_text()==str(len(text))+' / 200자'
                assert page.locator('#sender-count').inner_text()==str(len('테스트 보내는 분'))+' / 140자'
                phone=page.locator('[name=buyer_phone]');phone.fill('010-1234-5678');phone.blur()
                assert phone.input_value()=='01012345678'
                assert phone.evaluate('el=>el.validity.valid')
                fields=fixture.catalog_fields()
                for name in ('venue','address','event_time'):page.locator('[name='+name+']').fill(fields[name])
                receipt=page.locator('[name=cash_receipt_type]');identifier=page.locator('[name=cash_receipt_identifier]')
                receipt.select_option('business');identifier.fill('123')
                assert not identifier.evaluate('el=>el.validity.valid')
                identifier.fill('123-45-67890');identifier.blur();assert identifier.input_value()=='1234567890'
                assert identifier.evaluate('el=>el.validity.valid')
                receipt.select_option('self');assert identifier.is_disabled()
                receipt.select_option('personal');identifier.fill('');assert identifier.evaluate('el=>el.validity.valid')
                page.locator('[name=consent]').check()
                assert page.locator('#order').evaluate('form=>form.reportValidity()')
                assert not page.locator('#order-errors').is_visible()
                summary=page.locator('#order-review')
                assert text in summary.inner_text() and summary.locator('img').count()==0
                assert '165,000원' in summary.inner_text() and fields['venue'] in summary.inner_text()
                required=page.locator('#order').evaluate('f=>[...f.elements].filter(e=>e.required&&!e.disabled).length')
                assert page.locator('#order-progress').inner_text()==f'필수 입력 {required} / {required} 완료'
                summary.get_by_role('button',name='보내시는 분 수정',exact=True).click()
                assert page.locator('[name=sender]').evaluate('el=>el===document.activeElement')
                summary.get_by_role('button',name='주문자 이름 수정',exact=True).click()
                assert page.locator('[name=same_buyer]').evaluate('el=>el===document.activeElement')
                if width==390:
                    bar=page.locator('.preflight-mobile');assert bar.is_visible()
                    assert bar.locator('strong').inner_text()=='165,000원'
                    bar.get_by_role('button',name='주문 내용 확인').click()
                    assert summary.evaluate('el=>el===document.activeElement')
                else:assert not page.locator('.preflight-mobile').is_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                Path('test-artifacts').mkdir(exist_ok=True)
                page.screenshot(path=f'test-artifacts/checkout-preflight-{width}.png',full_page=True)
                reset=summary.get_by_role('button',name='입력 내용 지우기')
                reset.click();page.get_by_role('dialog').get_by_role('button',name='계속 입력하기').click()
                assert page.locator('[name=message]').input_value()==text
                reset.click();page.get_by_role('dialog').get_by_role('button',name='지우기',exact=True).click()
                assert page.locator('[name=message]').input_value()==''
                assert phone.input_value()=='' and page.locator('[name=buyer]').input_value()==''
                assert not page.locator('[name=same_buyer]').is_checked()
                assert page.locator('[name=attempt]').input_value()==attempt
                assert page.locator('#amount').inner_text()=='110,000원'
                page.locator('[name=sender]').fill('나가기 전 입력')
                before=len(dialogs);page.reload()
                assert dialogs[before:]==['beforeunload']
                assert page.locator('[name=sender]').input_value()==''
                assert page.evaluate('localStorage.length===0 && sessionStorage.length===0')
                results.append({'width':width,'live_summary':True,'edit_focus':True,'character_counts':True,'phone_validation_and_cleanup':True,'receipt_validation_and_modes':True,'error_summary_and_recovery':True,'required_progress':True,'dirty_navigation_warning':True,'mobile_amount_and_review':True,'confirmed_reset':True,'no_personal_browser_storage':True,'no_horizontal_overflow':True})
                context.close()
            for width in (390,1280):
                context=browser.new_context(viewport={'width':width,'height':900})
                context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',handle_dialog)
                page.goto(ORIGIN+'/i/'+job+'/93500/order')
                assert page.locator('#wreath-preview').is_visible()
                assert page.locator('[name=cash_receipt_type]').is_disabled()
                for i,g in enumerate(product['groups']):
                    value='custom' if g['label']=='축하 문구' else next(iter(g['choices']))
                    page.locator('[name=choice'+str(i)+']').select_option(value)
                page.locator('[name=sender]').fill('초대장 보내는 분');page.locator('[name=message]').fill('초대장 자동 족자 문구 확인')
                paint=page.locator('#scroll-greeting canvas')
                shown=paint.get_attribute('aria-label') if paint.count() else page.locator('#scroll-greeting').inner_text()
                assert '초대장자동족자문구확인' in ''.join(shown.split())
                if paint.count():assert paint.evaluate('c=>c.getContext("2d").getImageData(0,0,c.width,c.height).data.some((v,i)=>i%4===3&&v>0)')
                assert product['venue'] in page.locator('#order-review').inner_text()
                assert page.locator('#order-review dt',has_text='현금영수증').count()==0
                page.locator('#order-review').get_by_role('button',name='축하 문구 수정',exact=True).click()
                assert page.locator('[name=message]').evaluate('el=>el===document.activeElement'), page.evaluate('({active:document.activeElement.tagName, hidden:document.getElementById("custom").hidden, disabled:document.querySelector("[name=message]").disabled})')
                page.locator('#order-review').get_by_role('button',name='상품 선택 수정',exact=True).click()
                if width==390:
                    assert page.locator('.mobile-choice').first.evaluate('el=>el.open')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.locator('#order-review').get_by_role('button',name='입력 내용 지우기').click()
                page.get_by_role('dialog').get_by_role('button',name='지우기',exact=True).click()
                assert page.locator('#scroll-greeting').inner_text()==''
                if width==390:assert page.locator('#mobile-option-status').inner_text()=='옵션 0 / 3 선택'
                invitation_results.append({'width':width,'static_installation_summary':True,'legacy_receipt_disabled':True,'live_scroll_preserved':True,'edit_mobile_step':True,'reset_clears_scroll_and_steps':True})
                context.close()
            context=browser.new_context(viewport={'width':390,'height':900});context.route('**/*',route)
            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',handle_dialog)
            page.goto(ORIGIN+'/shop/products/'+fixture.identity)
            handoff_fields=fixture.catalog_fields()
            for name,value in handoff_fields.items():
                if name.startswith('choice'):page.locator('[name='+name+']').select_option(value)
            for name,value in handoff_fields.items():
                control=page.locator('[name='+name+']')
                if not control.count() or control.get_attribute('type')=='hidden':continue
                if name=='consent':control.check()
                elif name.startswith('choice'):continue
                else:control.fill(value)
            assert page.locator('#order').evaluate('f=>f.reportValidity()')
            allow_handoff['enabled']=True;before=len(dialogs)
            with page.expect_navigation():
                page.locator('#order').evaluate('f=>{f.requestSubmit();f.requestSubmit();}')
            assert len(handoffs)==1
            assert dialogs[before:]==[]
            context.close()
            browser.close()
        assert not errors,errors
        assert not writes,writes
        with agent.db() as c:assert c.execute('SELECT count(*) FROM invitation_orders').fetchone()[0]==0
        report={'results':results,'invitation_results':invitation_results,'javascript_errors':errors,'orders_created':0,'unexpected_order_requests':len(writes),'captured_mock_handoffs':len(handoffs),'double_submit_blocked':True,'submit_has_no_leave_warning':True,'sender_preview_mocked':True,'external_requests':0,'real_payment':False}
        Path('test-artifacts/checkout-preflight.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()

if __name__=='__main__':main()
