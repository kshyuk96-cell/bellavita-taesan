import unittest
import test_order_lifecycle
import order_lifecycle as life,order_assistance as assist,order_operations as ops

class CancellationTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    def confirmation(self,target='배송',evidence='배송 담당자에게 중단을 확인함'):
        return f'주문 취소확인 21380\n대상: {target}\n확인ID: confirm-1\n근거: {evidence}'
    def test_request_is_not_delivery_confirmation(self):
        life.command(self.db,'staff','주문 취소 21380')
        self.assertIn('배송 미확인',assist.command(self.db,'staff','주문 상태 21380'))
    def test_manual_delivery_record_does_not_confirm_other_domains(self):
        life.command(self.db,'staff','주문 취소 21380')
        self.assertIn('직원 수동',life.command(self.db,'staff',self.confirmation()))
        status=assist.command(self.db,'staff','주문 상태 21380')
        for text in ('배송 확인 기록 있음','원장 미확인','공유 미확인','청구 미확인'):self.assertIn(text,status)
        with self.assertRaises(ValueError):ops.save(self.db,'parent',None,None)
    def test_confirmation_requires_request_and_correct_owner(self):
        self.assertIn('먼저',life.command(self.db,'staff',self.confirmation()))
        life.command(self.db,'staff','주문 취소 21380')
        self.assertIn('확인하지 못',life.command(self.db,'other',self.confirmation()))
    def test_planned_result_cannot_be_recorded_as_fact(self):
        life.command(self.db,'staff','주문 취소 21380')
        self.assertIn('실제 취소 확인',life.command(self.db,'staff',self.confirmation(evidence='배송 중단 요청 예정입니다')))
    def test_confirmation_replay_and_conflict_do_not_overwrite(self):
        life.command(self.db,'staff','주문 취소 21380');life.command(self.db,'staff',self.confirmation())
        self.assertIn('이미',life.command(self.db,'staff',self.confirmation()))
        self.assertIn('덮어쓰지',life.command(self.db,'staff',self.confirmation(evidence='다른 담당자에게 확인함')))
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM order_cancel_confirmations').fetchone()[0],1)

    def test_billing_confirmation_does_not_confirm_physical_delivery(self):
        life.command(self.db,'staff','주문 취소 21380')
        life.command(self.db,'staff',self.confirmation(target='청구',evidence='청구 담당자에게 청구 없음 확인'))
        status=assist.command(self.db,'staff','주문 상태 21380')
        self.assertIn('배송 미확인',status);self.assertIn('청구 확인 기록 있음',status)
