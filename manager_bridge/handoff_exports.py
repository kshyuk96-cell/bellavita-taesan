"""Customer-facing format: preparation, actual result, evidence, defects and retest."""
import json
from pathlib import Path


def txt(v):return json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(dict,list)) else '' if v is None else str(v)


def export_package(intake,report,output):
    from docx import Document
    from openpyxl import Workbook,load_workbook
    from openpyxl.styles import Font,PatternFill,Alignment
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    def save(name,value):(output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    save('intake.json',intake);save('customer-report.json',report)
    book=Workbook();book.remove(book.active)
    def sheet(name,headers,rows):
        ws=book.create_sheet(name);ws.append(headers)
        for row in rows:
            cells=[txt(v) for v in row];ws.append(["'"+v if v.startswith(('=','+','-','@')) else v for v in cells])
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for cell in ws[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='214D65')
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width=30
            for cell in col:cell.alignment=Alignment(wrap_text=True,vertical='top')
    labels={'total':'전체 시나리오','counts':'통과·실패·차단·미실시','executed':'실행 수','customer_ready':'고객 검토용 인계 가능','recommendation':'최종 권고','source_revision':'검증 후보 버전','actual_pg':'실제 PG','usability':'사용성 실측','operational_authorization':'운영 승인','customer_acceptance_approved':'발주사 최종 인수 승인'}
    sheet('종합결과',['항목','값'],[(labels.get(k,k),v) for k,v in report['summary'].items()])
    sheet('검증준비',['항목','상태'],[('URL',report['target']),('버전',report['summary']['source_revision']),('계정',report['accounts']),('브라우저',report['browser_readiness']),('읽기 연결 확인',report['read_only_probe'])])
    headers=['시험ID','요구사항ID','업무','중요도','시험내용','시험일시','수행자','검토자','버전','환경','사전조건','실제절차','기대값','실제값','판정','증빙','결함','재시험','비고']
    keys=['test_id','requirement_id','group','priority','title','tested_at','performer','reviewer','deployed_revision','environment','preconditions','execution_steps','expected','actual','status','evidence','defect_ids','retest','note']
    sheet('시나리오결과',headers,[[r[k] for k in keys] for r in report['scenarios']])
    sheet('개발사목록대조',['개발사ID','항목','개발사 주장','개발사 기대값','독립 검증'],[(r['id'],r['title'],r['developer_claimed_status'],r['developer_expected'],r['verified']) for r in report['developer_claims']])
    sheet('QA추가검사',['ID','업무','유형','기대조건','상태'],[(r['id'],r['group'],r['title'],r['expected'],r['execution_status']) for r in report['qa_added_checks']])
    sheet('위험산정',['시험ID','중요도','영향','가능성','복구','이유'],[(r['test_id'],r['priority'],r['risk']['impact'],r['risk']['likelihood'],r['risk']['recovery'],r['risk']['reason']) for r in report['scenarios']])
    sheet('계산식',['계약·변수·단위·순서·반올림·승인','기대값','실제값','판정'],[(r.get('contract'),r.get('expected'),r.get('actual'),r.get('status')) for r in report['formula_report']])
    sheet('오류관리',['BUG','심각도','현상','재현','상태','담당','수정버전','재시험'],[(r.get('id'),r.get('severity'),r.get('symptom'),r.get('reproduce'),r.get('status'),r.get('owner'),r.get('fixed_revision'),r.get('retest')) for r in report['defects']])
    sheet('재시험회귀',['시험ID','수정버전','일시','실제값','판정'],[(r['test_id'],r['retest']['fixed_revision'],r['retest']['tested_at'],r['retest']['actual'],r['retest']['final_verdict']) for r in report['scenarios']])
    sheet('인계조건',['단계','상태','이유'],[(r['stage'],r['status'],r['reason']) for r in report['approval_gates']])
    sheet('정책결정',['항목','상태'],[(p,'정책결정 대기') for p in report['policy_decisions_pending']])
    sheet('부족증거',['내용'],[(s,) for s in report['missing_evidence']]);book.save(output/'customer-report.xlsx')
    doc=Document();doc.add_heading(report['project']+' 인계 전 테스트 확인서',0)
    doc.add_paragraph('문서 상태: '+('검증 준비·미실시 — 완료 확인서로 사용 불가' if report['document_status']=='DRAFT_NOT_VERIFIED' else '독립 검증 완료·고객 검토용' if report['summary']['customer_ready'] else '인계 보류·보완 필요'))
    doc.add_paragraph('개발사의 완료 주장과 독립 실행 결과는 구분합니다. 미실시·차단을 통과로 표시하지 않으며 발주사의 최종 인수 승인은 별도입니다.')
    doc.add_heading('종합 결과보고서',1)
    table=doc.add_table(rows=0,cols=2);table.style='Table Grid'
    for k,v in report['summary'].items():
        cells=table.add_row().cells;cells[0].text=labels.get(k,k);cells[1].text=txt(v)
    doc.add_heading('준비·환경과 인계 조건',1)
    doc.add_paragraph('검증 URL: '+report['target'])
    doc.add_paragraph('테스트 계정: '+str(len(report['accounts']))+'개 역할별 접수. 로그인 확인은 별도 실행 결과로 기록하며 계정 원문·비밀번호는 싣지 않습니다.')
    for gate in report['approval_gates']:doc.add_paragraph(gate['stage']+' / '+gate['status']+' / '+gate['reason'])
    doc.add_heading('시나리오별 결과',1)
    for row in report['scenarios']:
        doc.add_heading(row['test_id']+' '+row['title'],2)
        table=doc.add_table(rows=0,cols=2);table.style='Table Grid'
        for label,key in [('시험일시','tested_at'),('중요도','priority'),('환경','environment'),('사전조건','preconditions'),('실제 수행 단계','execution_steps'),('기대값','expected'),('실제값','actual'),('판정','status'),('증거','evidence'),('결함','defect_ids'),('재시험','retest'),('비고','note')]:
            cells=table.add_row().cells;cells[0].text=label;value=row[key]
            if key=='expected':value=value.get('description',value) if isinstance(value,dict) else value
            elif key=='actual' and isinstance(value,dict):value='화면: '+txt(value.get('ui') or '미확인')+' / 서버·원장: '+txt(value.get('server') or '미확인')+' / PG: '+txt(value.get('pg') or '미확인')
            elif value is None:value='미실시' if key in ('tested_at','execution_steps') else '미확인'
            cells[1].text=txt(value)
    for title,body in [('독립 계산식',report['formula_report']),('결함·UX 개선',dict(defects=report['defects'],ux=report['ux_observations'])),('추가 QA 검사',report['qa_added_checks']),('정책결정 대기',report['policy_decisions_pending']),('부족한 증거',report['missing_evidence'])]:doc.add_heading(title,1);doc.add_paragraph(txt(body))
    doc.save(output/'customer-report.docx')
    request='개발 Codex 보완 요청서\n\n대상: '+report['target']+'\n목적: 고객 인계 전 최종 독립 검증. 개발자 PASS 표시는 실행 증거로 채택하지 않습니다.\n\n'
    request+='\n'.join(str(i+1)+'. '+s for i,s in enumerate(report['missing_evidence']))
    request+='\n\n가능한 자료: 비운영 여부·배포 후보 식별, 합성 테스트 데이터/초기화 방법, 원장·요청·예약·PG 테스트 결과의 비밀값 없는 읽기 증거. 운영 결제/발송/자료변경은 요청하지 않습니다. 비밀번호·토큰·실제 개인정보를 결과표에 넣지 마세요.\n'
    (output/'developer-followup.txt').write_text(request)
    check=load_workbook(output/'customer-report.xlsx',read_only=True)
    if check['시나리오결과'].max_row-1!=len(report['scenarios']) or not Document(output/'customer-report.docx').paragraphs:raise ValueError('customer format incomplete')
    return dict(rows=len(report['scenarios']),word_reopened=True,excel_reopened=True,customer_ready=report['summary']['customer_ready'])
