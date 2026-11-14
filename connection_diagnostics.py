"""Safe summaries of injected read-only connection probes, never configuration.

The server authenticates the role and supplies configuration booleans and probe
callables. User/Sheet/model text cannot define a role, probe, or its evidence.
ProbeEvidence must be constructed after checking an actual read response. This
module cannot independently authenticate a caller's claimed provider evidence.
It never fetches data itself, invokes shell commands, or exposes probe payloads.
"""
from collections.abc import Mapping
from dataclasses import dataclass

from read_resilience import classify_read_error


CONNECTIONS = {'sheets': '구글 시트', 'model': 'AI', 'map': '주소 조회', 'pc': '제작 PC'}
_VISIBLE = {'admin': tuple(CONNECTIONS), 'writer': ('sheets', 'model', 'map'),
            'staff': ('sheets', 'model', 'map')}
_MESSAGES = {
    'healthy': '실제 읽기 응답을 확인했어요.',
    'empty': '읽기는 성공했고 조회 결과는 0건이에요.',
    'missing': '연결 설정이 없어 조회하지 않았어요.',
    'unverified': '실제 읽기 결과가 없어 연결 상태는 미확인이에요.',
    'restricted': '연결 점검은 대표 계정에서 확인해 주세요.',
}


@dataclass(frozen=True)
class ProbeEvidence:
    verified: bool
    empty: bool = False


def diagnose_connections(probes, *, configured, role):
    """Run trusted admin probes once and emit only fixed, safe status fields.

    Probe keys: sheets/model/map/pc. Each read-only probe returns ProbeEvidence
    only after checking its actual provider response. Arbitrary truthy objects,
    booleans or configuration presence cannot count as a healthy connection.
    No retries here: use read_resilience only inside explicitly safe read probes.
    """
    if not isinstance(role, str) or role not in _VISIBLE:
        return []
    probes = probes if isinstance(probes, Mapping) else {}
    configured = configured if isinstance(configured, Mapping) else {}
    results = []
    for name in _VISIBLE[role]:
        status = 'unverified'
        category = None
        if role != 'admin':
            status = 'restricted'
        elif configured.get(name) is not True:
            status = 'missing'
        elif callable(probes.get(name)):
            try:
                evidence = probes[name]()
                if (isinstance(evidence, ProbeEvidence) and evidence.verified is True
                        and type(evidence.empty) is bool):
                    status = 'empty' if evidence.empty else 'healthy'
            except Exception as error:
                failure = classify_read_error(error)
                status, category = 'failed', failure.category
                message = failure.message
        if status != 'failed':
            message = _MESSAGES[status]
        result = {'name': name, 'label': CONNECTIONS[name], 'status': status, 'message': message}
        if category:
            result['category'] = category
        results.append(result)
    return results


def summarize_connections(probes, *, configured, role):
    results = diagnose_connections(probes, configured=configured, role=role)
    if not results:
        return '연결 점검 권한이 없어요.'
    return '\n'.join(f"{item['label']} · {item['message']}" for item in results)
