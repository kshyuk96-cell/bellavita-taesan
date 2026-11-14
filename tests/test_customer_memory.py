import unittest
from customer_memory import CustomerMemory,enrich_order_input

class CustomerMemoryTests(unittest.TestCase):
    def setUp(self):
        self.memory=CustomerMemory(':memory:');self.addCleanup(self.memory.db.close)
        self.memory.confirm_profile('customer-jht','모의고객','모의협회 회장 모의고객','user-confirmed in this conversation')
        self.memory.bind('verified-kakao-room','room-jht','customer-jht','room explicitly confirmed by user')
    def resolve(self,**kw):return self.memory.resolve('verified-kakao-room','room-jht',**kw)
    def test_link_only_uses_owner_confirmed_default(self):
        result=self.resolve(current_text='https://example.test/wedding/123')
        self.assertEqual(result.sender,'모의협회 회장 모의고객');self.assertEqual(result.source,'confirmed_default')
    def test_same_display_name_is_not_identity(self):
        result=self.memory.resolve('unverified-display-name','모의고객',current_text='https://example.test')
        self.assertEqual(result.status,'review');self.assertEqual(result.sender,'')
    def test_explicit_current_order_wins_and_does_not_change_default(self):
        self.assertEqual(self.resolve(explicit_sender='모의모임 모의고객군수').sender,'모의모임 모의고객군수')
        self.assertEqual(self.resolve().sender,'모의협회 회장 모의고객')
    def test_history_about_another_person_does_not_poison_default(self):
        self.memory.record_history('customer-jht',9252,'전국무총리 모의인사')
        self.assertEqual(self.resolve().sender,'모의협회 회장 모의고객')
    def test_multi_title_fragments_preserve_order(self):
        result=self.resolve(sender_fragments=['모의협회 지회장 모의대표','모의회사A · 모의회사B 회장'])
        self.assertEqual(result.sender,'모의협회 지회장 모의대표\n모의회사A · 모의회사B 회장')
    def test_known_context_selects_approved_variant(self):
        self.memory.confirm_variant('manson','customer-jht','모의모임 모의고객군수','모의모임','user-confirmed variant')
        self.assertEqual(self.resolve(current_text='이번엔 모의모임로 부탁해요').sender,'모의모임 모의고객군수')
    def test_url_path_does_not_select_variant(self):
        self.memory.confirm_variant('manson','customer-jht','모의모임 모의고객군수','모의모임','user-confirmed variant')
        self.assertEqual(self.resolve(current_text='https://example.test/모의모임').source,'confirmed_default')
    def test_unresolved_override_asks_instead_of_using_default(self):
        self.assertEqual(self.resolve(current_text='이번엔 다른 직함으로 해주세요').status,'review')
    def test_conflicting_cues_review(self):
        self.memory.confirm_variant('a','customer-jht','모의모임 모의고객군수','모의모임','confirmed')
        self.memory.confirm_variant('b','customer-jht','다른 모임 모의고객','다른 모임','confirmed')
        self.assertEqual(self.resolve(current_text='모의모임 또는 다른 모임').status,'review')
    def test_source_binding_cannot_silently_switch_customer(self):
        self.memory.confirm_profile('other','다른고객','다른고객','confirmed')
        with self.assertRaises(ValueError):self.memory.bind('verified-kakao-room','room-jht','other','confirmed')
    def test_unparsed_new_sender_does_not_get_silent_default(self):
        self.assertEqual(self.resolve(current_text='보내는사람\n다른 회사 대표 홍길동').status,'review')
    def test_explicit_compact_label_normalized_for_existing_parser(self):
        text,result=enrich_order_input('주문 접수\nhttps://example.test/wedding/123\n보내는분: 다른 회사 대표 홍길동',self.memory,'verified-kakao-room','room-jht')
        self.assertIn('보내는 분: 다른 회사 대표 홍길동',text)
        self.assertEqual(result.source,'current_order')
    def test_link_only_adapter_for_existing_taesan(self):
        text,result=enrich_order_input('https://example.test/wedding/123',self.memory,'verified-kakao-room','room-jht')
        self.assertTrue(text.startswith('주문 접수\n'));self.assertIn('보내는 분: 모의협회 회장 모의고객',text)
        self.assertEqual(result.status,'auto')
    def test_employee_id_not_equated_with_customer(self):
        text,result=enrich_order_input('https://example.test/wedding/123',self.memory,'kakao-channel-employee','employee-01')
        self.assertEqual(result.status,'review');self.assertNotIn('제50대',text)

if __name__=='__main__':unittest.main()
