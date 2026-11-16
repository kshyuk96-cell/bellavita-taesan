"""Customer's final 38-case format, preparation gates, defects and retest exports."""
from datetime import datetime
from zoneinfo import ZoneInfo
from taesan_core.hope_tour_qa import REFERENCE_SHA256, FORMAT_SHA256, POLICY_VERSION

# Final customer document §6–7. IDs are fixed; missing tests remain in denominator.
ROWS=[
 ('MEM-01','회원관리','P0','3등급 최초 납부·월회비/가입비 분리','U01,U02,U03','75,000 / 150,000 / 300,000원; 원금은 월회비만','signup'),
 ('MEM-02','회원관리','P0','카드 거절 시 미활성·미적립','U01,C02','납부 대기; DP=BP=0','declined'),
 ('MEM-03','회원관리','P0','원금 누계/사용/잔액·BP/SP 구분','U04','원금·무상 포인트 별도 표시',None),
 ('MEM-04','회원관리','P0','정기청구·다음 회차·원장 1회','U01,C01','월 청구와 원금 적립 각 1회','monthly-replay'),
 ('MEM-05','회원관리','P1','미납·재시도·휴면·정지·알림','U08','실패 횟수별 승인된 상태 전환',None),
 ('MEM-06','회원관리','P1','등급 변경 적용일·예정 청구','U01,U02,U03','적용일·혜택·청구액 일치',None),
 ('MEM-07','회원관리','P0','탈퇴·정기결제 중단·원금 상한','U09','반환 750,000원 이하; BP 제외','withdrawal-ceiling-runtime'),
 ('MEM-08','회원관리','P0','자격 중지 정책','U08','정지≠자동 전액 환불','suspension-no-auto-refund'),
 ('MEM-09','회원관리','P1','추천 중복·취소·회수','U01','원 보상별 중복 지급/회수 없음',None),
 ('MEM-10','회원관리','P1','약어 전체 명칭·모바일 도움말','U10','DP/BP/SP/CP 등 전체 명칭·누를 수 있는 설명','terms-expanded'),
 ('PRD-01','상품','P0','관리자 등록→고객 노출·수정','P01','서버에 저장된 동일 상품 노출','product-cross-device'),
 ('PRD-02','상품','P0','등급·옵션·객실별 총액','P01,P02,P04','표시=서버 견적=결제 총액',None),
 ('PRD-03','상품','P0','신청·확보·확정·관리자 조회','P02,U04','예약 1건, 재고 1석 차감',None),
 ('PRD-04','상품','P0','마지막 1석 동시신청','P02','확정 합계 1 이하; 초과예약 없음','inventory-concurrent'),
 ('PRD-05','상품','P1','매진/마감 차단·대체 안내','P03','신청 차단과 다음 행동 안내',None),
 ('PRD-06','상품','P1','예약금·잔금·미납·기한','P04','분할청구 합계·확정 상태 일치','booking-confirmed'),
 ('PRD-07','상품','P0','취소 후 재고·원수단 복원','P02,R01','DP/BP/SP·카드·재고 원거래별 복원','refund-restoration'),
 ('PRD-08','상품','P0','교차 브라우저 동일 서버 데이터','P01,U04','상품·예약·원장 동일; 타인 접근 금지','product-cross-device'),
 ('PAY-01','결제','P0','원금 우선 혼합결제','U04,P02','DP600000→BP100000→SP100000→카드200000','principal-first-runtime'),
 ('PAY-02','결제','P0','원금 충분','U06','DP 전액, BP/SP 차감 0',None),
 ('PAY-03','결제','P0','원금 0','U05','추가 포인트/카드 합계=상품가',None),
 ('PAY-04','결제','P0','등급×상품 BP 상한','U01,U02,U03,P01,P02,P03','희망15/10/50%;행복20/15/70%;프리미엄25/18/100%','bp-limit-approved'),
 ('PAY-05','결제','P0','카드 실패 후 중복차감 방지','C02,U04','실패 후 원금·포인트·재고 손실 없음',None),
 ('PAY-06','결제','P0','더블클릭·재시도·중복콜백','C04','승인·적립·예약 각각 1회','monthly-replay'),
 ('PAY-07','결제','P0','응답 불명 조회 후 복구','C03','확인 전 재청구 금지, 원거래 대조','payment-unknown'),
 ('PAY-08','결제','P0','부분취소·원수단·PG/원장','C05,R02','원수단 배분 정책 승인 후 대사','refund-pg-ledger'),
 ('PAY-09','결제','P0','본인 원금 반환 상한','U09','max(0,1200000-450000)=750000;BP85000 제외','withdrawal-ceiling-runtime'),
 ('PAY-10','결제','P0','PG 승인·취소·정산 대사','C01,C05','PG 테스트 기록과 거래별 원장 일치','actual-pg'),
 ('UX-01','UX·보안','P1','첫 방문 고객 무설명 완료','U01,P02','성공률·소요시간·오류·도움요청 실측',None),
 ('UX-02','UX·보안','P1','360px 모바일 가독성','U10','360/390/768/1280px 및 실기기 기록','mobile-help'),
 ('UX-03','UX·보안','P1','오류 원인·수정·다음 행동','C02','구체적이고 복구 가능한 오류 안내',None),
 ('SEC-01','UX·보안','P0','회원의 관리자·재무 접근 거절','U10','인증·역할·회사/본인 범위 차단','member-admin-denied'),
 ('SEC-02','UX·보안','P0','카드번호 미저장·마스킹·로그','U10','PAN/CVC 미보관, 개인정보 접근 기록','mfa-audit-privacy'),
 ('OPS-01','UX·보안','P1','백업복구·배포 후 핵심 회귀','P01,U01','별도 복원 환경·정합성·회귀 증거','backup-restore'),
 ('E2E-01','통합','P0','원금 우선 혼합결제 통합','U04,P02','원금/포인트/카드/재고/예약/원장 일치','principal-first-runtime'),
 ('E2E-02','통합','P0','취소·환불 통합','U04,P02,R01','원수단/재고/화면/PG/원장 일치','refund-restoration'),
 ('E2E-03','통합','P0','탈퇴 통합','U09','청구 중단→750000원 한도→승인·감사','withdrawal-ceiling-runtime'),
 ('E2E-04','통합','P0','마지막 재고·타임아웃 복구','U04,P02,C03','확보 만료·원거래 조회·중복청구 없음','payment-unknown')]
POLICY_PENDING=['BP/SP 상대 순서','CP 사용·반환 조건','가입비 반환 여부','자격 중지 시 청구·반환 시점','취소수수료 원수단별 배분']


def final_report(evaluation,collected,*,engine_revision,sut_revision,reviewer=None):
    now=datetime.now(ZoneInfo('Asia/Seoul')).isoformat()
    diagnostics={r['id']:r for r in evaluation['report']['scenarios']}
    results=[]
    for sid,group,priority,title,data,expected,check in ROWS:
        observed=diagnostics.get(check)
        # API-only evidence cannot satisfy the customer's paired UI/server proof.
        status='NOT RUN';note='실행 증거 없음'
        if observed and observed['status']=='fail':
            status='FAIL';note='관찰한 기대값 불일치; 증거 수준은 별도 명시'
        elif observed and observed['status'] in ('pass','blocked'):
            status='BLOCKED';note='일부 HTTP 검사만 수행; 화면·추가 원장/PG 증거 미확보'
        elif sid in ('MEM-08','PAY-08'):
            status='BLOCKED';note='정책결정 대기'
        results.append(dict(test_id=sid,requirement_id=sid,priority=priority,group=group,title=title,
            tested_at=now if status in ('PASS','FAIL','BLOCKED') and observed and observed['executed'] else None,
            performer='자동 검증기',reviewer=reviewer,deployed_revision=sut_revision,
            environment={'url':'https://hope-tour-feedback.shrewdmaxim1.chatgpt.site/review/',
                'browser':None,'device':None,'os':None,'pg_mode':'simulator'},
            preconditions={'dataset_ids':data.split(','),'restoration':'새 cookie jar의 격리 검수 공간; 운영 자료 초기화 없음'},
            execution_steps=observed.get('evidence',{}).get('steps') if observed else None,
            expected=expected,actual={'ui':None,'server':observed['actual'] if observed else None,'pg':None},
            status=status,evidence=observed.get('evidence') if observed else None,
            note=note,defect_ids=['BUG-TERM-001'] if sid=='MEM-10' and status=='FAIL' else [],
            retest={'fixed_revision':None,'tested_at':None,'actual':None,'final_verdict':'NOT RUN'}))
    defects=[]
    if diagnostics.get('principal-first-declaration',{}).get('status')=='fail':
        defects.append(dict(id='BUG-POLICY-001',scenario_ids=['PAY-01','E2E-01'],severity='S1',priority='P0',
            kind='developer_declaration_conflict_not_runtime_proof',
            symptom='FB-13의 SP→BP→DP→카드 표기가 고객 원금 우선 기준과 충돌',
            reproduce=['검수 상태 JSON 조회','FB-13 label과 고객 §7/최종 §4 대조'],
            actual=diagnostics['principal-first-declaration']['actual'],expected='DP',
            owner=None,status='OPEN',due_date=None,fixed_revision=None,retest='NOT RUN'))
    if diagnostics.get('terms-expanded',{}).get('status')=='fail':
        defects.append(dict(id='BUG-TERM-001',scenario_ids=['MEM-10'],severity='S3',priority='P1',
            kind='server_html_text',symptom='고객이 지정한 전체 명칭(약어) 표기를 공개 검수 화면에서 모두 확인할 수 없음',
            reproduce=['검수·개발현황·앱 HTML의 본문 추출','전체 명칭(약어) 목록과 대조'],
            actual=diagnostics['terms-expanded']['actual'],expected={k:True for k in diagnostics['terms-expanded']['actual']},
            owner=None,status='OPEN',due_date=None,fixed_revision=None,retest='NOT RUN'))
    counts={s:sum(r['status']==s for r in results) for s in ('PASS','FAIL','BLOCKED','NOT RUN')}
    denominator=counts['PASS']+counts['FAIL']
    unresolved=sum(d['severity'] in ('S1','S2') and d['status']!='CLOSED' for d in defects)
    gates=[dict(stage=stage,status='BLOCKED',reason=reason) for stage,reason in [
        ('① 계획','요구사항·기대값 작성 완료; 발주사 사전 승인 미기록'),
        ('② 준비','고정 데이터 정의 완료; U04/P02 등 지정 데이터와 모든 기기 준비 미확인'),
        ('③ 실행','일부 격리 HTTP 관찰만 확보; 화면/PG/실기기 미실시'),
        ('④ 결함·UX','실측 사용성 참가자 0명; 결함 담당·기한 미지정'),
        ('⑤ 재시험','수정 버전·재시험·회귀 실행 미기록'),
        ('⑥ 보고·인수','P0 전항목 통과·S1/S2 0건·실제 PG 증거·정책 승인 조건 미충족')]]
    return dict(schema='hope-customer-final-report/v1',reference_documents=[REFERENCE_SHA256,FORMAT_SHA256],
        policy_version=POLICY_VERSION,engine_revision=engine_revision,sut_revision=sut_revision,
        summary={'basis_at':now,'total':len(results),'counts':counts,
            'executed':counts['PASS']+counts['FAIL']+sum(r['status']=='BLOCKED' and r['execution_steps'] is not None for r in results),
            'execution_basis_pass_rate_percent':round(100*counts['PASS']/denominator,2) if denominator else None,
            'unresolved_S1_S2':unresolved,'actual_pg':'미연동','recommendation':'재시험 필요',
            'usability':{'participants':0,'task_success_rate':None,'duration':None,'help_requests':None},
            'operational_authorization':False,'customer_acceptance_approved':False},
        approval_gates=gates,scenarios=results,defects=defects,policy_decisions_pending=POLICY_PENDING,
        developer_claims=collected.get('developer_claims',{}),
        workflow_results=evaluation['requirements'],
        ux_observations=[{'screen':'검수/개발현황/앱','priority':'P1','before':'약어 전체 명칭 누락 여부 자동 대조',
                         'proposed_after':'전체 명칭(약어)와 키보드/터치 도움말','actual_user_observation':False}],
        device_matrix=[{'device':d,'status':'NOT RUN','evidence':None} for d in
            ['Windows Chrome','Windows Edge','macOS Safari','Android Chrome','iPhone Safari']],
        viewport_matrix=[{'width':w,'status':'NOT RUN','evidence':None} for w in [360,390,768,1280]],
        retest_regression={'status':'NOT RUN','fixed_revision':None,'original_defect_ids':[d['id'] for d in defects]})
