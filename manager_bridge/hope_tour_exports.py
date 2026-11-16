"""Export the customer's requested preparation/execution/defect/retest format."""
import json
from .hope_tour_reporting import ROWS
from taesan_core.hope_tour_qa import REQUIREMENTS

DATASETS={
'U01':'희망 신규; 월50000/가입비25000; 카드 미등록',
'U02':'행복 신규; 월100000/가입비50000; 카드 미등록',
'U03':'프리미엄 신규; 월200000/가입비100000; 카드 미등록',
'U04':'DP600000/BP150000/SP100000', 'U05':'DP=BP=SP=0',
'U06':'DP=상품가','U07':'DP=상품가-1; BP 한도+1',
'U08':'청구 1/2회 실패; 3개월 휴면/6개월 정지',
'U09':'원금1200000/사용450000/BP85000; 반환 상한750000',
'U10':'일반회원/관리자/재무; 본인·타회사 경계',
'P01':'국내300000원; 재고10','P02':'해외/크루즈1000000원; 재고1',
'P03':'문화100000원; 재고0','P04':'크루즈 객실2종; 예약금/잔금/옵션',
'C01':'시뮬레이터 승인','C02':'시뮬레이터 거절','C03':'응답 타임아웃',
'C04':'중복 콜백','C05':'부분취소','R01':'취소수수료0','R02':'일부 취소수수료','R03':'전액 범위 취소수수료'}


def text(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True) if type(value) in (dict,list) else ('' if value is None else str(value))


SUMMARY_LABELS={'basis_at':'시험 기준일','deployed_revision':'배포 버전','total':'전체 시나리오 수','counts':'통과/실패/차단/미실시',
 'executed':'실행 수(부분 실행 포함)','execution_basis_pass_rate_percent':'실행기준 통과율(%)','unique_observed_checks':'고유 관찰 항목 수',
 'execution_note':'실행 집계 범위','business_results':'업무별 통과 현황','unresolved_S1_S2':'미해결 S1/S2',
 'actual_pg':'실제 PG 연동','recommendation':'최종 권고','usability':'사용성 실측',
 'operational_authorization':'운영 승인','customer_acceptance_approved':'발주사 최종 인수 승인'}


def spreadsheet(report,path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    book=Workbook();book.remove(book.active)
    def sheet(name,headers,rows):
        ws=book.create_sheet(name);ws.append(headers)
        for row in rows:
            values=[text(x) for x in row]
            # External labels/text must never become spreadsheet formulas.
            for index,value in enumerate(values):
                if value.startswith(('=','+','-','@')):values[index]="'"+value
            ws.append(values)
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for cell in ws[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='214D65')
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width=min(70,max(16,len(str(col[0].value))+5))
            for cell in col:cell.alignment=Alignment(vertical='top',wrap_text=True)
    sheet('종합결과',['항목','보고값'],[(SUMMARY_LABELS.get(k,k),v) for k,v in report['summary'].items()])
    sheet('승인게이트',['단계','상태','사유'],[(g['stage'],g['status'],g['reason']) for g in report['approval_gates']])
    sheet('시나리오결과',['시험ID','업무','중요도','시험내용','일시','수행자','검토자','배포버전','환경','데이터','실제절차','기대값','실제값','판정','증빙','결함','재시험','비고'],
        [(r['test_id'],r['group'],r['priority'],r['title'],r['tested_at'],r['performer'],r['reviewer'],r['deployed_revision'],r['environment'],r['preconditions'],r['execution_steps'],r['expected'],r['actual'],r['status'],r['evidence'],r['defect_ids'],r['retest'],r['note']) for r in report['scenarios']])
    claims={r['id']:r for r in report.get('developer_claims',{}).get('rows',[])}
    trace=[]
    for n in range(1,81):
        fb='FB-'+str(n).zfill(2);links=[key for key,value in REQUIREMENTS.items() if fb in value[2]]
        row=claims.get(fb,{})
        trace.append((fb,row.get('label'),row.get('status','미확인'),links,
                      '기존 Txx 매핑; 최종 시험ID 상세 추적 검토 필요' if links else '추적설계 대기',
                      '개발사 코드상 주장; 인수 통과 아님'))
    sheet('FB80추적표',['FB ID','항목','개발사표시','기존요구 매핑','추적상태','구분'],trace)
    sheet('준비데이터',['데이터ID','사전조건','대상 준비상태','초기화방법'],
          [(key,value,'NOT VERIFIED','새 격리 검수 공간; 지정 금액/재고 seed 적용 확인 필요') for key,value in DATASETS.items()])
    defect_headers=['id','scenario_ids','severity','priority','kind','symptom','reproduce','expected','actual','owner','status','due_date','fixed_revision','retest']
    sheet('오류관리',defect_headers,[[r.get(k) for k in defect_headers] for r in report['defects']])
    sheet('UX개선',['화면','중요도','개선전','개선안','실사용자관찰'],[(r['screen'],r['priority'],r['before'],r['proposed_after'],r['actual_user_observation']) for r in report['ux_observations']])
    sheet('재시험회귀',['시험ID','수정버전','재시험일','실제값','최종판정'],[(r['test_id'],r['retest']['fixed_revision'],r['retest']['tested_at'],r['retest']['actual'],r['retest']['final_verdict']) for r in report['scenarios']])
    sheet('기기환경',['대상','상태','증빙'],[(r['device'],r['status'],r['evidence']) for r in report['device_matrix']]+[(str(r['width'])+'px',r['status'],r['evidence']) for r in report['viewport_matrix']])
    sheet('정책결정',['정책','상태','승인자','승인일'],[(p,'정책결정 대기',None,None) for p in report['policy_decisions_pending']])
    book.save(path)


def document(report,path):
    from docx import Document
    doc=Document();doc.add_heading('희망투어클럽 테스트 준비·실행·결과보고',0)
    doc.add_paragraph('고객 최종 양식 §10~12 반영 · 자동 생성 · 미실시와 미확정 정책은 통과로 처리하지 않음')
    doc.add_heading('종합 결과보고서',level=1)
    table=doc.add_table(rows=0,cols=2);table.style='Table Grid'
    for key,value in report['summary'].items():
        row=table.add_row().cells;row[0].text=SUMMARY_LABELS.get(key,key);row[1].text=text(value)
    doc.add_paragraph('실행기준 통과율 = PASS ÷ (PASS+FAIL) ×100; 분모 0이면 미산정. BLOCKED/NOT RUN은 별도 표시.')
    doc.add_heading('준비 및 승인 게이트',level=1)
    for gate in report['approval_gates']:doc.add_paragraph(gate['stage']+' / '+gate['status']+' / '+gate['reason'])
    doc.add_heading('시나리오별 결과보고서',level=1)
    for scenario in report['scenarios']:
        doc.add_heading(scenario['test_id']+' '+scenario['title']+' — '+scenario['status'],level=2)
        t=doc.add_table(rows=0,cols=2);t.style='Table Grid'
        for label,key in [('기본정보','test_id'),('요구사항','requirement_id'),('시험일시','tested_at'),('수행자','performer'),('검토자','reviewer'),('배포 버전','deployed_revision'),('시험환경','environment'),('사전조건·데이터','preconditions'),('실제 수행절차','execution_steps'),('기대 결과','expected'),('실제 결과 UI/서버/PG','actual'),('판정','status'),('증빙','evidence'),('결함 및 개선','defect_ids'),('재시험','retest'),('제한','note')]:
            row=t.add_row().cells;row[0].text=label;row[1].text=text(scenario[key]) or '미확보 / 미실시'
    doc.add_heading('결함 및 재시험',level=1)
    for defect in report['defects']:doc.add_paragraph(text(defect))
    doc.add_heading('잔여 정책·실기기·사용성',level=1)
    for policy in report['policy_decisions_pending']:doc.add_paragraph(policy+' — 정책결정 대기')
    doc.add_paragraph('실제 이용자 참여 0명. 성공률·소요시간·도움요청 미측정. 브라우저/실기기·UI 영상·실제 PG 연동은 미실시.')
    doc.add_paragraph('참조 문서 SHA-256: '+', '.join(report['reference_documents']))
    doc.add_paragraph('엔진 '+report['engine_revision']+' / 대상 '+(text(report['sut_revision']) or '미확인')+' / 정책 '+report['policy_version'])
    doc.save(path)
