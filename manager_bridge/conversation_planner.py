"""Read-only conversation proposals from a registered model or explicit local rules.

The registered observer is host configuration. It never receives credentials or
approval authority. Media, transcripts and source content remain untrusted data.
The rule path is intentionally limited and is never scored as native-model NLP.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import math
import re
import time
from zoneinfo import ZoneInfo

from taesan_core.adapters import MediaContext
from taesan_core.core import CoreError, Plan, encoded

CATALOG = {
    'bellavita-erp': {'erp.orders': {'id': 'text', 'date': 'date', 'status': ('접수','제작완료','배송완료','미확인'), 'limit': 'limit'}},
    'bellavita-shop': {'shop.orders': {'id': 'text', 'status': ('requesting','waiting','failed','paid','partial_cancel','cancelled'), 'limit': 'limit'}},
    'hope-sunrise': {
        'hope.products': {'id': 'text','status': ('draft','published'),'limit': 'limit'},
        'hope.inquiries': {'id': 'text','status': ('상담 접수','상담 중','견적 작성','견적 전달','예약 요청','종결'),'limit': 'limit'},
    },
    'funeral-association': {'association.access': {}, 'association.obituary': {'id': 'text'}},
}
# Renderer may echo only these reviewed static strings, never model/source prose.
SAFE_MESSAGES = frozenset({
    '음성 지시를 하나로 확인해 주십시오.',
    '음성 내용을 확인할 연결이 없습니다.',
    '사진으로 어떤 업무를 조회할지 알려주십시오.',
    '조회할 업무를 알려주십시오.',
    '확인할 조회 업무를 알려주십시오.',
    '현재 선택한 회사·업무와 요청 대상이 다릅니다.',
    '이 대화 연결에서는 조회만 가능합니다. 변경은 별도 검토가 필요합니다.',
    '요청한 조건·집계는 아직 이 업무 조회에 연결되지 않았습니다.',
    '미완료에 포함할 원본 상태를 확인해 주십시오.',
    '제작완료와 배송완료 중 어떤 상태인지 알려주십시오.',
    '이 업무의 날짜 조건은 아직 연결되지 않았습니다.',
    '조회할 번호를 하나만 알려주십시오.',
    '대화 해석 연결을 확인할 수 없습니다.',
    '조회 조건을 확인해 주십시오.',
    '이 요청은 현재 조회 범위에 포함되지 않습니다.',
    '대화 해석 결과를 검증하지 못했습니다.',
    '접근 권한과 부고 내용 중 무엇을 조회할지 알려주십시오.',
    '조회할 업무와 필요한 조건을 알려주십시오.',
    '유효한 날짜를 알려주십시오.',
    '조회 날짜를 하나로 알려주십시오.',
    '이 업무는 번호별 조회를 제공하지 않습니다.',
    '조회 개수를 하나로 알려주십시오.',
    '조회 개수는 1~100건으로 알려주십시오.',
    '상담 단계 중 조회할 상태를 알려주십시오.',
    '조회할 상태를 하나로 알려주십시오.',
    '부고 번호만 알려주십시오.',
    '조회 계획을 확인했습니다.',
    '등록된 조회 규칙으로 해석했습니다.',
})

_MUTATION = re.compile(r'(?:취소|결제|등록|수정|변경|입고|환불|삭제|발송|승인)(?:\s*(?:처리|진행))?\s*(?:해|하자|시켜|하라고|해주세요|해줘|하세요|할게|진행해)|(?:문자|카톡)\s*(?:보내|발송)|바꿔|지워|삭제해')
_ACK = re.compile(r'^(?:ㅇㅇ|ㅇㅋ|응|네|예|그래|오케이?|오케|00|좋아|진행해|yes|ok|okay|no|true|false|confirm|approve)[.!?\s]*$',re.I)
_UNSUPPORTED_FILTER = re.compile(r'(?:구로|동대문|수원|천안|아산|부산|서울)(?:\s*(?:센터|출고))?\s*만|(?:구로|동대문|수원)\s*(?:센터|출고)|(?:엑셀|xlsx|다운로드|통계|합계|집계|최다|몇\s*건|몇\s*개|건수)')


@dataclass(frozen=True)
class PlannerResult:
    state: str
    plan: Plan | None
    message: str
    source: str
    draft: Plan | None = None

    def __post_init__(self):
        if self.state not in {'planned','clarify','unsupported','unavailable','failed'} or self.source not in {'rules','registered_model'}:
            raise CoreError('invalid planner result')
        if (self.state=='planned') != isinstance(self.plan,Plan):
            raise CoreError('planned result requires exactly one plan')
        if type(self.message) is not str or len(self.message)>500:
            raise CoreError('bounded planner message required')
        if self.draft is not None:
            if self.state!='clarify' or not isinstance(self.draft,Plan) or self.draft.tool!='association.obituary' or self.draft.arguments or self.draft.follow_up:
                raise CoreError('only a missing-id read clarification may carry a draft')


def _args(project,tool,args):
    specs=CATALOG.get(project,{}).get(tool)
    if specs is None or type(args) is not dict or not args.keys()<=specs.keys():raise CoreError('unsupported read proposal')
    result={}
    for key,value in args.items():
        spec=specs[key]
        if spec=='text':
            if type(value) is not str or not value.strip() or len(value)>150 or any(ord(c)<32 for c in value):raise CoreError('bounded source identity required')
        elif spec=='date':
            if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise CoreError('ISO date required')
            if datetime.strptime(value,'%Y-%m-%d').strftime('%Y-%m-%d')!=value:raise CoreError('valid date required')
        elif spec=='limit':
            if type(value) is not int or not 1<=value<=100:raise CoreError('bounded limit required')
        elif value not in spec or type(value) is not str:raise CoreError('exact source status required')
        result[key]=value
    return result


def _context(project,context):
    if context is None:return None
    if type(context) is not dict or set(context)!={'tool','arguments'}:raise CoreError('exact scoped context required')
    args=_args(project,context['tool'],context['arguments'])
    return {'tool':context['tool'],'arguments':args}


def _project_conflict(project,text):
    targets=set()
    if '희망일출' in text:targets.add('hope-sunrise')
    if re.search(r'장례지도사협회|협회',text):targets.add('funeral-association')
    if re.search(r'erp|이알피',text,re.I):targets.add('bellavita-erp')
    if re.search(r'벨라비타\s*(?:쇼핑몰|몰)',text) or ('쇼핑몰' in text and '희망일출' not in text):targets.add('bellavita-shop')
    if '벨라비타' in text and project not in {'bellavita-erp','bellavita-shop'}:return True
    return bool(targets and targets!={project})


def _id(text):
    values=[]
    patterns=(r'(?:주문\s*번호|주문번호|부고\s*(?:번호|아이디)|아이디|\bid)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})',
              r'(?:^|\s)([0-9]{1,12})\s*번(?:\s|만|은|을|이|$)',
              r'(?:주문|부고)\s*#\s*([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})')
    for pattern in patterns:
        for match in re.finditer(pattern,text,re.I):
            value=match.group(1).rstrip('.:')
            if value.endswith('번') and value[:-1].isdigit():value=value[:-1]
            if value not in values:values.append(value)
    return values


def _filter_delta(project,text,context,today):
    """Recognize only a current read query's explicit field change.

    This bounded exception never renames or edits a source order. Extra verbs,
    source objects, arbitrary fields and missing context are not accepted.
    """
    if not context:return None
    match=re.fullmatch(r'(?:(?:그럼|그러면|그중)\s*)?(?:(?:조회|검색)\s*조건(?:에서|의)?\s*)?'
                       r'(날짜|주문번호|상태)(?:만)?\s*(.+?)\s*(?:으?로)\s*바꿔(?:줘|주세요)?'
                       r'(?:서\s*조회(?:해줘|해주세요|해)?|\s+조회(?:해줘|해주세요|해)?)?[.!?\s]*',text)
    if not match:return None
    field,value=match.groups();value=value.strip();tool=context['tool'];spec=CATALOG[project][tool]
    key={'날짜':'date','주문번호':'id','상태':'status'}[field]
    if key not in spec:return None
    if field=='주문번호' and tool not in {'erp.orders','shop.orders'}:return None
    if key=='date':
        offsets={'오늘':0,'내일':1,'어제':-1}
        if value in offsets:value=(today+timedelta(days=offsets[value])).isoformat()
    elif key=='id':
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',value):return None
    elif key=='status':
        matches=[status for status in spec['status'] if status.replace(' ','')==re.sub(r'\s+','',value)]
        if len(matches)!=1:return None
        value=matches[0]
    try:return Plan(tool,_args(project,tool,{key:value}),True)
    except (CoreError,ValueError):return None


class Planner:
    def __init__(self,observe=None,*,clock=time.time):
        if observe is not None and not callable(observe):raise CoreError('registered planner observer required')
        if not callable(clock):raise CoreError('host clock required')
        self.observe,self.clock=observe,clock

    def _today(self):
        value=self.clock()
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise CoreError('valid host clock required')
        return datetime.fromtimestamp(value,ZoneInfo('Asia/Seoul')).date()

    def _reply(self,state,message,source,*,draft=None):return PlannerResult(state,None,message,source,draft=draft)

    def propose(self,project,text,media=None,context=None):
        if project not in CATALOG or type(text) is not str or len(text)>12000 or '\x00' in text:raise CoreError('bounded project request required')
        if media is not None and not isinstance(media,MediaContext):raise CoreError('typed interpreted media required')
        if media is not None and media.question.strip()!=text.strip():raise CoreError('current media question mismatch')
        context=_context(project,context);today=self._today();source='registered_model' if self.observe else 'rules'
        candidate=text.strip()
        if media and not candidate:
            audio=[o.extraction.text for o in media.observations if o.kind=='audio' and o.status=='ok']
            if len(audio)==1:candidate=audio[0].strip()
            elif len(audio)>1:return self._reply('clarify','음성 지시를 하나로 확인해 주십시오.',source)
            elif any(o.kind=='audio' for o in media.observations):return self._reply('unavailable','음성 내용을 확인할 연결이 없습니다.',source)
            else:return self._reply('clarify','사진으로 어떤 업무를 조회할지 알려주십시오.',source)
        if not candidate:return self._reply('clarify','조회할 업무를 알려주십시오.',source)
        if _ACK.fullmatch(candidate):return self._reply('clarify','확인할 조회 업무를 알려주십시오.',source)
        if _project_conflict(project,candidate):return self._reply('clarify','현재 선택한 회사·업무와 요청 대상이 다릅니다.',source)
        delta=_filter_delta(project,candidate,context,today)
        missing_id=bool(context and context['tool']=='association.obituary' and 'id' not in context['arguments'])
        if missing_id and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',candidate):
            if candidate.casefold() in {'y','n','cancel','delete','update','write','register','pay','refund','send','admin','administrator','root'}:
                return self._reply('clarify','부고 번호만 알려주십시오.',source,draft=Plan('association.obituary',{}))
            delta=Plan('association.obituary',{'id':candidate},True)
        if _MUTATION.search(candidate) and delta is None:return self._reply('unsupported','이 대화 연결에서는 조회만 가능합니다. 변경은 별도 검토가 필요합니다.',source)
        if _UNSUPPORTED_FILTER.search(candidate):return self._reply('unsupported','요청한 조건·집계는 아직 이 업무 조회에 연결되지 않았습니다.',source)
        compact=re.sub(r'\s+','',candidate)
        if re.search(r'미완료|미배송|아직.*완료',candidate):return self._reply('clarify','미완료에 포함할 원본 상태를 확인해 주십시오.',source)
        if project=='bellavita-erp' and '완료' in compact and not ('제작완료' in compact or '배송완료' in compact):
            return self._reply('clarify','제작완료와 배송완료 중 어떤 상태인지 알려주십시오.',source)
        if project!='bellavita-erp' and re.search(r'오늘|내일|어제|\d{4}-\d{2}-\d{2}',candidate):
            return self._reply('unsupported','이 업무의 날짜 조건은 아직 연결되지 않았습니다.',source)
        if len(_id(candidate))>1:return self._reply('clarify','조회할 번호를 하나만 알려주십시오.',source)
        if self.observe is not None:
            request={'project':project,'text':text,'today':today.isoformat(),
                     'catalog':json.loads(encoded(CATALOG[project])),'context':context,
                     'media':media.planner_data() if media else None,
                     'authority':'read_proposal_only','confirmation_allowed':False,'mutation_allowed':False}
            try:value=self.observe(json.loads(encoded(request)))
            except Exception:return self._reply('unavailable','대화 해석 연결을 확인할 수 없습니다.',source)
            try:
                if type(value) is not dict:raise CoreError('typed proposal required')
                if set(value)=={'state','message'} and value['state'] in {'clarify','unsupported'}:
                    if type(value['message']) is not str or len(value['message'])>500:raise CoreError('bounded message required')
                    # Model prose cannot claim actual processing success.
                    return self._reply(value['state'],'조회 조건을 확인해 주십시오.' if value['state']=='clarify' else '이 요청은 현재 조회 범위에 포함되지 않습니다.',source)
                if not {'tool','arguments'}<=value.keys() or not value.keys()<={'tool','arguments','follow_up'}:raise CoreError('exact plan fields required')
                args=_args(project,value['tool'],value['arguments']);follow=value.get('follow_up',False)
                if type(follow) is not bool or follow and (not context or context['tool']!=value['tool']):raise CoreError('current same-tool context required')
                plan=Plan(value['tool'],args,follow)
                if delta is not None and plan!=delta:raise CoreError('explicit query change must preserve scoped read context')
                return PlannerResult('planned',plan,'조회 계획을 확인했습니다.',source)
            except (CoreError,ValueError,TypeError,KeyError):return self._reply('failed','대화 해석 결과를 검증하지 못했습니다.',source)
        if delta is not None:return PlannerResult('planned',delta,'등록된 조회 규칙으로 해석했습니다.',source)
        return self._rules(project,candidate,media,context,today)

    def _rules(self,project,text,media,context,today):
        source='rules';compact=re.sub(r'\s+','',text)
        if re.search(r'미완료|미배송|아직.*완료',text):return self._reply('clarify','미완료에 포함할 원본 상태를 확인해 주십시오.',source)
        tool=None
        if project=='bellavita-erp' and re.search(r'주문|배송|제작|접수|행사|현황',text):tool='erp.orders'
        elif project=='bellavita-shop' and re.search(r'주문|결제|입금|취소',text):tool='shop.orders'
        elif project=='hope-sunrise':
            inquiry=bool(re.search(r'문의|상담|견적|예약\s*요청|종결',text));product=bool(re.search(r'상품|투어|여행',text))
            if inquiry:tool='hope.inquiries'
            elif product:tool='hope.products'
        elif project=='funeral-association':
            access=bool(re.search(r'권한|로그인|인증|세션|접속',text));obituary=bool(re.search(r'부고|빈소|발인',text))
            if access and obituary:return self._reply('clarify','접근 권한과 부고 내용 중 무엇을 조회할지 알려주십시오.',source)
            if access:tool='association.access'
            elif obituary:tool='association.obituary'
        follow=False
        short=bool(re.fullmatch(r'(?:그럼\s*|그러면\s*)?(?:오늘|내일|어제|\d{4}-\d{2}-\d{2}|\d+\s*번|접수|제작\s*완료|배송\s*완료|완료|미확인|결제\s*완료|부분\s*취소|취소|결제\s*대기|초안|공개|게시|상담\s*접수|상담\s*중|견적\s*작성|견적\s*전달|예약\s*요청|종결)(?:만|은|로|으로|도)?[?!.\s]*',text))
        if context and short:
            tool=context['tool'];follow=True
        if tool is None:return self._reply('clarify','조회할 업무와 필요한 조건을 알려주십시오.',source)
        args={};spec=CATALOG[project][tool]
        dates=[]
        for word,offset in (('오늘',0),('내일',1),('어제',-1)):
            if word in text:dates.append((today+timedelta(days=offset)).isoformat())
        for value in re.findall(r'\d{4}-\d{2}-\d{2}',text):
            try:dates.append(datetime.strptime(value,'%Y-%m-%d').date().isoformat())
            except ValueError:return self._reply('clarify','유효한 날짜를 알려주십시오.',source)
        dates=list(dict.fromkeys(dates))
        if len(dates)>1:return self._reply('clarify','조회 날짜를 하나로 알려주십시오.',source)
        if dates:
            if 'date' not in spec:return self._reply('unsupported','이 업무의 날짜 조건은 아직 연결되지 않았습니다.',source)
            args['date']=dates[0]
        ids=_id(text)
        if not ids and media:
            for obs in media.observations:
                if obs.status=='ok':
                    for value in _id(obs.extraction.text):
                        if value not in ids:ids.append(value)
        if len(ids)>1:return self._reply('clarify','조회할 번호를 하나만 알려주십시오.',source)
        if ids:
            if 'id' not in spec:return self._reply('unsupported','이 업무는 번호별 조회를 제공하지 않습니다.',source)
            args['id']=ids[0]
        limits=re.findall(r'(?:상위\s*)?(\d+)\s*(?:건|개)\s*만',text)
        if len(set(limits))>1:return self._reply('clarify','조회 개수를 하나로 알려주십시오.',source)
        if limits:
            limit=int(limits[0])
            if 'limit' not in spec or not 1<=limit<=100:return self._reply('clarify','조회 개수는 1~100건으로 알려주십시오.',source)
            args['limit']=limit
        states=[]
        if tool=='erp.orders':
            for term,state in (('제작완료','제작완료'),('배송완료','배송완료'),('미확인','미확인'),('접수','접수')):
                if term in compact:states.append(state)
            if '완료' in compact and not any(s in states for s in ('제작완료','배송완료')):return self._reply('clarify','제작완료와 배송완료 중 어떤 상태인지 알려주십시오.',source)
        elif tool=='shop.orders':
            rules=((r'부분취소','partial_cancel'),(r'결제완료|paid','paid'),(r'결제대기|입금대기|미결제|waiting','waiting'),(r'결제실패|failed','failed'),(r'요청중|requesting','requesting'))
            for pattern,state in rules:
                if re.search(pattern,compact,re.I):states.append(state)
            if re.search(r'취소(?:된|주문|만)|cancelled',compact,re.I) and '부분취소' not in compact:states.append('cancelled')
            if compact in ('취소','취소만'):states.append('cancelled')
        elif tool=='hope.products':
            if re.search(r'비공개|초안|draft',compact,re.I):states.append('draft')
            elif re.search(r'공개|게시|published',compact,re.I):states.append('published')
        elif tool=='hope.inquiries':
            for state in spec['status']:
                if state.replace(' ','') in compact:states.append(state)
            if '접수된문의' in compact and '상담 접수' not in states:states.append('상담 접수')
            if '완료' in compact:return self._reply('clarify','상담 단계 중 조회할 상태를 알려주십시오.',source)
        if len(set(states))>1:return self._reply('clarify','조회할 상태를 하나로 알려주십시오.',source)
        if states:args['status']=states[0]
        if tool=='association.obituary' and 'id' not in args and not(follow and context['arguments'].get('id')):
            return self._reply('clarify','부고 번호만 알려주십시오.',source,draft=Plan('association.obituary',{}))
        return PlannerResult('planned',Plan(tool,_args(project,tool,args),follow),'등록된 조회 규칙으로 해석했습니다.',source)
