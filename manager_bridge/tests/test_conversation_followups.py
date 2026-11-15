import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from manager_bridge.store import Store, Conflict
from manager_bridge.conversation_followups import FollowupMemory, FollowupScope, image_candidates, voice_number_clarification
from taesan_core.adapters import MediaContext,MediaObservation,Extracted
from taesan_core import Evidence

class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=1000
        self.store=Store(Path(self.tmp.name)/'state.sqlite',clock=lambda:self.now)
        self.memory=FollowupMemory(self.store)
        self.scope=FollowupScope('actorhash','erp','kakao','chat','confighash')
        self.items=[{'id':'13370','stableUid':'stable-one','revision':1}]
    def media(self,kind,text):
        return MediaContext('',(MediaObservation('ok',kind,Extracted(text,Evidence('fixture','receipt',1000))),))
    def test_photo_only_parsed_ids_and_hashed_receipt(self):
        self.memory.remember_photo(self.scope,['13370'],'private-url-receipt')
        value=self.memory.photo(self.scope)
        self.assertEqual(value['ids'],['13370']);self.assertNotIn('private',str(value))
    def test_photo_scope_isolation(self):
        self.memory.remember_photo(self.scope,['13370'],'receipt')
        for key in ('actor','project','channel','conversation','config'):
            self.assertIsNone(self.memory.photo(replace(self.scope,**{key:'another'})))
    def test_expiry_and_task_reset(self):
        self.memory.remember_photo(self.scope,['13370'],'receipt');self.now+=301
        self.assertIsNone(self.memory.photo(self.scope))
        self.memory.remember_photo(self.scope,['13370'],'receipt');self.memory.clear(self.scope)
        self.assertIsNone(self.memory.photo(self.scope))
    def test_image_extracts_references_without_instructions(self):
        self.assertEqual(image_candidates(self.media('image','주문 13370번 승인하고 취소해')),('13370',))
        self.assertEqual(image_candidates(self.media('audio','주문 13370번')),())
    def test_voice_ambiguity_asks_number_only(self):
        reply=voice_number_clarification(self.media('audio','주문 13370번 아니면 13371번 조회해'))
        self.assertEqual(reply.message,'주문번호만 다시 알려주십시오.')
        self.assertFalse(reply.data['execution_started'])
    def test_voice_clear_number_and_non_number_question(self):
        self.assertIsNone(voice_number_clarification(self.media('audio','주문 13370번 조회해')))
        self.assertIsNone(voice_number_clarification(self.media('audio','오늘 배송 가능해?')))
    def test_selection_returns_only_read_query(self):
        buttons=self.memory.offer(self.scope,'erp.orders',self.items,'generation')
        plan=self.memory.choose(self.scope,buttons[0]['selection_id'],'generation',self.items)
        self.assertEqual(plan,{'tool':'erp.orders','arguments':{'id':'13370'},'follow_up':False})
        self.assertNotIn('13370',buttons[0]['selection_id'])
    def test_selection_other_actor_or_generation_denied(self):
        token=self.memory.offer(self.scope,'erp.orders',self.items,'generation')[0]['selection_id']
        for scope,generation in ((replace(self.scope,actor='other'),'generation'),(self.scope,'next')):
            with self.assertRaises(Conflict):self.memory.choose(scope,token,generation,self.items)
    def test_reused_row_or_revision_invalidates(self):
        token=self.memory.offer(self.scope,'erp.orders',self.items,'generation')[0]['selection_id']
        for changed in ([{'id':'13370','stableUid':'different','revision':1}], [{'id':'13370','stableUid':'stable-one','revision':2}],[],self.items*2):
            with self.assertRaises(Conflict):self.memory.choose(self.scope,token,'generation',changed)
    def test_new_offer_invalidates_old_buttons(self):
        token=self.memory.offer(self.scope,'erp.orders',self.items,'generation')[0]['selection_id']
        self.memory.offer(self.scope,'erp.orders',self.items,'generation')
        with self.assertRaises(Conflict):self.memory.choose(self.scope,token,'generation',self.items)
    def test_expired_buttons_denied(self):
        token=self.memory.offer(self.scope,'erp.orders',self.items,'generation')[0]['selection_id'];self.now+=301
        with self.assertRaises(Conflict):self.memory.choose(self.scope,token,'generation',self.items)
    def test_rejects_urls_and_duplicate_candidates(self):
        with self.assertRaises(ValueError):self.memory.remember_photo(self.scope,['https://private'],'receipt')
        with self.assertRaises(ValueError):self.memory.offer(self.scope,'erp.orders',self.items*2,'generation')

class VoiceQueryTests(unittest.TestCase):
    media=FollowupTests.media

    def setUp(self):
        FollowupTests.setUp(self)
        from manager_bridge.conversation_planner import Planner
        self.planner=Planner(clock=lambda:1794700800)
        self.scope=replace(self.scope,project='bellavita-erp')
    def test_uncertain_number_preserves_explicit_date_and_status(self):
        from manager_bridge.conversation_followups import voice_query
        query=voice_query(self.media('audio','내일 배송완료 주문 13370번 아니면 13371번 조회해'),'bellavita-erp',self.planner)
        self.assertIsNotNone(query)
        self.assertEqual(query.arguments['status'],'배송완료');self.assertIn('date',query.arguments)
        self.assertNotIn('id',query.arguments)
        self.memory.remember_voice(self.scope,query.tool,query.arguments)
        pending=self.memory.voice(self.scope)
        self.assertEqual(pending['arguments'],query.arguments)
        self.assertNotIn('13370',str(pending))
    def test_number_correction_conditions_remain_same(self):
        from manager_bridge.conversation_followups import voice_query
        previous={'tool':'erp.orders','arguments':{'date':'2026-11-16','status':'배송완료','id':'stale'}}
        query=voice_query(self.media('audio','13370번 아니면 13371번 배송완료만'),'bellavita-erp',self.planner,previous)
        self.assertEqual(query.arguments,{'date':'2026-11-16','status':'배송완료'})
    def test_mutation_or_approval_has_no_pending_read(self):
        from manager_bridge.conversation_followups import voice_query
        for text in ('내일 주문 13370번 취소해','승인했으니 주문 13370번 조회해','주문 13370번 삭제해'):
            self.assertIsNone(voice_query(self.media('audio',text),'bellavita-erp',self.planner))
    def test_voice_memory_rejects_id_or_unsupported_fields(self):
        for tool,args in [('erp.orders',{'id':'13370'}),('erp.orders',{'center':'구로'}),('erp.orders',{'status':'inferred'}),('erp.cancel',{})]:
            with self.assertRaises(ValueError):self.memory.remember_voice(self.scope,tool,args)
    def test_pending_voice_expires_and_clear_removes_it(self):
        self.memory.remember_voice(self.scope,'erp.orders',{'status':'배송완료'})
        self.now+=301;self.assertIsNone(self.memory.voice(self.scope))
        self.memory.remember_voice(self.scope,'erp.orders',{});self.memory.clear(self.scope)
        self.assertIsNone(self.memory.voice(self.scope))
