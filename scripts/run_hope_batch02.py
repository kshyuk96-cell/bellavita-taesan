"""Build pinned actual Site source in Docker and export conservative customer reports."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.hope_service_runtime import ActualServiceProfile,execute_actual_service
from manager_bridge.hope_batch02 import contract,evaluate,customer_report,formula_report
from manager_bridge.hope_tour_exports import spreadsheet,document,text
ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    config=json.loads((ROOT/'deploy/hope-batch02/source-profile.json').read_text())
    profile=ActualServiceProfile(args.source,config['source_revision'],config['docker_image'])
    paths=[ROOT/'scripts/run_hope_batch02.py',ROOT/'manager_bridge/hope_batch02.py',ROOT/'manager_bridge/hope_service_runtime.py',ROOT/'deploy/hope-batch02/source-profile.json',ROOT/'taesan_core/universal_qa.py',ROOT/'taesan_core/hope_tour_qa.py',ROOT/'manager_bridge/qa_admission.py',ROOT/'manager_bridge/hope_tour_reporting.py',ROOT/'manager_bridge/hope_tour_exports.py',ROOT/'manager_bridge/project_runtime.py',ROOT/'manager_bridge/source_rehearsal.py',ROOT/'manager_bridge/store.py',*sorted((ROOT/'service_adapters/hope_tour').glob('*.mjs'))]
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    for path in paths:
        if subprocess.check_output(['git','show',revision+':'+str(path.relative_to(ROOT))],cwd=ROOT)!=path.read_bytes():raise ValueError('commit execution source before running')
    trusted=contract(profile.revision) # manager-owned, before target code executes
    raw=execute_actual_service(profile,ROOT/'service_adapters/hope_tour',timeout=config['timeout_seconds'])
    raw['engine_revision']=revision;raw['engine_files']=hashes
    if any(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h for p,h in hashes.items()):raise ValueError('execution inputs changed')
    if subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()!=revision:raise ValueError('engine revision changed')
    result=evaluate(raw,profile.revision,trusted)
    formulas=formula_report(raw)
    if any(r['status']!='PASS' for r in formulas['records']):raise ValueError('independent formula mismatch; evidence requires investigation')
    prior=json.loads((ROOT/'docs/qa/hope-tour/run-20261116/customer-report.json').read_text())
    report=customer_report(raw,result,engine_revision=revision,prior=prior)
    output=args.output;output.mkdir(parents=True,exist_ok=True)
    spreadsheet(report,output/'customer-report.xlsx');document(report,output/'customer-report.docx')
    from openpyxl import load_workbook
    book=load_workbook(output/'customer-report.xlsx');sheet=book.create_sheet('실제소스21-40')
    sheet.append(['번호','항목','범위','판정','기대값','실제값'])
    for row in result['common_report']['scenarios']:
        number=row['inputs']['item'];item=next(i for i in raw['items'] if i['id']==number)
        sheet.append([number,item['title'],raw['scope'],row['status'],text(row['expected']),text(row['actual'])])
    sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
    fsheet=book.create_sheet('독립계산식검증');fsheet.append(['계산식ID','식','변수','단위','반올림','정책승인','기대값','실제값','판정'])
    for f in formulas['records']:fsheet.append([f['id'],f['expression'],text(f['variables']),f['units'],f['rounding'],f['policy_approval'],f['expected'],f['actual'],f['status']])
    book.save(output/'customer-report.xlsx')
    from docx import Document
    doc=Document(output/'customer-report.docx');doc.add_heading('실제 소스 21~40번 비운영 검증',1)
    doc.add_paragraph('Site 버전33 소스 '+profile.revision+'를 네트워크 차단 컨테이너에서 실행. 실제 Worker/업무함수와 로컬 SQLite D1 호환 계층을 사용. 공개 사이트·실제 PG·브라우저 검증은 미실시. 취소수수료 0 등 TEST 정책은 운영 승인 규칙이 아님.')
    doc.add_paragraph(text(result['counts']))
    doc.add_heading('독립 계산식 검증',2)
    for f in formulas['records']:doc.add_paragraph(f["id"]+' | '+f["expression"]+' | expected='+str(f["expected"])+' actual='+str(f["actual"])+' '+f["status"])
    doc.save(output/'customer-report.docx')
    # Reopen both final-format artifacts; missing customer rows cannot disappear.
    check=load_workbook(output/'customer-report.xlsx',read_only=True)
    if check['시나리오결과'].max_row!=39 or len(Document(output/'customer-report.docx').paragraphs)==0:raise ValueError('customer report export incomplete')
    for name,value in [('actual-source-evidence',raw),('common-report',result['common_report']),('trusted-contract',trusted)]:
        with gzip.GzipFile(filename=str(output/(name+'.json.gz')),mode='wb',mtime=0) as stream:stream.write(json.dumps(value,ensure_ascii=False,sort_keys=True).encode())
    for name,value in [('formula-report',formulas),('customer-report',report),('batch-summary',dict(source_revision=profile.revision,engine_revision=revision,counts=result['counts'],admission=result['admission'],glossary=result['glossary'],report_exports=dict(canonical_customer_rows=38,word_reopened=True,excel_reopened=True),source_provenance=raw['source_provenance'],runtime_provenance=raw['runtime_provenance']))]:
        (output/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    (output/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in sorted(output.iterdir()) if p.is_file() and p.name!='SHA256SUMS'))
    print(json.dumps(dict(counts=result['counts'],admitted=result['admission'].get('admitted'),customer_counts=report['summary']['counts'],source=profile.revision),ensure_ascii=False))
    return 0 if result['admission'].get('admitted') else 1

if __name__=='__main__':raise SystemExit(main())
