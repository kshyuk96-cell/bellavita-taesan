"""Approved81–100: one claimed, source-pinned persistent recovery QA job; never deploys."""
import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.hope_service_runtime import ActualServiceProfile
from manager_bridge.hope_registered_runtime import HopeProfile,HopeRegistry,HopeArtifacts
from manager_bridge.hope_shared_inventory import provision_exchange
from manager_bridge.hope_persistent_review import provision_business
from manager_bridge.hope_recovery_execution import PersistentRecoveryEngine
from manager_bridge.hope_persistent_manager import PersistentManager
from manager_bridge.hope_batch05 import contract,evaluate,customer_report,VERSION
from manager_bridge.qa_jobs import QAJobs
from manager_bridge.qa_worker import QAClient,QAWorker
from manager_bridge.store import Store
from manager_bridge.control import request
from manager_bridge.hope_tour_exports import spreadsheet,document,text
ROOT=Path(__file__).resolve().parents[1]


def export(root,raw,result,report,patch,manager,baseline):
    from openpyxl import load_workbook
    from docx import Document
    root.mkdir(parents=True,exist_ok=True);spreadsheet(report,root/'customer-report.xlsx');document(report,root/'customer-report.docx')
    book=load_workbook(root/'customer-report.xlsx');sheet=book.create_sheet('개발81-100');sheet.append(['항목','우선순위','판정','기대값','실제값','증거'])
    for row in result['common_report']['scenarios']:sheet.append([row['inputs']['item'],row['priority'],row['status'],text(row['expected']),text(row['actual']),text(row['evidence'])])
    fs=book.create_sheet('독립부분취소계산식');fs.append(['ID','식','변수','단위','반올림','정책승인','기대값','실제값','판정'])
    for f in result['rounding']['records']:fs.append([f['id'],f['expression'],text(f['variables']),f['units'],f['rounding'],f['policy_approval'],f['expected'],f['actual'],f['status']])
    cs=book.create_sheet('필수하위증거');cs.append(['고객ID','중요도','판정','필수 누락','관찰'])
    for row in result['coverage']['rows']:cs.append([row['id'],row['priority'],row['status'],text(row['missing']),text(row['observations'])])
    book.save(root/'customer-report.xlsx')
    doc=Document(root/'customer-report.docx');doc.add_heading('81–100 영속 TEST 업무·재고 복구',1);doc.add_paragraph('실제 후보 '+raw['source_revision']+'; '+text(result['counts'])+'. 실제 공급사·PG·브라우저·운영 배포 미적용.')
    doc.add_paragraph('업무 저장 전후5개 종료 지점은 실제 프로세스 종료와 저장소 체크포인트로 확인. 종료가 입증된 실행자의 원거래만 조회하며 기존 요청을 재실행하지 않습니다. 같은 업무 리비전의 두 Worker CAS 쓰기는1건만 성공했습니다.')
    for f in result['rounding']['records']:doc.add_paragraph(f['id']+' '+f['expression']+' '+text(f['variables'])+' expected='+str(f['expected'])+' actual='+str(f['actual'])+' '+f['status'])
    doc.save(root/'customer-report.docx')
    if load_workbook(root/'customer-report.xlsx',read_only=True)['시나리오결과'].max_row!=39 or not Document(root/'customer-report.docx').paragraphs:raise ValueError('customer38-row format incomplete')
    for name,value in [('actual-source-evidence',raw),('common-report',result['common_report']),('trusted-contract',result['trusted_contract'])]:
        with gzip.GzipFile(filename=str(root/(name+'.json.gz')),mode='wb',mtime=0) as stream:stream.write(json.dumps(value,ensure_ascii=False,sort_keys=True).encode())
    prior=next(f for f in baseline['artifacts']['faultInjection'] if f['id']=='duplicate-debit')
    for name,value in [('customer-report',report),('formula-report',dict(source_revision=raw['source_revision'],**result['rounding'],issues=result['issues'],policy_approval='TEST assumptions; operational approval pending')),('customer-evidence-contract',result['customer_contract']),('batch-summary',dict(source_revision=raw['source_revision'],engine_revision=raw['engine_revision'],counts=result['counts'],admission=result['admission'],manager=manager,browser_plan=result['browser_plan'],source_provenance=raw['source_provenance'],runtime_provenance=raw['runtime_provenance'])),('defect-retest',dict(original_source_revision=baseline['source_revision'],original_duplicate_detected=prior['targetDetected'],candidate_source_revision=raw['source_revision'],candidate_fault_results=result['fault_injection'],defects=report['defects'],published=False))]:
        (root/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    (root/'source-candidate.patch').write_bytes(patch)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--state-dir',type=Path,required=True);args=parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):raise ValueError('fresh output required')
    if args.state_dir.exists() and any(args.state_dir.iterdir()):raise ValueError('never replay an existing run; fresh dedicated state required')
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if subprocess.run(['git','diff','--quiet','HEAD'],cwd=ROOT).returncode:raise ValueError('commit tracked inputs first')
    paths=sorted({*ROOT.glob('manager_bridge/*.py'),*ROOT.glob('taesan_core/*.py'),*ROOT.glob('service_adapters/hope_tour/*.mjs'),*ROOT.glob('deploy/hope-batch05/*.json'),ROOT/'scripts/run_hope_batch05.py'})
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    for p in paths:
        if subprocess.check_output(['git','show',revision+':'+str(p.relative_to(ROOT))],cwd=ROOT)!=p.read_bytes():raise ValueError('uncommitted execution input')
    config=json.loads((ROOT/'deploy/hope-batch05/source-profile.json').read_text());inv=json.loads((ROOT/'deploy/hope-batch05/inventory-contract.json').read_text());biz=json.loads((ROOT/'deploy/hope-batch05/business-contract.json').read_text())
    source=ActualServiceProfile(args.source,config['source_revision'],config['docker_image']);source.source_check()
    args.state_dir.mkdir(parents=True,mode=0o700,exist_ok=True);exchange=args.state_dir/'exchange';exchange.mkdir();business=args.state_dir/'business';business.mkdir();provision_exchange(exchange,inv,source.revision);provision_business(business,biz,source.revision,exchange)
    # One QA-job lifetime key: never written to config, arguments, logs or reports.
    runtime_secrets={'REVIEW_DATA_KEY':base64.b64encode(os.urandom(32)).decode()}
    store=Store(args.state_dir/'manager.sqlite');owner=store.add_token('local-owner','owner',['*']);store.register_project(owner,'hope-tour','local/hope-tour-source');worker=store.add_token('local-worker','worker',['hope-tour'])
    profile=HopeProfile('hope-tour',VERSION,source,ROOT/'service_adapters/hope_tour',entrypoint='run-batch05.mjs',exchange=exchange,inventory_contract=inv,business=business,business_contract=biz)
    registry=HopeRegistry(store,[profile]);registry.provision(owner,profile);trusted=contract(source.revision)
    with PersistentManager(ROOT,store.path) as manager:
        jobs=QAJobs(store);approval=jobs.approve(owner,project='hope-tour',source_revision=source.revision,contract=trusted,coverage=['batch05-'+str(n) for n in range(81,101)],adapter_release=VERSION,max_seconds=90,execution_profile_digest=profile.fingerprint)
        job=jobs.receive(owner,approval,'approved-batch05',biz['task']);manifest=jobs.inspect(owner,job['id'])['manifest'];last={}
        def evaluator(raw):
            result=evaluate(raw,source.revision,trusted,inv,biz);last.update(result);return result['common_report']
        engine=PersistentRecoveryEngine(registry,manifest,evaluator,runtime_secrets);outcome=QAWorker(QAClient(manager.base,worker),registry,engine=engine).run_once(job['id'])
        if not engine.raw or not last or outcome['state'] not in ('accepted','blocked'):raise ValueError('result uncertain; preserve state and reconcile; no automatic replay')
        raw=engine.raw;raw.update(engine_revision=revision,engine_files=hashes,baseline_revision=config['baseline_revision'])
        serialized=json.dumps(raw,ensure_ascii=False)
        if 'HopeTest!2026' in serialized or runtime_secrets['REVIEW_DATA_KEY'] in serialized:raise ValueError('private runtime data must not be exported')
        final=jobs.get(owner,job['id']);last.update(common_report=final['report'],admission=final['decision'])
        prior=json.loads((ROOT/'docs/qa/hope-tour/batch04/run-20261116/customer-report.json').read_text());baseline=json.loads(gzip.decompress((ROOT/'docs/qa/hope-tour/batch04/run-20261116/actual-source-evidence.json.gz').read_bytes()))
        if baseline['source_revision']!=config['previous_candidate_revision']:raise ValueError('historical defect source mismatch')
        report=customer_report(raw,last,revision,prior);patch=subprocess.check_output(['git','diff','--binary',config['baseline_revision'],source.revision],cwd=source.source)
        export(args.output,raw,last,report,patch,dict(job_id=job['id'],state=final['state'],decision=final['decision'],remote_server_connected=False,transport='separate persistent local manager HTTP; source Workers Docker',profile_digest=profile.fingerprint),baseline)
        artifacts=HopeArtifacts(store);items=artifacts.collect(owner,job['id'],args.output);sealed=artifacts.seal(owner,job['id'],[r['name'] for r in items]);manager.restart();fetched=[]
        for item in items:
            response=request(manager.base,owner,'GET','/v1/qa/jobs/'+job['id']+'/artifacts/'+item['name'],max_response_bytes=12*1024*1024)
            if not response.get('ok') or hashlib.sha256(base64.b64decode(response['artifact']['data'])).hexdigest()!=item['sha']:raise ValueError('sealed recovery artifact mismatch')
            fetched.append(item['name'])
        (args.output/'manager-recovery.json').write_text(json.dumps(dict(job_id=job['id'],sealed=sealed,after_process_restart_verified=fetched,raw_tokens_persisted=False,raw_encryption_key_persisted=False,remote_server_connected=False),ensure_ascii=False,indent=2)+'\n')
        (args.output/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in sorted(args.output.iterdir()) if p.is_file() and p.name!='SHA256SUMS'))
        if any(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h for p,h in hashes.items()):raise ValueError('execution code changed during run')
        print(json.dumps(dict(counts=last['counts'],manager=final['state'],customer_counts=last['coverage']['counts'],recovered_artifacts=len(fetched),source=source.revision),ensure_ascii=False))
        return 0 if final['decision']['admitted'] else 1

if __name__=='__main__':raise SystemExit(main())
