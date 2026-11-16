"""Three-input customer handoff intake. Developer claims are data, never execution proof."""
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit,urlunsplit
import zipfile
from lxml import etree
from .store import Conflict,digest

ROLE_NAMES={'anonymous','member','admin','finance','operator','corporate','owner'}
KINDS={
 'money':('결제·정산',('결제','환불','정산','원금','포인트','payment','refund','billing','discount','tax','금액','가격','수수료','할인','세금','잔액','청구','납부','출금','입금')),
 'access':('권한·보안',('권한','회사','개인정보','인증','permission','access','tenant','privacy','auth','접근','유출','삭제','데이터 손실','password')),
 'booking':('상품·예약',('주문','예약','재고','상품','order','booking','stock','inventory','product')),
 'member':('회원관리',('회원','가입','탈퇴','로그인','member','signup','login')),
 'ui':('화면·사용성',('화면','버튼','모바일','용어','ui','mobile','layout','screen'))}
EXTRA=[('normal','정상 업무 완료','초기값→행동→최종 상태를 화면과 서버에서 대조'),('boundary','빈값·잘못된 타입·경계값','최소/최대/1원 경계·오류 복구를 확인'),('access','본인·다른 사용자·회사·역할','허용된 업무만 접근 가능하며 정보가 노출되지 않음'),('duplicate','중복 클릭·요청·재전달','같은 요청의 예약·차감·적립·승인은1회'),('recovery','타임아웃·중단·재시작','불명확한 결과는 조회 전 재실행하지 않음'),('cancel','취소·되돌리기·원수단 복원','원거래·재고·잔액·원장이 승인된 정책대로 복원'),('regression','연관 기능 회귀','변경 전 정상 기능과 관련 업무 흐름 유지')]


def target_url(value):
    if type(value) is not str or len(value)>2048 or any(c.isspace() for c in value):raise ValueError('bounded application URL required')
    parsed=urlsplit(value)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('application URL without embedded credentials/query/fragment required')
    try:port=parsed.port
    except ValueError:raise ValueError('invalid target port') from None
    return urlunsplit((parsed.scheme,parsed.netloc,parsed.path or '/','',''))


class TestAccounts:
    """Keep raw usernames/passwords in memory; reports receive role/count only."""
    __test__=False
    def __init__(self,rows):
        if type(rows) is not list or len(rows)>30:raise ValueError('bounded test accounts required')
        self._rows=[]
        for row in rows:
            if type(row) is not dict or not {'role','username','password'}<=row.keys() or set(row)-{'role','username','password','otp'}:raise ValueError('role/username/password test account required')
            if row['role'] not in ROLE_NAMES or any(type(row[k]) is not str or not 1<=len(row[k])<=4096 for k in ('username','password')):raise ValueError('typed account fields required')
            self._rows.append(dict(row))
    def manifest(self):return [dict(alias='TEST-ACCOUNT-'+str(i+1),role=r['role'],configured=True,login_verified=False,otp_required=bool(r.get('otp'))) for i,r in enumerate(self._rows)]
    def redact(self,text):
        value=str(text)
        for row in self._rows:
            for key in ('password','otp','username'):
                secret=row.get(key)
                if secret and not (key=='username' and secret in ROLE_NAMES):value=value.replace(secret,'[REDACTED]')
        # Do not persist accidental credential assignments from a Codex checklist.
        return re.sub(r"(?im)([\"']?(?:password|passwd|token|api[_-]?key|secret|비밀번호|암호)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",r'\1"[REDACTED]"',value)
    def __repr__(self):return 'TestAccounts(<redacted>)'


def read_checklist(path):
    path=Path(path)
    if path.stat().st_size>2_000_000:raise ValueError('bounded developer checklist required')
    raw=path.read_bytes()
    if path.suffix.lower()=='.docx':
        with zipfile.ZipFile(path) as archive:
            entry=archive.getinfo('word/document.xml')
            if entry.file_size>4_000_000:raise ValueError('bounded document XML required')
            xml=archive.read(entry)
        if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:raise ValueError('document entities not accepted')
        root=etree.fromstring(xml,etree.XMLParser(resolve_entities=False,no_network=True,load_dtd=False))
        text='\n'.join(''.join(p.itertext()) for p in root.xpath('//*[local-name()="p"]'))
        # itertext includes XML text only; no macros, links or embedded commands execute.
    else:text=raw.decode('utf-8-sig')
    return text


def developer_rows(text,accounts):
    text=accounts.redact(text)
    if len(text)>2_000_000:raise ValueError('bounded checklist required')
    try:parsed=json.loads(text)
    except ValueError:parsed=None
    if type(parsed) is dict and type(parsed.get('tests')) is list:parsed=parsed['tests']
    items=[]
    if type(parsed) is list:
        for row in parsed:
            if type(row) is str:items.append(dict(title=row,status='unknown',expected=None))
            elif type(row) is dict:
                title=row.get('title',row.get('name',row.get('description')))
                if type(title) is not str:raise ValueError('developer test title missing')
                items.append(dict(title=title,status=str(row.get('status','unknown')),expected=row.get('expected')))
            else:raise ValueError('checklist rows must be text or named test records')
    else:
        in_code=False
        for line in text.splitlines():
            if line.strip().startswith('```'):in_code=not in_code;continue
            if in_code or not line.strip() or line.lstrip().startswith('#'):continue
            value=re.sub(r'^\s*(?:[-*+]\s+|\d+[.)]\s*)','',line).strip()
            claimed='claimed_pass' if re.match(r'^\[[xX]\]',value) else 'unknown'
            value=re.sub(r'^\[[ xX]\]\s*','',value)
            if value:items.append(dict(title=value,status=claimed,expected=None))
    if not items or len(items)>300:raise ValueError('1–300 developer test rows required; no silent truncation')
    if any(len(r['title'])>2000 for r in items):raise ValueError('developer title too long; split the checklist without dropping requirements')
    return [dict(id='DEV-'+str(i+1).zfill(3),title=r['title'],developer_claimed_status=r['status'],developer_expected=r['expected'],verified=False,interpretation='developer_statement_not_execution_evidence') for i,r in enumerate(items)]


def category(title):
    low=title.lower()
    for kind,(label,words) in KINDS.items():
        if any(w in low for w in words):return kind,label
    return 'general','일반 업무'


def risk(kind):
    priority='P0' if kind in ('money','access','booking') else 'P1' if kind in ('member','general') else 'P2'
    return dict(priority=priority,impact='금액·권한·데이터·핵심 업무의 잘못된 처리' if priority=='P0' else '업무 완료와 화면 이해에 영향',likelihood='경계·중복·역할·복구 경로에서 발생 가능',recovery='원거래와 초기 상태를 복원하고 동일 조건 재시험 필요',reason='독립 QA 분류; 개발자의 통과 표시나 낮은 중요도로 자동 하향하지 않음')


def prepare(url,text,accounts,*,project='검수 대상 서비스',template='generic'):
    url=target_url(url)
    if accounts.redact(url)!=url:raise ValueError('target URL must not embed private test account values')
    claims=developer_rows(text,accounts);rows=[]
    if template=='hope-tour':
        from .hope_tour_reporting import ROWS
        from .hope_customer_coverage import coverage_contract
        requirements=coverage_contract(dict(project='hope-tour',task='intake',source_revision='0'*40,test_contract_digest='0'*64,adapter_release='pending',generation=1))['rows']
        scope_map={'actual_source_api':'api','independent_ledger':'ledger','shared_test_inventory':'inventory','server_html':'api','supported_browser':'ui','actual_pg':'pg','approved_policy':'policy','human_observation':'human','real_device':'devices','independent_backup_restore':'backup','actual_supplier_inventory':'supplier'}
        for sid,group,priority,title,data,expected,_ in ROWS:
            kind,_=category(title);r=risk(kind);r['priority']=priority
            rows.append(dict(id=sid,group=group,title=title,risk=r,inputs=dict(dataset_ids=data.split(',')),expected=dict(description=expected,approval='customer_reference; unresolved policies require decision'),required=list(requirements[sid]['required']),check_capabilities={k:scope_map[v] for k,v in requirements[sid]['required'].items()},origin='customer_reference',developer_refs=[]))
    elif template=='generic':
        for claim in claims:
            kind,label=category(claim['title']);required=['ui','api']
            if kind=='money':
                required+=['formula','policy']
                if any(k in claim['title'].lower() for k in ('결제','카드','환불','정산','납부','출금','payment','refund','billing')):required+=['ledger','pg']
            if kind=='booking' and any(k in claim['title'].lower() for k in ('예약','재고','booking','inventory','stock')):required+=['inventory']
            if kind=='access':required+=['cross_user']
            rows.append(dict(id='CHK-'+claim['id'][4:],group=label,title=claim['title'],risk=risk(kind),inputs=dict(initial_state='to_be_observed'),expected=dict(description='독립 기대값 확인 필요',approval='unconfirmed; developer expected values are reference only'),required=required,origin='developer_list_for_retest',developer_refs=[claim['id']]))
    else:raise ValueError('registered report template required')
    extensions=[]
    for kind in sorted({category(c['title'])[0] for c in claims}):
        group=KINDS.get(kind,('일반 업무',()))[0]
        for key,title,expected in EXTRA:extensions.append(dict(id='QA-'+kind+'-'+key,group=group,title=title,expected=expected,origin='qa_added',risk=risk(kind),required=['ui','api'],execution_status='NOT RUN'))
    contract=dict(schema='customer-handoff-plan/v1',project=accounts.redact(project),target=url,template=template,scenarios=rows,extensions=extensions,policy_approval='not_inferred',customer_final_approval=False)
    return dict(schema='customer-handoff-intake/v1',project=contract['project'],target=url,received_at=datetime.now(timezone.utc).isoformat(),accounts=accounts.manifest(),developer_claims=claims,checklist_digest=digest(claims),plan=contract,plan_digest=digest(contract),candidate_identity=dict(kind='unverified',source_revision=None),mode='intake_and_preparation_not_completed_test')


def draft_report(intake,*,probe=None,browser=None):
    if digest(intake['plan'])!=intake['plan_digest']:raise Conflict('handoff plan changed')
    rows=[]
    for item in intake['plan']['scenarios']:
        rows.append(dict(test_id=item['id'],requirement_id=item['id'],group=item['group'],priority=item['risk']['priority'],risk=item['risk'],title=item['title'],tested_at=None,performer=None,reviewer=None,deployed_revision=None,environment=dict(url=intake['target'],browser=None,device=None,os=None,pg_mode='unverified'),preconditions=item['inputs'],execution_steps=None,expected=item['expected'],actual=dict(ui=None,server=None,pg=None),status='NOT RUN',evidence=[],defect_ids=[],retest=dict(fixed_revision=None,tested_at=None,actual=None,final_verdict='NOT RUN'),note='실제 실행 증거를 수집하기 전 준비 상태',developer_refs=item['developer_refs']))
    missing=['실제 브라우저 실행과 전후 화면','후보 버전의 신뢰 가능한 식별·실행 전후 일치','서버/API·원장·결제 결과의 필요한 읽기 연결','업무 규칙의 근거와 미확정 항목 확인']
    if not intake['accounts']:missing.insert(0,'테스트 계정의 보안 전달')
    from .hope_tour_reporting import POLICY_PENDING
    policies=list(POLICY_PENDING) if intake['plan']['template']=='hope-tour' else ['업무 규칙의 근거·승인 확인'] if any('policy' in r['required'] for r in intake['plan']['scenarios']) else []
    summary=dict(total=len(rows),counts={'PASS':0,'FAIL':0,'BLOCKED':0,'NOT RUN':len(rows)},executed=0,customer_ready=False,recommendation='검증 준비·미실시; 완료 확인서로 고객 인계 불가',source_revision=None,actual_pg='미검증',usability=dict(participants=0,task_success_rate=None),operational_authorization=False,customer_acceptance_approved=False)
    return dict(schema='customer-handoff-report/v1',document_status='DRAFT_NOT_VERIFIED',project=intake['project'],target=intake['target'],summary=summary,required_evidence={r['id']:r['required'] for r in intake['plan']['scenarios']},scenarios=rows,qa_added_checks=intake['plan']['extensions'],developer_claims=intake['developer_claims'],accounts=intake['accounts'],plan_digest=intake['plan_digest'],missing_evidence=missing,policy_decisions_pending=policies,read_only_probe=probe,browser_readiness=browser,defects=[],formula_report=[],ux_observations=[],approval_gates=[dict(stage=s,status='BLOCKED',reason=reason) for s,reason in [('준비','초기값·역할·환경 확인 전'),('실행','화면과 서버의 실제 실행 증거 없음'),('재시험','결함 수정 버전·연관 회귀 확인 전'),('고객 인계','필수 P0/P1과 미해결 주요 결함 판단 전')]])
