"""Local batch evidence is supplemental; it never promotes customer-service tests."""
import json
from copy import deepcopy
from pathlib import Path
from .hope_tour_acceptance import CASES
from .hope_tour_reporting import final_report
from .hope_tour_exports import spreadsheet,document,text
from taesan_core.hope_tour_qa import REQUIREMENTS


def customer_template(batch,prior=None):
    evaluation={'report':{'counts':{'executed':0},'scenarios':[
        dict(id=key,group=requirement,status='unmeasured',actual=None,executed=False,
             evidence={'reason':'local_reference_fixture_is_not_customer_site_evidence'})
        for key,(requirement,_) in CASES.items()]},
        'requirements':{k:dict(label=v[0],status='unmeasured') for k,v in REQUIREMENTS.items()}}
    result=final_report(evaluation,{},engine_revision=batch['source_revision'],sut_revision=None)
    result['local_batch']=dict(scope='local_reference_fixture',customer_site_verified=False,
        counts=batch.get('counts',{}),items=batch['items'])
    result['defects']=[]
    if prior:
        for defect in prior.get('defects',[]):
            if defect.get('status')!='CLOSED':
                carried=deepcopy(defect);carried.update(status='OPEN_NOT_RETESTED',
                    source_revision=prior.get('sut_revision'),provenance='historical_customer_observation_not_retested_by_batch01')
                result['defects'].append(carried)
    result['summary']['unresolved_S1_S2']=sum(d['severity'] in ('S1','S2') for d in result['defects'])
    result['summary']['execution_scope']='본 묶음은 로컬 참조 어댑터 검증; 고객 사이트 시험과 별도'
    result['summary']['actual_pg']='미연동'
    result['summary']['actual_pg_evidence']='본 묶음에서는 실제 PG를 연결하거나 검증하지 않음'
    result['retest_regression']={'status':'NOT RUN','fixed_revision':None,'original_defect_ids':[d['id'] for d in result['defects']]}
    return result


def export(batch,output,*,prior=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    report=customer_template(batch,prior)
    (output/'customer-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    spreadsheet(report,output/'customer-report.xlsx');document(report,output/'customer-report.docx')
    from openpyxl import load_workbook
    book=load_workbook(output/'customer-report.xlsx');sheet=book.create_sheet('개발묶음20')
    sheet.append(['번호','개발 항목','검증 범위','로컬 판정','기대값','실제값','고객 사이트 확인'])
    for item in batch['items']:
        sheet.append([item['item'],item['title'],item['scope'],item['status'],text(item['expected']),text(item['actual']),'미연결'])
    sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
    evidence=book.create_sheet('로컬거래증거');evidence.append(['workspace','generation','sequence','kind','receipt'])
    for run in batch.get('evidence',[]):
        for event in run['events']:
            evidence.append([run['workspace'],event['generation'],event['sequence'],event['kind'],text(event['receipt'])])
    book.save(output/'customer-report.xlsx')
    from docx import Document
    doc=Document(output/'customer-report.docx')
    doc.add_heading('승인된 첫 20개 개발 묶음 — 로컬 참조 검증',level=1)
    doc.add_paragraph('고객 사이트의 실행 결과가 아닙니다. 지정 데이터·거래 어댑터의 개발 및 로컬 HTTP 검증 결과입니다. 기존 고객 결함은 미재시험 상태로 보존합니다.')
    table=doc.add_table(rows=1,cols=4);table.style='Table Grid'
    for cell,value in zip(table.rows[0].cells,['번호','개발 항목','범위','판정']):cell.text=value
    for item in batch['items']:
        for cell,value in zip(table.add_row().cells,[str(item['item']),item['title'],item['scope'],item['status']]):cell.text=value
    doc.save(output/'customer-report.docx')
    check=load_workbook(output/'customer-report.xlsx',read_only=True)
    return dict(customer_promoted=any(r['status']=='PASS' for r in report['scenarios']),
                canonical_cases=check['시나리오결과'].max_row-1,
                word_created=(output/'customer-report.docx').is_file(),excel_created=(output/'customer-report.xlsx').is_file())
