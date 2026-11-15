"""Customer guidance in non-Korean device timezones, no order submissions."""
import datetime as dt
import json
import re
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from test_shop_catalog import CatalogTest
from test_shop import ORIGIN
import agent
import checkout


def main():
    fixture=CatalogTest('test_only_four_public_products_and_native_options');fixture.setUp()
    errors=[];posts=[];results=[];legacy=[]
    today=dt.datetime.now(checkout.SEOUL).date()
    now=dt.datetime.combine(today,dt.time(23,50),checkout.SEOUL)
    tomorrow=dt.datetime.combine(today+dt.timedelta(days=1),dt.time(0,10),checkout.SEOUL)
    when=tomorrow.strftime('%Y-%m-%dT%H:%M')
    job='d'*32;folder=fixture.root/'invitations'/job;folder.mkdir(parents=True)
    product=dict(fixture.product,event_time=tomorrow.isoformat(),date=tomorrow.date().isoformat(),clock='00:10',wedding_photo=ORIGIN+'/checkout-art/rice-10kg.webp');product.pop('cash_tax_type',None)
    (folder/'93500.order.json').write_text(json.dumps(product,ensure_ascii=False))
    try:
        upstream='http://127.0.0.1:'+str(fixture.server.server_address[1])
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
            for width,timezone in [(390,'America/Los_Angeles'),(1280,'Asia/Tokyo')]:
                context=browser.new_context(viewport={'width':width,'height':900},timezone_id=timezone)
                def route(request):
                    req=request.request
                    if not req.url.startswith(ORIGIN+'/'):return request.abort()
                    if req.url==ORIGIN+'/checkout-sender':return request.fulfill(status=200,content_type='application/json',body=json.dumps({'group':['보내는 분']}))
                    if req.method!='GET':posts.append(req.url);return request.abort()
                    response=request.fetch(url=upstream+req.url[len(ORIGIN):])
                    if 'text/html' in response.headers.get('content-type',''):
                        body=re.sub(r'data-server-now="[^"]+"','data-server-now="'+str(now.timestamp())+'"',response.text())
                        return request.fulfill(response=response,body=body)
                    request.fulfill(response=response)
                context.route('**/*',route);page=context.new_page();page.clock.install();page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
                page.goto(ORIGIN+'/shop/products/'+fixture.identity)
                next_button=page.locator('#mobile-next-required' if width==390 else '#next-required')
                next_button.click();assert page.locator('[name=choice0]').evaluate('el=>el===document.activeElement')
                for i,g in enumerate(fixture.source['groups']):page.locator('[name=choice'+str(i)+']').select_option(next(iter(g['choices'])))
                size=page.locator('[name=choice0]');choices=list(fixture.source['groups'][0]['choices'])
                size.select_option(choices[1])
                assert '기본 판매가 110,000원' in page.locator('#price-breakdown').inner_text()
                assert '추가금 55,000원' in page.locator('#price-breakdown').inner_text()
                assert '55,000원 증가' in page.locator('#option-price-change').inner_text()
                assert '3호' in page.locator('.option-explanation').first.inner_text()
                size.select_option(choices[0]);assert '55,000원 감소' in page.locator('#option-price-change').inner_text()
                size.select_option(choices[1]);assert page.locator('#amount').inner_text()=='165,000원'
                address=page.locator('[name=address]');address.fill('가'*200)
                expect(page.locator('#address-count')).to_have_text('200 / 200자')
                assert 'address-count' in address.get_attribute('aria-describedby')
                event=page.locator('[name=event_time]');event.fill(now.strftime('%Y-%m-%dT%H:%M'));event.blur()
                assert not event.evaluate('el=>el.validity.valid')
                assert '한국 시간' in event.locator('..').locator('.field-error').inner_text()
                event.fill(when);event.blur();assert event.evaluate('el=>el.validity.valid')
                expect(page.locator('#event-countdown')).to_have_text('내일 행사 · 한국 시간 기준')
                page.locator('[name=buyer]').fill('  주문자  ');page.locator('[name=buyer]').blur()
                assert page.locator('[name=buyer]').input_value()=='주문자'
                page.locator('[name=same_buyer]').check();page.locator('[name=sender]').fill('  보내는 분  ');page.locator('[name=sender]').blur()
                assert page.locator('[name=sender]').input_value()=='보내는 분';assert page.locator('[name=buyer]').input_value()=='보내는 분'
                assert page.locator('[name=buyer]').locator('..').locator('.field-requirement').inner_text()=='자동 입력'
                page.locator('[name=venue]').fill('  테스트 홀  ');page.locator('[name=venue]').blur()
                address.fill('  서울  강남구 1  ');address.blur();assert address.input_value()=='서울  강남구 1'
                assert page.locator('[name=venue]').input_value()=='테스트 홀'
                phone=page.locator('[name=buyer_phone]');phone.fill('123');phone.blur()
                assert phone.get_attribute('aria-invalid')=='true';assert phone.locator('..').locator('.field-error').is_visible()
                phone.fill('01012345678');phone.blur();assert phone.get_attribute('aria-invalid') is None
                assert phone.locator('..').locator('.field-error').is_hidden()
                assert phone.locator('..').locator('.field-requirement').inner_text()=='필수'
                assert page.locator('[name=cash_receipt_identifier]').locator('..').locator('.field-requirement').inner_text()=='선택'
                page.locator('[name=extra0]').fill('없음');page.locator('[name=message]').fill('<b>축하합니다</b>');page.locator('[name=consent]').check()
                assert page.locator('#order').evaluate('f=>f.reportValidity()')
                next_button.click();assert page.locator('#order-review').evaluate('el=>el===document.activeElement')
                assert page.locator('#order-review b').count()==0
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                Path('test-artifacts').mkdir(exist_ok=True);page.screenshot(path=f'test-artifacts/checkout-guidance-batch04-{width}.png',full_page=True)
                page.clock.fast_forward(30*60*1000)
                assert not event.evaluate('el=>el.validity.valid')
                assert '지났습니다' in page.locator('#event-countdown').inner_text()
                assert '55,000원 증가' in page.locator('#option-price-change').inner_text()
                page.locator('.preflight-reset').click();page.get_by_role('dialog').get_by_role('button',name='지우기',exact=True).click()
                assert page.locator('#option-price-change').inner_text()==''
                assert page.locator('#address-count').inner_text()=='0 / 200자'
                assert page.locator('.field-error:visible').count()==0
                page.goto(ORIGIN+'/i/'+job+'/93500/order')
                expect(page.locator('#event-countdown')).to_have_text('내일 행사 · 한국 시간 기준')
                next_button=page.locator('#mobile-next-required' if width==390 else '#next-required');next_button.click()
                if width==390:
                    assert page.locator('.mobile-choice').first.evaluate('el=>el.open')
                    assert page.locator('.mobile-choice').first.locator('.field-error').is_visible()
                    assert page.locator('.mobile-choice').first.locator('.field-requirement').inner_text()=='필수'
                for i,g in enumerate(product['groups']):page.locator('[name=choice'+str(i)+']').select_option('custom' if g['label']=='축하 문구' else list(g['choices'])[-1] if g['label']=='상품 선택' else next(iter(g['choices'])))
                assert '30kg' in page.locator('.option-explanation:visible').first.inner_text()
                page.locator('[name=message]').fill('초대장 문구 확인');page.locator('[name=sender]').fill('보내는 분')
                assert page.locator('[name=cash_receipt_type]').is_disabled()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.evaluate('localStorage.length===0&&sessionStorage.length===0')
                legacy.append({'width':width,'static_event_kst_day':True,'mobile_step_focus':True,'rice_weight_explanation':True,'legacy_receipt_disabled':True})
                results.append({'width':width,'device_timezone':timezone,'option_breakdown':True,'price_change_delta':True,'address_counter':True,'past_event_rejected':True,'kst_midnight_boundary':True,'event_expiring_while_open':True,'frame_size_hint':True,'required_optional_markers':True,'inline_error_recovery':True,'trim_preserves_internal_spaces':True,'next_field_and_summary_focus':True,'reset_clears_guidance':True,'horizontal_overflow':False})
                context.close()
            browser.close()
        assert not errors,errors;assert not posts,posts
        with agent.db() as c:assert c.execute('SELECT count(*) FROM invitation_orders').fetchone()[0]==0
        report={'results':results,'invitation_results':legacy,'javascript_errors':errors,'orders_created':0,'actual_provider_calls':0,'browser_personal_storage_used':False,'clock_scope':'synthetic server-render time and virtual elapsed time; actual server clock unchanged'}
        Path('test-artifacts/checkout-guidance-batch04.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:fixture.tearDown()

if __name__=='__main__':main()
