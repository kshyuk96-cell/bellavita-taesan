"""Short Korean replies rendered from typed server outcomes, never provider prose."""
from datetime import date, datetime
import math
import re
from zoneinfo import ZoneInfo
from taesan_core.core import CoreError, Reply
from .conversation_planner import SAFE_MESSAGES

PROJECT_NAMES = {'bellavita-erp': '벨라비타 ERP', 'bellavita-shop': '벨라비타 쇼핑몰',
                 'hope-sunrise': '희망일출', 'funeral-association': '협회'}
STATUS_NAMES = {'접수': '접수', '제작완료': '제작완료', '배송완료': '배송완료', '미확인': '미확인',
                'requesting': '요청 중', 'waiting': '결제 대기', 'failed': '결제 실패', 'paid': '결제 확인',
                'partial_cancel': '부분 취소', 'cancelled': '취소', 'draft': '초안', 'published': '게시 중',
                '상담 접수': '상담 접수', '상담 중': '상담 중', '견적 작성': '견적 작성',
                '견적 전달': '견적 전달', '예약 요청': '예약 요청', '종결': '종결', 'closed': '종료', 'active': '진행 중'}
MISSING_NAMES = {'id': '주문·자료 번호', 'date': '조회 날짜', 'status': '상태', 'limit': '조회 개수'}
ACTION_NAMES = {'erp.orders': '주문', 'shop.orders': '주문', 'hope.products': '상품', 'hope.inquiries': '문의',
                'association.access': '접근 권한', 'association.obituary': '부고',
                'orders.list': '주문', 'products.list': '상품', 'inquiries.list': '문의',
                'access.status': '접근 권한', 'obituary.get': '부고'}


def _iso_date(value):
    if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):return None
    try:return date.fromisoformat(value).isoformat()
    except ValueError:return None


def _identifier(value):
    # A numeric phone number is not exposed as a supposed order identifier.
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}', value):return None
    if value.isdigit() and len(value) > 8:return None
    return value if len(value) <= 24 else value[:21] + '…'


def _time(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value <= 0:return None
    try:return datetime.fromtimestamp(value, ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M')
    except (ValueError, OverflowError, OSError):return None


def render_reply(reply, project, action=None):
    """Render only recognized statuses/fields; do not echo arbitrary reply.message.

    A row with a completed/cancelled status describes source data and never means
    this request performed that action. The integration registers read tools only.
    """
    if not isinstance(reply, Reply):raise CoreError('typed server reply required')
    if project not in PROJECT_NAMES:raise CoreError('registered business project required')
    data = reply.data if type(reply.data) is dict else {}
    result = data.get('result') if type(data.get('result')) is dict else {}
    source_state = result.get('provider_state', result.get('state', result.get('source_state')))
    if reply.status == 'denied' or source_state in {'denied','permission_denied'}:
        return '조회 권한이 없습니다. 담당 계정의 접근 권한을 확인해 주십시오.'
    if data.get('media_state') == 'not_configured':
        return '사진·음성 인식 연결이 없습니다. 내용을 글로 알려주십시오.'
    if data.get('media_state') == 'failed':
        return '사진·음성 내용을 확인하지 못했습니다. 내용을 글로 알려주십시오.'
    if reply.status == 'unavailable' or source_state in {'unavailable','not_configured'}:
        return '이 업무의 조회 연결이 아직 없습니다.' if source_state == 'not_configured' else '조회 연결을 확인하지 못했습니다. 잠시 후 다시 요청해 주십시오.'
    if reply.status in {'uncertain','stale'} or source_state in {'ambiguous','unknown'}:
        return '조회 근거를 확정하지 못했습니다. 처리 완료로 판단하지 마십시오.'
    if reply.status in {'clarify','unsupported'} and reply.message in SAFE_MESSAGES:
        return reply.message
    if reply.status == 'unsupported':return '이 요청은 현재 조회 범위에 포함되지 않습니다.'
    if reply.status == 'clarify':
        missing = data.get('missing')
        if project=='funeral-association' and missing==['id']:return '부고 번호만 알려주십시오.'
        if type(missing) is list and missing and all(type(v) is str and v in MISSING_NAMES for v in missing):
            return ' · '.join(dict.fromkeys(MISSING_NAMES[v] for v in missing)) + '만 알려주십시오.'
        return '어떤 자료를 조회할지 조금 더 알려주십시오.'
    if reply.status == 'expired':return '이전 조회의 보관 시간이 지났습니다. 새로 조회해 주십시오.'
    if reply.status == 'busy':return '앞선 요청을 확인 중입니다. 잠시 기다려 주십시오.'
    if reply.status not in {'ok','empty'}:return '처리 결과를 확인하지 못했습니다.'
    count = data.get('count')
    if count is None:count = result.get('count')
    if type(count) is not int or count < 0:
        return '조회 건수를 확인하지 못했습니다.'
    if (reply.status == 'empty') != (count == 0):
        return '조회 건수와 결과가 달라 확인이 필요합니다.'
    noun = ACTION_NAMES.get(action, ACTION_NAMES.get(result.get('action'), '자료'))
    prefix = PROJECT_NAMES[project] + ' ' + noun
    query = result.get('query') if type(result.get('query')) is dict else {}
    filters = []
    parsed_date = _iso_date(query.get('date'))
    if parsed_date:filters.append(parsed_date)
    status = query.get('status')
    if type(status) is str and status in STATUS_NAMES:filters.append(STATUS_NAMES[status])
    if filters:prefix += ' · ' + ' · '.join(filters)
    message = prefix + ': ' + str(count) + '건입니다.'
    items = result.get('items')
    if count and type(items) is list:
        ids = [_identifier(item.get('id')) for item in items[:3] if type(item) is dict]
        ids = list(dict.fromkeys(value for value in ids if value))
        if ids:message += ' 번호 ' + ', '.join(ids) + '.'
        statuses = [item.get('status', item.get('state', item.get('stage'))) for item in items[:3] if type(item) is dict]
        statuses = list(dict.fromkeys(STATUS_NAMES[value] for value in statuses if type(value) is str and value in STATUS_NAMES))
        if statuses and not query.get('status'):message += ' 조회 상태 ' + ' · '.join(statuses) + '.'
    total = result.get('total_matched')
    if result.get('truncated') is True:
        if type(total) is int and total >= count:message += ' 전체 ' + str(total) + '건 중 일부입니다.'
        else:message += ' 일부 결과입니다.'
    evidence = data.get('evidence') if type(data.get('evidence')) is dict else {}
    observed = _time(result.get('observed_at', evidence.get('observed_at')))
    if observed:message += ' 기준 ' + observed + ' KST.'
    return message[:360]
