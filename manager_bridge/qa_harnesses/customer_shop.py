"""Manager-owned isolated observer. Never imports customer code on the host."""
import json
import sys
from unittest.mock import patch
sys.path[:0]=['/runtime/python','/work','/work/tests']
CASES={}

def observe(ids,fn):
    try:
        values=fn()
        for key in ids:
            value=values[key]
            CASES[key]={'status':'observed','actual':value['actual'],'evidence':value['evidence']}
    except Exception as exc:
        for key in ids:CASES[key]={'status':'unavailable','reason':'entrypoint_failed','error_type':type(exc).__name__}

def result(value,entrypoint,**evidence):return dict(actual=value,evidence=dict(entrypoint=entrypoint,**evidence))

def fixture(cls,method,fn):
    case=cls(method);case.setUp()
    try:return fn(case)
    finally:case.tearDown();case.doCleanups()

try:
    from test_shop import ShopTest,notification,PRODUCT
    from test_order_operations import OrderOperationsTest
    from test_shop_delivery_suite import DeliverySuiteTest
    import agent,cash_receipts,shop_settlement

    def prices(f):
        f.post_admin(action='save_product',product_id=f.product_id,revision='1',**dict(PRODUCT,price='110000',options='쌀 10kg | 5000'))
        first=f.place(price='1',amount='1',same_buyer='yes');amount=json.loads(f.order()['details'])['amount']
        f.place(price='1',amount='1',same_buyer='yes');count=f.mock_payment.call_count
        f.mock_payment.return_value=('5678','https://payapp.kr/independent');f.offer=f.offers[1]['id']
        second=f.place('b'*32,choice0='2');total=json.loads(f.order('b'*32)['details'])['amount']
        return {'server-price':result(amount,'checkout/start_order',http_status=first[0]),
                'duplicate-order':result(count,'checkout/start_order',observation='mock_gateway_call_count'),
                'price-plus-option':result(total,'checkout/start_order',http_status=second[0])}
    observe(['server-price','duplicate-order','price-plus-option'],lambda:fixture(ShopTest,'test_price_snapshot_and_repeat_issue',prices))

    def refunds(f):
        f.place();event=notification('a'*32,'9001','70',10000,orig_mul_no='1234',orig_price='93500')
        f.notify(event);f.notify(dict(event,pay_state='71'));f.notify(notification('a'*32))
        f.notify(notification('a'*32,'9002','71',5000,orig_mul_no='1234',orig_price='93500'))
        with agent.db() as c:_,_,rows=shop_settlement.entries(c,{})
        return {'refund-remaining':result(rows[0][12],'shop_settlement.entries',settlement_components=rows[0][9:14])}
    observe(['refund-remaining'],lambda:fixture(ShopTest,'test_duplicate_reordered_partial_and_full_cancellation',refunds))

    def receipts(f,tax):
        f.paid(cash=True,tax=tax);calls=[]
        def gateway(fields):
            calls.append(dict(fields));return {'state':'1','cashstno':str(1000+len(calls)),'receipt_no':'INDEPENDENT','cashsturl':'https://payapp.kr/fixture'}
        with f.receipt_env(),patch.object(cash_receipts,'request',side_effect=gateway):
            cash_receipts.process_one(agent.db);cash_receipts.process_one(agent.db)
            original=calls[0]
            values={'cash-tax-exempt':result(original['amt_tax'],'cash_receipts.process_one',tax_type=tax),
                    'cash-tax-conservation':result(original['amt_sup']+original['amt_tax'],'cash_receipts.process_one',supply=original['amt_sup'],tax=original['amt_tax']),
                    'vat-taxable-split':result([original['amt_sup'],original['amt_tax']],'cash_receipts.process_one',tax_type=tax)}
            f.notify(notification('a'*32,'9001','70',10000,orig_mul_no='1234',orig_price='93500'))
            cash_receipts.process_one(agent.db);cash_receipts.process_one(agent.db)
            values['cash-target']=result(calls[-1]['amt_tot'],'cash_receipts.process_one',gateway_command=calls[-1]['cmd'])
            return values
    observe(['cash-tax-exempt','cash-target'],lambda:fixture(OrderOperationsTest,'test_cash_receipt_issue_partial_cancel_reissue_and_full_cancel',lambda f:receipts(f,'TG02')))
    observe(['cash-tax-conservation','vat-taxable-split'],lambda:fixture(OrderOperationsTest,'test_receipt_timeout_recovery_and_reconciliation',lambda f:receipts(f,'TG01')))

    def stages(f):
        f.paid();skip=f.http(f.admin,dict(action='change_stage',order_id='a'*32,version='1',stage='delivered'))[0]
        f.post_admin(action='change_stage',order_id='a'*32,version='1',stage='shipping')
        f.post_admin(action='change_stage',order_id='a'*32,version='2',stage='delivered')
        f.post_admin(action='change_stage',order_id='a'*32,version='3',stage='delivered')
        return {'order-stage':result({'skip_status':skip,'history_count':len(f.rows('shop_stage_history'))},'order_ops.change_stage')}
    observe(['order-stage'],lambda:fixture(OrderOperationsTest,'test_paid_notifications_and_three_stages_are_idempotent',stages))
    observe(['foreign-origin'],lambda:fixture(ShopTest,'test_authorization_origin_and_staff_approval',lambda f:{'foreign-origin':result(f.place_with_origin('https://evil.test')[0],'checkout/order_handler')}))
    def settlement(f):
        f.pay(sender='=HYPERLINK("https://example.test")');f.notify(notification('a'*32,'9001','70',10000,orig_mul_no='1234',orig_price='93500'))
        with agent.db() as c:_,_,rows=shop_settlement.entries(c,{})
        return {'settlement-tuple':result(rows[0][9:14],'shop_settlement.entries')}
    observe(['settlement-tuple'],lambda:fixture(DeliverySuiteTest,'test_settlement_partial_full_request_cancel_and_formula_safety',settlement))
except Exception as exc:
    CASES['__harness__']={'status':'unavailable','reason':'fixture_initialization_failed','error_type':type(exc).__name__}
with open('/outputs/customer-business.json','x') as output:
    json.dump({'schema_version':'customer-business-actuals/v1','project':'bellavita-shop','cases':CASES},output,allow_nan=False,separators=(',',':'))
