import unittest
from unittest.mock import patch
import order_intake
from customer_memory import CustomerMemory
import test_customer_orders
from customer_orders import prepare

class DefaultGapTests(unittest.TestCase):
    setUp = test_customer_orders.ForwardTests.setUp
    def test_no_current_invitation_cannot_use_history(self):
        for text in ('주문 접수','https://unknown.example/private','https://bojagicard.com/a\nhttps://bojagicard.com/b'):
            with self.subTest(text=text),self.assertRaises(ValueError):prepare('고객 주문 room1 sender1\n'+text,self.path)

class MemoryGapTests(unittest.TestCase):
    def setUp(self):
        self.m=CustomerMemory(':memory:');self.addCleanup(self.m.db.close)
        self.m.confirm_profile('a','모의A','모의협회 회장','test');self.m.bind('r','s','a','test')
        self.m.confirm_variant('v','a','모의모임 회장','모의모임','test')
    def test_negative_cue_is_not_positive_selection(self):
        for text in ('모의모임 말고','모의모임 제외','모의모임 빼고','모의모임 쓰지 마세요','모의모임 취소'):
            with self.subTest(text=text):self.assertEqual(self.m.resolve('r','s',current_text=text).status,'review')
    def test_variant_id_cannot_transfer_customer(self):
        self.m.confirm_profile('b','모의B','모의회사 대표','test')
        with self.assertRaises(ValueError):self.m.confirm_variant('v','b','모의회사 대표','모의회사','test')
        self.assertEqual(self.m.resolve('r','s',current_text='모의모임').sender,'모의모임 회장')

class QuantityGapTests(unittest.TestCase):
    def test_multi_quantity_held_before_network(self):
        with patch.object(order_intake,'wedding',side_effect=AssertionError('network should not run')):
            for value in ('2개','10','0','-1','미정','1 또는 2'):
                with self.subTest(value=value),self.assertRaises(ValueError):order_intake.extract('주문 접수\nhttps://bojagicard.com/test\n수량: '+value+'\n보내는분: 모의회사')
    def test_single_quantity_is_not_part_of_inscription(self):
        result=order_intake.extract('주문 접수\n설치장소: 모의홀\n보내는분: 모의회사\n수량: 1개')
        self.assertEqual(result['sender'],'모의회사')
