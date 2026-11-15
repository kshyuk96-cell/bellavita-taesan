"""Scoped query reuse and calendar dates; no source mutation is authorized."""
from datetime import datetime, timezone
import unittest
from manager_bridge.conversation_planner import Planner

NOW=datetime(2026,11,14,15,30,tzinfo=timezone.utc).timestamp()  # Sunday in KST

class QueryUsabilityTests(unittest.TestCase):
    def setUp(self):
        self.planner=Planner(clock=lambda:NOW)
        self.context={'tool':'erp.orders','arguments':{'date':'2026-11-16','status':'접수','id':'13370','limit':3}}

    def test_repeat_materializes_query_without_request_receipt_replay(self):
        for text in ('아까 조건으로 다시 봐줘','이전 조회 조건대로 다시 조회해줘','다시 조회','재조회'):
            with self.subTest(text=text):
                result=self.planner.propose('bellavita-erp',text,context=self.context)
                self.assertEqual(result.state,'planned')
                self.assertEqual(result.plan.arguments,self.context['arguments'])
                self.assertFalse(result.plan.follow_up)
                self.assertIsNot(result.plan.arguments,self.context['arguments'])

    def test_repeat_without_prior_query_never_defaults_to_all_rows(self):
        self.assertEqual(self.planner.propose('bellavita-erp','아까 조건으로 다시 봐줘').state,'clarify')

    def test_remove_only_selected_filter(self):
        for text,key in [('날짜 빼고','date'),('상태만 제외','status'),('전체 상태로','status'),('주문번호 해제','id'),('개수 빼줘','limit')]:
            with self.subTest(text=text):
                result=self.planner.propose('bellavita-erp',text,context=self.context)
                self.assertEqual(result.state,'planned')
                self.assertEqual(result.plan.arguments,{k:v for k,v in self.context['arguments'].items() if k!=key})
                self.assertFalse(result.plan.follow_up)
        self.assertEqual(len(self.context['arguments']),4)

    def test_missing_context_and_unsupported_field_not_broadened(self):
        self.assertEqual(self.planner.propose('bellavita-erp','날짜 빼고').state,'clarify')
        ctx={'tool':'association.obituary','arguments':{'id':'x'}}
        self.assertEqual(self.planner.propose('funeral-association','날짜 빼고',context=ctx).state,'clarify')

    def test_source_mutation_is_not_filter_removal(self):
        for text in ('주문번호 삭제해','날짜 빼고 원장 수정해','다시 조회하고 결제해'):
            self.assertEqual(self.planner.propose('bellavita-erp',text,context=self.context).state,'unsupported')

    def test_kst_calendar_uses_week_start_and_explicit_next_week(self):
        for text,date in [('모레 주문 조회','2026-11-17'),('이번 주 토요일 주문 조회','2026-11-14'),('다음 주 토요일 주문 조회','2026-11-21')]:
            self.assertEqual(self.planner.propose('bellavita-erp',text).plan.arguments,{'date':date})

    def test_natural_date_followup_preserves_existing_status(self):
        result=self.planner.propose('bellavita-erp','모레로',context=self.context)
        self.assertTrue(result.plan.follow_up)
        self.assertEqual(result.plan.arguments,{'date':'2026-11-17'})

    def test_weekend_and_multiple_dates_ask(self):
        for text in ('이번 주말 주문 조회','주말 주문 조회','모레 내일 주문 조회','이번 주 토요일 다음 주 토요일 주문 조회'):
            self.assertEqual(self.planner.propose('bellavita-erp',text).state,'clarify')

    def test_unconnected_project_date_does_not_get_ignored(self):
        self.assertEqual(self.planner.propose('bellavita-shop','모레 주문 조회').state,'unsupported')
        self.assertEqual(self.planner.propose('hope-sunrise','이번 주 토요일 상품 조회').state,'unsupported')

    def test_registered_model_cannot_change_materialized_requery(self):
        good=Planner(lambda _: {'tool':'erp.orders','arguments':self.context['arguments'],'follow_up':False},clock=lambda:NOW)
        self.assertEqual(good.propose('bellavita-erp','다시 조회',context=self.context).state,'planned')
        bad=Planner(lambda _: {'tool':'erp.orders','arguments':{},'follow_up':False},clock=lambda:NOW)
        self.assertEqual(bad.propose('bellavita-erp','다시 조회',context=self.context).state,'failed')

    def test_registered_model_cannot_retain_removed_filter(self):
        model=Planner(lambda _: {'tool':'erp.orders','arguments':self.context['arguments'],'follow_up':True},clock=lambda:NOW)
        self.assertEqual(model.propose('bellavita-erp','날짜 빼고',context=self.context).state,'failed')

    def test_bare_weekday_is_not_silently_ignored(self):
        self.assertEqual(self.planner.propose('bellavita-erp','토요일 주문 조회').state,'clarify')

    def test_model_must_preserve_host_resolved_calendar_date(self):
        for args in ({},{'date':'2026-11-16'}):
            model=Planner(lambda _, args=args: {'tool':'erp.orders','arguments':args},clock=lambda:NOW)
            self.assertEqual(model.propose('bellavita-erp','모레 주문 조회').state,'failed')
        model=Planner(lambda _: {'tool':'erp.orders','arguments':{'date':'2026-11-17'}},clock=lambda:NOW)
        self.assertEqual(model.propose('bellavita-erp','모레 주문 조회').state,'planned')
