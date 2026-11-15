import unittest
from manager_bridge.conversation_batch import propose_batch, summarize_batch, split_requests, BatchClarification


class ConversationBatchTests(unittest.TestCase):
    def test_two_explicit_reads(self):
        result = propose_batch('오늘 접수 주문 조회 그리고 내일 배송완료 주문 보여줘')
        self.assertEqual(result.state, 'batch')
        self.assertEqual([t.kind for t in result.tasks], ['read', 'read'])

    def test_read_and_write_remain_separate(self):
        result = propose_batch('오늘 주문 조회 그리고 13370번 주문 취소해줘')
        self.assertEqual([t.kind for t in result.tasks], ['read', 'unsupported'])

    def test_write_disguised_with_read_is_never_executed(self):
        result = propose_batch('오늘 주문 조회 그리고 내일 주문 조회 후 삭제해줘')
        self.assertEqual(result.tasks[1].kind, 'unsupported')

    def test_address_and_status_lists_are_not_split(self):
        for text in ('서울시 강남구, 신관 3층 주문 조회', '접수하고 배송완료 주문 조회', '접수 및 배송완료 주문 조회'):
            self.assertEqual(propose_batch(text).state, 'single')

    def test_dangling_clause_is_clarified_without_dropping(self):
        result = propose_batch('오늘 주문 조회 그리고 내일도')
        self.assertEqual(result.state, 'clarify')
        self.assertFalse(result.tasks)

    def test_semicolon_and_newline(self):
        self.assertEqual(len(propose_batch('주문 조회; 상품 조회\n상담 조회').tasks), 3)

    def test_task_limit_and_nonempty_validation(self):
        self.assertEqual(propose_batch(';'.join(['주문 조회'] * 6)).state, 'clarify')
        for invalid in ('', None, 'x' * 12001):
            with self.assertRaises(ValueError): propose_batch(invalid)

    def test_partial_result_never_claims_everything_completed(self):
        result = summarize_batch([self.child('ok'), self.child('unsupported')])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['data']['completed'], 1)
        self.assertEqual(result['data']['pending'], 1)
        self.assertNotIn('완료', result['message'])

    def test_empty_is_successful_read_not_failed(self):
        result = summarize_batch([self.child('empty'), self.child('ok')])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['completed'], 2)

    def test_failures_and_permissions_individually_preserved(self):
        result = summarize_batch([self.child('denied'), self.child('unavailable')])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual([t['status'] for t in result['data']['tasks']], ['denied', 'unavailable'])
        self.assertEqual(result['data']['completed'], 0)

    def test_not_echo_arbitrary_child_messages(self):
        child = self.child('ok'); child['message'] = 'everything changed secret raw transcript'
        self.assertNotIn('secret', str(summarize_batch([child, self.child('empty')])))

    def test_invalid_mutation_or_unknown_status_rejected(self):
        for child in ({'status': 'ok'}, {'status': 'ok', 'operational_change': True, 'customer_write_enabled': False}, self.child('made_up')):
            with self.assertRaises(ValueError): summarize_batch([child, self.child('ok')])

    def test_gateway_split_contract(self):
        self.assertIsNone(split_requests('오늘 주문 조회'))
        self.assertEqual(split_requests('오늘 주문 조회 그리고 주문 취소해줘'), ('오늘 주문 조회', '주문 취소해줘'))
        with self.assertRaises(BatchClarification): split_requests('주문 조회 그리고 그것도')

    @staticmethod
    def child(status):
        return {'status': status, 'operational_change': False, 'customer_write_enabled': False, 'data': {'count': 0}}
