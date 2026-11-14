import unittest
from unittest.mock import patch
import order_intake,order_review_rules as rules
import test_order_lifecycle
import order_lifecycle,order_assistance

class RuleTests(unittest.TestCase):
    def test_location_requires_pair_for_either_changed_field(self):
        before={'venue':'기존 모의홀','address':'서울특별시 강남구'}
        for fields in ({'venue':'새 모의홀'},{'address':'부산광역시 해운대구'}):
            with self.assertRaises(ValueError):rules.check_location_change(before,fields)
        rules.check_location_change(before,{'venue':'새 모의홀','address':'부산광역시 해운대구'})
        rules.check_location_change(before,{'sender':'모의회사'})
    def test_two_orders_split_without_cross_filling(self):
        text='주문 분리 미리보기\n[주문1]\n설치장소: 모의홀A\n보내는분: 모의조직A\n[주문2]\n설치장소: 모의홀B\n보내는분: 모의조직B'
        parts=rules.split_orders(text);self.assertEqual(len(parts),2)
        self.assertNotIn('모의조직B',parts[0]);self.assertNotIn('모의조직A',parts[1])
        self.assertIn('아직 접수',rules.preview(text))
        with patch.object(order_intake,'wedding',side_effect=AssertionError('no network')):
            with self.assertRaises(ValueError):order_intake.extract(text)
    def test_duplicate_or_empty_boundaries_held(self):
        for text in ('[주문1]\nA\n[주문1]\nB','[주문1]\n[주문2]\nB','공통 보내는분: 모의회사\n[주문1]\nA\n[주문2]\nB'):
            with self.assertRaises(ValueError):rules.split_orders(text)
    def test_all_titles_and_single_selection(self):
        titles='모의협회 회장\n모의회사 대표'
        self.assertEqual(rules.sender_text('[모두 인쇄]\n'+titles),titles)
        self.assertEqual(rules.sender_text('[선택: 2]\n'+titles),'모의회사 대표')
        self.assertEqual(rules.sender_text(titles),titles)
    def test_ambiguous_title_held_before_network(self):
        for text in ('모의협회 회장 또는 모의회사 대표','[선택: 3]\n모의협회 회장\n모의회사 대표'):
            with patch.object(order_intake,'wedding',side_effect=AssertionError('no network')):
                with self.assertRaises(ValueError):order_intake.extract('주문 접수\nhttps://bojagicard.com/synthetic\n보내는분: '+text)
    def test_choice_directive_not_printed(self):
        result=order_intake.extract('주문 접수\n설치장소: 모의홀\n보내는분: [선택: 2]\n모의협회 회장\n모의회사 대표')
        self.assertEqual(result['sender'],'모의회사 대표')

class LocationMutationTests(unittest.TestCase):
    setUp=test_order_lifecycle.LifecycleTests.setUp
    row=test_order_lifecycle.LifecycleTests.row
    def test_lifecycle_and_supplement_hold_partial_move(self):
        for command in (order_lifecycle.command,order_assistance.command):
            text='주문 수정 21380' if command==order_lifecycle.command else '주문 보완 21380'
            self.assertIn('함께',command(self.db,'staff',text+'\n설치장소: 새 모의홀'))
            self.assertEqual(self.row()['state'],'saved')
