"""Conservative transient decomposition of explicit, read-only customer requests.

This is a routing aid, never an executor or approval interpreter. Callers must
freshly authorize every child read and must not persist task text. A partial
result always accounts for unsupported/failed clauses rather than claiming all
work completed.
"""
from dataclasses import dataclass
import re

MAX_TASKS = 5
_READ = re.compile(r'조회|보여\s*줘|보여\s*주세요|알려\s*줘|확인해\s*줘|찾아\s*줘')
_WRITE = re.compile(r'(?:취소|결제|등록|수정|변경|입고|환불|삭제|발송|승인|적용|저장|복제)(?:\s*(?:처리|진행))?\s*(?:해|하자|시켜|해주세요|하세요)|(?:문자|카톡|이메일)\s*(?:보내|발송)|바꿔|지워|(?:취소|등록|삭제|발송|입고|환불|결제|수정|변경)(?:하고|한\s*뒤|\s*후)')
# Commas and bare '하고' may be part of an address, name, date or status list.
_SEPARATOR = re.compile(r'\s*(?:[;\n]+|(?:그리고|그다음(?:에)?|다음으로|또한)\s+)\s*')


@dataclass(frozen=True)
class BatchTask:
    kind: str
    text: str


@dataclass(frozen=True)
class BatchProposal:
    state: str
    tasks: tuple[BatchTask, ...] = ()
    message: str = ''


def propose_batch(text):
    """Split only explicit independently actionable clauses; no inherited scope.

    `single` means use the ordinary planner. `clarify` must execute nothing.
    `batch` tasks are transient and each read must use a separate request id.
    """
    if type(text) is not str or not text.strip() or len(text) > 12000:
        raise ValueError('bounded nonempty user request required')
    # This exact ending unambiguously joins command predicates, unlike general
    # Korean conjunctions embedded in a location or list of status conditions.
    prepared = re.sub(r'(조회|보여줘|알려줘|확인해줘)\s*하고\s+', r'\1; ', text)
    clauses = tuple(part.strip(' .') for part in _SEPARATOR.split(prepared) if part.strip(' .'))
    if len(clauses) == 1:
        return BatchProposal('single')
    if len(clauses) > MAX_TASKS:
        return BatchProposal('clarify', message='한 번에 다섯 업무까지 나누어 요청해 주십시오.')
    # Every independent clause must state an action. Never drop a dangling
    # clause such as an address continuation or '그것도'.
    if any(not (_READ.search(part) or _WRITE.search(part)) for part in clauses):
        return BatchProposal('clarify', message='각 업무에서 무엇을 조회하거나 변경할지 알려주십시오.')
    tasks = tuple(BatchTask('unsupported' if _WRITE.search(part) else 'read', part) for part in clauses)
    return BatchProposal('batch', tasks)


_ACCEPTED = frozenset({'ok','empty','clarify','unsupported','unavailable','failed','denied','uncertain','conflict','busy','permission_denied','not_found'})
_SUCCESS = frozenset({'ok','empty'})


def summarize_batch(results):
    """Account for every authorized child result without performing any work.

    Child results must come from the server's read gateway. Unsupported clauses
    should be represented by a synthetic `unsupported` result, never executed.
    This deliberately returns no input text or arbitrary model messages.
    """
    if type(results) not in (tuple, list) or not 2 <= len(results) <= MAX_TASKS:
        raise ValueError('two to five child results required')
    children = []
    for index, result in enumerate(results, 1):
        if type(result) is not dict or result.get('status') not in _ACCEPTED:
            raise ValueError('validated read child status required')
        if result.get('operational_change') is not False or result.get('customer_write_enabled') is not False:
            raise ValueError('explicit read-only child result required')
        children.append({'task': index, 'status': result['status'], 'data': result.get('data')})
    successful = sum(child['status'] in _SUCCESS for child in children)
    blocked = len(children) - successful
    if not blocked:
        status, message = 'ok', f'요청한 조회 {successful}건을 확인했습니다.'
    elif successful:
        status, message = 'partial', f'{successful}개 조회를 확인했습니다. 나머지 {blocked}개 업무는 확인 또는 연결이 필요합니다.'
    else:
        status, message = 'unavailable', f'요청한 {blocked}개 업무를 처리하지 못했습니다. 각 업무의 상태를 확인해 주십시오.'
    return {'status': status, 'message': message, 'data': {'tasks': children, 'completed': successful, 'pending': blocked},
            'operational_change': False, 'customer_write_enabled': False, 'raw_input_retained': False}


class BatchClarification(ValueError):
    """No child may run when a multi-clause request is ambiguous."""


def split_requests(text):
    """Gateway adapter: None for single, bounded tuple for explicit batch.

    Raises BatchClarification before any execution for ambiguous/oversized
    batches. Every returned clause must still pass the normal read planner;
    write clauses intentionally remain in the tuple for unsupported reporting.
    """
    proposal = propose_batch(text)
    if proposal.state == 'single':
        return None
    if proposal.state == 'clarify':
        raise BatchClarification(proposal.message)
    return tuple(task.text for task in proposal.tasks)


def contains_mutation(text):
    return type(text) is str and _WRITE.search(text) is not None
