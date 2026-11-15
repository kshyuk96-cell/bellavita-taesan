"""Server-boundary regression checks, with synthetic current source callbacks."""
import base64
from io import BytesIO
from openpyxl import load_workbook
import unittest
import threading
from manager_bridge.tests import test_conversation_gateway as fixture
from manager_bridge.server import make_server
from manager_bridge.control import request
from manager_bridge.project_adapters import ProviderResult,SOURCE_COMMITS
from manager_bridge.store import Conflict,NotFound,PermissionDenied
from taesan_core import Attachment,Evidence
from taesan_core.adapters import Media,Extracted


class ConversationUsabilityGatewayTests(unittest.TestCase):
    setUp=fixture.ConversationGatewayTests.setUp
    turn=fixture.ConversationGatewayTests.turn
    provider=fixture.ConversationGatewayTests.provider
    def rows(self,rows):
        rows=[dict({'date':'2026-11-15','revision':1},**r) for r in rows]
        self.callback=lambda q:ProviderResult('ok','fixture/new',self.now,{'orders':rows},SOURCE_COMMITS['bellavita-erp'])

    def test_refresh_reads_current_source_and_same_request_stays_idempotent(self):
        self.rows([{'id':'13370','status':'접수','date':'2026-11-15','revision':1}])
        first=self.turn('오늘 접수 주문 조회')
        self.rows([{'id':'13371','status':'접수','date':'2026-11-15','revision':1}])
        second=self.turn('아까 조건으로 다시 봐줘',request_id='refresh')
        self.assertEqual(second['status'],'ok');self.assertEqual(second['data']['result']['items'][0]['id'],'13371')
        calls=len(self.calls)
        self.assertEqual(second,self.turn('아까 조건으로 다시 봐줘',request_id='refresh'))
        self.assertEqual(len(self.calls),calls);self.assertNotEqual(first['snapshot_id'],second['snapshot_id'])

    def test_remove_date_does_not_reappear_through_core_inheritance(self):
        self.turn('오늘 접수 주문 조회');result=self.turn('날짜 빼고')
        self.assertEqual(result['status'],'ok');self.assertNotIn('date',self.calls[-1]['query'])
        self.assertEqual(self.calls[-1]['query']['status'],'접수')

    def test_diff_freshly_observes_source_and_does_not_claim_deletion(self):
        self.rows([{'id':'13370','status':'접수','date':'2026-11-15','revision':1}])
        self.turn('주문 조회')
        self.rows([{'id':'13370','status':'배송완료','date':'2026-11-15','revision':2}])
        result=self.turn('아까랑 뭐가 달라졌어?')
        self.assertEqual(result['status'],'ok');diff=result['data']['comparison']
        self.assertEqual(diff['changed'][0]['id'],'13370');self.assertFalse(diff['source_deletions_inferred'])
        self.assertEqual(len(self.calls),2)

    def test_export_is_exact_result_and_rechecks_authorization(self):
        self.rows([{'id':'13370','sender':'=HYPERLINK("bad")','status':'접수','date':'2026-11-15'}])
        first=self.turn('주문 조회')
        result=self.turn('이거 엑셀로 줘')
        self.assertEqual(result['status'],'ok');data=result['data']['export']
        book=load_workbook(BytesIO(base64.b64decode(data['content_base64'])))
        values=list(book['조회 결과'].values);self.assertIn("'=HYPERLINK(\"bad\")",values[1])
        self.gateway.authorize=lambda *args:False
        with self.assertRaises(PermissionDenied):self.gateway.export(self.manager,'bellavita-erp','erp','conversation',first['snapshot_id'])

    def test_snapshot_expiry_cannot_export(self):
        first=self.turn('주문 조회');self.now+=901
        with self.assertRaises(NotFound):self.gateway.export(self.manager,'bellavita-erp','erp','conversation',first['snapshot_id'])

    def test_selector_refetches_and_rejects_reused_row(self):
        rows=[{'id':'13370','stableUid':'original','revision':1,'status':'접수'}, {'id':'13371','stableUid':'second','revision':1,'status':'접수'}]
        self.rows(rows);first=self.turn('주문 조회');choice=first['choices'][0]
        self.rows([dict(rows[0],stableUid='reused',revision=2),rows[1]])
        with self.assertRaises(Conflict):self.gateway.select(self.manager,'bellavita-erp','erp','conversation','choose',choice['selection_id'],first['snapshot_id'])
        self.assertEqual(len(self.calls),2)

    def test_selector_reads_only_chosen_order_and_other_user_cannot_use(self):
        self.rows([{'id':'13370','stableUid':'one','revision':1,'status':'접수'}, {'id':'13371','stableUid':'two','revision':1,'status':'접수'}])
        first=self.turn('주문 조회');choice=first['choices'][0]
        with self.assertRaises(NotFound):self.gateway.select(self.other,'bellavita-erp','erp','conversation','other',choice['selection_id'],first['snapshot_id'])
        result=self.gateway.select(self.manager,'bellavita-erp','erp','conversation','choose',choice['selection_id'],first['snapshot_id'])
        self.assertEqual(result['status'],'ok');self.assertEqual(result['data']['count'],1)
        self.assertEqual(result['data']['result']['items'][0]['id'],'13370')

    def photo(self):
        self.gateway.media=Media(image=lambda *args:Extracted('주문번호 13370번',Evidence('fixture-photo','photo-receipt',self.now)))
        return self.turn('',attachments=(Attachment('image',b'not-a-real-photo',True),))

    def test_photo_next_turn_uses_only_recent_same_employee_parsed_id(self):
        first=self.photo();self.assertEqual(first['status'],'clarify');self.assertFalse(self.calls)
        result=self.turn('이 주문 조회해줘')
        self.assertEqual(result['status'],'ok');self.assertEqual(self.calls[-1]['query']['id'],'13370')
        with self.store._transaction() as c:
            rows=c.execute('SELECT value FROM manager_read_followups').fetchall()
        self.assertNotIn('주문번호',str([r['value'] for r in rows]));self.assertNotIn('not-a-real-photo',str(rows))

    def test_photo_next_turn_other_employee_expiry_and_other_task_do_not_reuse(self):
        self.photo();result=self.turn('이 주문 조회해줘',token=self.other)
        self.assertNotIn('id',self.calls[-1]['query']) if self.calls else None
        self.now+=301;self.turn('이 주문 조회해줘')
        if self.calls:self.assertNotIn('id',self.calls[-1]['query'])
        self.photo();self.turn('문자 발송해줘');self.turn('이 주문 조회해줘')
        if self.calls:self.assertNotIn('id',self.calls[-1]['query'])

    def test_voice_uncertainty_only_asks_number_and_runs_nothing(self):
        self.gateway.media=Media(audio=lambda *args:Extracted('주문번호 13370번인가 13371번 조회해줘',Evidence('voice','voice-receipt',self.now)))
        result=self.turn('',attachments=(Attachment('audio',b'fixture-voice',True),))
        self.assertEqual(result['message'],'주문번호만 다시 알려주십시오.');self.assertFalse(self.calls)

    def test_multi_task_does_not_claim_write_executed(self):
        result=self.turn('오늘 주문 조회 그리고 문자 발송해줘')
        self.assertEqual(result['status'],'partial');self.assertEqual(result['data']['completed'],1)
        self.assertEqual([t['status'] for t in result['data']['tasks']],['ok','unsupported'])
        self.assertEqual(len(self.calls),1);self.assertFalse(result['operational_change'])

    def test_multi_reads_independent_conditions_and_ambiguous_clause_executes_nothing(self):
        result=self.turn('오늘 접수 주문 조회 그리고 내일 배송완료 주문 조회')
        self.assertEqual(result['status'],'ok');self.assertEqual(len(self.calls),2)
        self.assertNotEqual(self.calls[0]['query']['date'],self.calls[1]['query']['date'])
        self.assertEqual(self.turn('오늘 주문 조회 그리고 그것도')['status'],'clarify')
        self.assertEqual(len(self.calls),2)

    def test_voice_corrected_number_preserves_explicit_date_and_status(self):
        self.gateway.media=Media(audio=lambda *args:Extracted('내일 접수 주문번호 13370번인가 13371번 조회해줘',Evidence('voice','receipt',self.now)))
        self.turn('',attachments=(Attachment('audio',b'fixture',True),))
        result=self.turn('13370번')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(self.calls[-1]['query'],{'id':'13370','date':'2026-11-16','status':'접수','limit':30})

    def test_cached_export_expires_with_source_snapshot(self):
        self.turn('주문 조회');self.turn('엑셀로 줘',request_id='export')
        self.now+=901
        self.assertEqual(self.turn('엑셀로 줘',request_id='export')['status'],'expired')

    def test_new_photo_and_new_audio_clear_old_result_and_photo(self):
        self.turn('주문 조회');self.photo()
        self.assertEqual(self.turn('엑셀로 줘')['status'],'clarify')
        self.gateway.media=Media(audio=lambda *args:Extracted('상품 조회해줘',Evidence('voice','receipt',self.now)))
        self.turn('',attachments=(Attachment('audio',b'voice',True),))
        result=self.turn('이 주문 조회해줘')
        if result['status']=='ok':self.assertNotIn('id',self.calls[-1]['query'])

    def test_conjunctive_mutation_is_not_reported_as_successful_read(self):
        result=self.turn('13370번 취소하고 결과 조회해줘')
        self.assertEqual(result['status'],'unsupported');self.assertFalse(self.calls)

    def test_multiple_voice_tasks_clarify_without_silent_partial_execution(self):
        self.gateway.media=Media(audio=lambda *args:Extracted('오늘 주문 조회 그리고 내일 주문 조회',Evidence('voice','receipt',self.now)))
        result=self.turn('',attachments=(Attachment('audio',b'voice',True),))
        self.assertEqual(result['status'],'clarify');self.assertFalse(self.calls)

    def test_http_export_and_selection_are_strict_authenticated_read_actions(self):
        self.rows([{'id':'13370','status':'접수'},{'id':'13371','status':'접수'}])
        first=self.turn('주문 조회');server=make_server(self.store,port=0);server.conversation_gateway=self.gateway
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1]);prefix='/v1/representative/runtime/conversation/'
        args={'project':'bellavita-erp','channel':'erp','conversation':'conversation'}
        try:
            result=request(base,self.manager,'POST',prefix+'export',dict(args,snapshot_id=first['snapshot_id']))
            self.assertIn('content_base64',result['result'])
            self.assertEqual(request(base,self.worker,'POST',prefix+'export',dict(args,snapshot_id=first['snapshot_id']))['status'],403)
            chosen=dict(args,request_id='http-select',selection_id=first['choices'][0]['selection_id'],generation=first['snapshot_id'])
            self.assertEqual(request(base,self.manager,'POST',prefix+'select',chosen)['result']['data']['count'],1)
            self.assertEqual(request(base,self.manager,'POST',prefix+'select',dict(chosen,roles=['admin']))['status'],400)
        finally:server.shutdown();thread.join(timeout=3);server.server_close()

    def test_snapshot_enrichment_failure_preserves_read_and_never_exports_old_result(self):
        self.turn('주문 조회')
        def unsupported_shape(*args):raise ValueError('fixture unsupported export shape')
        self.gateway.history.remember=unsupported_shape
        result=self.turn('내일 주문 조회')
        self.assertEqual(result['status'],'ok');self.assertNotIn('snapshot_id',result)
        self.assertEqual(self.turn('엑셀로 줘')['status'],'clarify')

    def test_audio_conjunctive_write_is_not_silently_dropped(self):
        self.gateway.media=Media(audio=lambda *args:Extracted('13370번 취소하고 결과 조회해줘',Evidence('voice','receipt',self.now)))
        result=self.turn('',attachments=(Attachment('audio',b'voice',True),))
        self.assertEqual(result['status'],'unsupported');self.assertFalse(self.calls)
