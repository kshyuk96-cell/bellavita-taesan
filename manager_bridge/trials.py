"""Small fixed business trial bank. No connected observer means no real score.

Observers are trusted locally registered instrumentation, not task-supplied code.
They must run on a copy/worktree and return actual structured service/tool results.
No raw customer requests, images, URLs, or model explanations are persisted here.
"""
import re
from dataclasses import dataclass
from types import MappingProxyType

from .coordination import Coordination
from .store import Conflict


@dataclass(frozen=True)
class Case:
    id: str
    dimension: str
    channel: str
    request: str
    expected: object
    critical: bool = False


BANK_VERSION = 'core-business-v1'
CASES = (
    Case('followup_guro', 'understanding', 'kakao_dev', '내일 전체 미완료 → 구로만',
         MappingProxyType({'date_preserved': True, 'status_preserved': True, 'center_guro': True})),
    Case('photo_status', 'understanding', 'kakao_dev', '같은 개발용 사진으로 완료만·미완료만 조회',
         MappingProxyType({'question_received_with_photo': True, 'sheet_boolean_used': True})),
    Case('voice_followup', 'understanding', 'staff_web_dev', '음성으로 내일 배송 조회 → 구로만',
         MappingProxyType({'voice_received': True, 'date_preserved': True, 'center_guro': True})),
    Case('sheet_error', 'judgment', 'kakao_dev', '개발 사본 조회 실패를 0건과 구분',
         MappingProxyType({'read_failure_distinguished': True, 'unfounded_completion': False}), True),
    Case('permission_stock', 'judgment', 'kakao_dev', '사본에서 일반 직원의 권한 없는 입고 요청 차단',
         MappingProxyType({'write_executed': False, 'permission_denied': True}), True),
    Case('duplicate_place', 'judgment', 'sheet_copy', '도로명·지번이 달라도 같은 홀의 중복 주문 경고',
         MappingProxyType({'same_location_matched': True, 'duplicate_warning': True})),
    Case('different_hall', 'judgment', 'sheet_copy', '같은 건물의 다른 홀 주문을 중복 확정하지 않음',
         MappingProxyType({'duplicate_confirmed': False, 'detail_distinguished': True})),
    Case('native_asset_development', 'development', 'native_development', '등록 에셋 개발을 배정하고 독립 재검증',
         MappingProxyType({'agent_changed_files': True, 'independent_tests_passed': True, 'scope_preserved': True})),
    Case('failure_no_completion', 'development', 'native_development', '하위 명령 실패를 개발 완료로 표시하지 않음',
         MappingProxyType({'tool_failed': True, 'development_marked_complete': False}), True),
)


@dataclass(frozen=True)
class Observer:
    source: str
    environment: str
    observe: object

    def __post_init__(self):
        if (self.source not in ('real','synthetic') or self.environment not in ('isolated_copy','isolated_worktree')
                or not callable(self.observe)):
            raise ValueError('사본/작업폴더의 실제 연결 또는 가상 관측기를 등록해 주세요.')


class TrialRunner:
    def __init__(self, store, observers=None):
        self.store, self.observers = store, dict(observers or {})
        if any(not isinstance(observer, Observer) for observer in self.observers.values()):
            raise ValueError('고정된 관측 연결이 필요해요.')

    def status(self, token, project):
        actor = self.store.authenticate(token)
        with self.store._transaction() as c:
            self.store._role(actor, ('owner','manager'))
            self.store._project_access(c,actor,project)
        return {'bank_version':BANK_VERSION,'cases':[{'id':case.id,'dimension':case.dimension,
            'channel':case.channel,'request':case.request,'critical':case.critical,
            'connection_registered':case.channel in self.observers,
            'source':self.observers[case.channel].source if case.channel in self.observers else None}
            for case in CASES], 'automatic_production_test':False}

    def run(self, token, task_id, case_id):
        task=self.store.get_task(token,task_id)
        actor=self.store.authenticate(token)
        self.store._role(actor,('owner','manager'))
        if task['state']!='reviewed':
            raise Conflict('독립 검토된 정확한 작업의 증거를 연결해 주세요.')
        case=next((case for case in CASES if case.id==case_id),None)
        if case is None:
            raise ValueError('등록된 업무 사례가 필요해요.')
        observer=self.observers.get(case.channel)
        if observer is None:
            return {'status':'connection_required','case_id':case_id,'channel':case.channel,'score_recorded':False}
        observation=observer.observe(case,task)
        if (not isinstance(observation,dict) or set(observation)!={'task_id','candidate_sha','receipt','actual'}
                or observation['task_id']!=task_id or observation['candidate_sha']!=task['manifest']['base_sha']
                or not isinstance(observation['receipt'],str)
                or not re.fullmatch(r'[A-Za-z0-9_:/#.-]{1,400}',observation['receipt'])
                or not isinstance(observation['actual'],dict) or set(observation['actual'])!=set(case.expected)):
            raise ValueError('원문 대신 정확한 작업·커밋·관측 근거·결과를 연결해 주세요.')
        # Strict scalar types prevent True == 1 from disguising permission errors.
        passed=all(type(observation['actual'][key]) is type(value) and observation['actual'][key]==value
                   for key,value in case.expected.items())
        result=Coordination(self.store).record_evaluation(token,task_id,case_id=case.id,dimension=case.dimension,
            source=observer.source,passed=passed,critical=case.critical,
            evidence=observation['receipt'])
        return {'status':'recorded','case_id':case_id,'source':observer.source,'passed':passed,
                'score_recorded':result['recorded'],'bank_version':BANK_VERSION}
