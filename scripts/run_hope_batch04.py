"""Approved61–80: shared TEST inventory, two container race and persistent local manager."""
import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.hope_service_runtime import ActualServiceProfile
from manager_bridge.hope_registered_runtime import HopeProfile,HopeRegistry,HopeEngine,HopeArtifacts
from manager_bridge.hope_shared_inventory import provision_exchange
from manager_bridge.hope_batch04 import contract,evaluate,customer_report
from manager_bridge.hope_batch04_probes import independent_race,persistent_checks
from manager_bridge.hope_persistent_manager import PersistentManager
from manager_bridge.qa_jobs import QAJobs
from manager_bridge.qa_worker import QAClient,QAWorker
from manager_bridge.store import Store
from manager_bridge.control import request
from manager_bridge.hope_tour_exports import spreadsheet,document,text
ROOT=Path(__file__).resolve().parents[1]


def export(root,raw,result,report,patch,manager):
    from openpyxl import load_workbook
    from docx import Document
    root.mkdir(parents=True,exist_ok=True);spreadsheet(report,root/'customer-report.xlsx');document(report,root/'customer-report.docx')
    book=load_workbook(root/'customer-report.xlsx');sheet=book.create_sheet('개발61-80');sheet.append(['항목','우선순위','판정','기대값','실제값','증거'])
    for r in result['common_report']['scenarios']:sheet.append([r['inputs']['item'],r['priority'],r['status'],text(r['expected']),text(r['actual']),text(r['evidence'])])
    formula=[]
    for slot in raw['artifacts']['inventory']['slots']:
        used=sum(r['quantity']-r['released'] for r in raw['artifacts']['inventory']['leases'] if r['slot']==slot['slot'] and r['status']!='released')
        formula.append(dict(id=slot['slot'],expression='available+sum(quantity-released)=capacity',variables=dict(available=slot['available'],allocated=used),units='TEST room',rounding='exact integer',expected=slot['capacity'],actual=slot['available']+used,status='PASS' if slot['available']+used==slot['capacity'] else 'FAIL',approval='approved QA fixture only; supplier not connected'))
    fs=book.create_sheet('독립재고대사');fs.append(['자원','계산식','변수','기대값','실제값','판정'])
    for f in formula:fs.append([f['id'],f['expression'],text(f['variables']),f['expected'],f['actual'],f['status']])
    faultsheet=book.create_sheet('결함주입');faultsheet.append(['오류','독립검출','검출내역','대상자체검출'])
    for f in result['fault_injection']:faultsheet.append([f['id'],f['detected'],text(f['issues']),f['target_detected']])
    book.save(root/'customer-report.xlsx')
    doc=Document(root/'customer-report.docx');doc.add_heading('61–80 공유 테스트 재고·영속 관리기 검증',1)
    doc.add_paragraph('후보 '+raw['source_revision']+'; '+text(result['counts'])+'; 공급사·실제 PG·브라우저·운영 배포 미적용.')
    doc.add_paragraph('독립 Docker 실행자 둘이 같은 테스트 SQLite의 마지막1석을 경쟁. 실제 관리기 프로세스를 중지·재시작하고 running job 재실행 거절·산출물 회수를 확인. 원본 서비스에 공유 공급사가 연결되었다는 뜻이 아닙니다.')
    for f in formula:doc.add_paragraph(f['id']+' expected='+str(f['expected'])+' actual='+str(f['actual'])+' '+f['status'])
    for f in result['fault_injection']:doc.add_paragraph(f['id']+' independent='+str(f['detected'])+' target='+str(f['target_detected'])+' '+text(f['issues']))
    doc.save(root/'customer-report.docx')
    if load_workbook(root/'customer-report.xlsx',read_only=True)['시나리오결과'].max_row!=39 or not Document(root/'customer-report.docx').paragraphs:raise ValueError('customer report format incomplete')
    for name,value in [('actual-source-evidence',raw),('common-report',result['common_report']),('trusted-contract',result['trusted_contract'])]:
        with gzip.GzipFile(filename=str(root/(name+'.json.gz')),mode='wb',mtime=0) as stream:stream.write(json.dumps(value,ensure_ascii=False,sort_keys=True).encode())
    for name,value in [('customer-report',report),('formula-report',dict(source_revision=raw['source_revision'],stock=formula,fault_injection=result['fault_injection'],domain_issues=result['domain_issues'],supplier_connected=False)),('defect-retest',dict(original=raw['baseline_revision'],candidate=raw['source_revision'],browser_verified=False,supplier_connected=False,defects=report['defects'])),('batch-summary',dict(source_revision=raw['source_revision'],engine_revision=raw['engine_revision'],counts=result['counts'],admission=result['admission'],manager=manager,browser_plan=result['browser_plan'],runtime_provenance=raw['runtime_provenance'],source_provenance=raw['source_provenance']))]:
        (root/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    (root/'source-candidate.patch').write_bytes(patch)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--state-dir',type=Path,required=True);args=parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):raise ValueError('fresh output required')
    if args.state_dir.exists() and any(args.state_dir.iterdir()):raise ValueError('fresh dedicated nonproduction state directory required; never replay an existing run')
    args.state_dir.mkdir(parents=True,mode=0o700,exist_ok=True)
    config=json.loads((ROOT/'deploy/hope-batch04/source-profile.json').read_text());inventory_contract=json.loads((ROOT/'deploy/hope-batch04/inventory-contract.json').read_text())
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if subprocess.run(['git','diff','--quiet','HEAD'],cwd=ROOT).returncode:raise ValueError('commit tracked inputs first')
    paths=sorted({*ROOT.glob('manager_bridge/*.py'),*ROOT.glob('taesan_core/*.py'),*ROOT.glob('service_adapters/hope_tour/*.mjs'),*ROOT.glob('deploy/hope-batch04/*.json'),ROOT/'scripts/run_hope_batch04.py'})
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    for p in paths:
        if subprocess.check_output(['git','show',revision+':'+str(p.relative_to(ROOT))],cwd=ROOT)!=p.read_bytes():raise ValueError('uncommitted execution input')
    source=ActualServiceProfile(args.source,config['source_revision'],config['docker_image']);source.source_check()
    exchange=args.state_dir/'exchange';exchange.mkdir();pin=provision_exchange(exchange,inventory_contract,source.revision)
    store=Store(args.state_dir/'manager.sqlite');owner=store.add_token('local-owner','owner',['*']);store.register_project(owner,'hope-tour','local/hope-tour-source');worker=store.add_token('local-worker','worker',['hope-tour']);store.register_project(owner,'outside','local/outside');outsider=store.add_token('outside','worker',['outside'])
    profile=HopeProfile('hope-tour','shared-fixture-persistent-manager-v1',source,ROOT/'service_adapters/hope_tour',entrypoint='run-batch04.mjs',exchange=exchange,inventory_contract=inventory_contract)
    registry=HopeRegistry(store,[profile]);registry.provision(owner,profile);trusted=contract(source.revision)
    host={};race,race_evidence=independent_race(profile);host[66]=race
    with PersistentManager(ROOT,store.path) as manager:
        probe,probe_evidence=persistent_checks(store,manager,owner,worker,outsider,registry,profile);host.update(probe)
        jobs=QAJobs(store);approval=jobs.approve(owner,project=profile.project,source_revision=source.revision,contract=trusted,coverage=['batch04-'+str(n) for n in range(61,81)],adapter_release=profile.release,max_seconds=90,execution_profile_digest=profile.fingerprint)
        job=jobs.receive(owner,approval,'approved-batch04','hope-batch04-source-candidate');manifest=jobs.inspect(owner,job['id'])['manifest'];last={}
        def evaluator(raw):
            result=evaluate(raw,source.revision,trusted,host,pin,inventory_contract);last.update(result);raw['browser_plan']=result['browser_plan'];return result['common_report']
        engine=HopeEngine(registry,manifest,evaluator);outcome=QAWorker(QAClient(manager.base,worker),registry,engine=engine).run_once(job['id'])
        if not engine.raw or not last or outcome['state'] not in ('accepted','blocked'):raise ValueError('execution uncertain; preserve state and reconcile; never auto retry')
        raw=engine.raw;raw.update(engine_revision=revision,engine_files=hashes,baseline_revision=config['baseline_revision'],manager_probe=probe_evidence,independent_race=race_evidence)
        if 'HopeTest!2026' in json.dumps(raw,ensure_ascii=False):raise ValueError('fixture password must be redacted')
        final=jobs.get(owner,job['id']);last.update(common_report=final['report'],admission=final['decision'])
        prior=json.loads((ROOT/'docs/qa/hope-tour/batch03/run-20261116/customer-report.json').read_text());report=customer_report(raw,last,engine_revision=revision,prior=prior)
        patch=subprocess.check_output(['git','diff','--binary',config['baseline_revision'],source.revision],cwd=source.source)
        export(args.output,raw,last,report,patch,dict(job_id=job['id'],state=outcome['state'],decision=outcome['decision'],transport='separate persistent local manager process HTTP',remote_server_connected=False,profile_digest=profile.fingerprint))
        artifacts=HopeArtifacts(store);inventory=artifacts.collect(owner,job['id'],args.output);sealed=artifacts.seal(owner,job['id'],[r['name'] for r in inventory]);manager.restart()
        fetched=[]
        for entry in inventory:
            response=request(manager.base,owner,'GET','/v1/qa/jobs/'+job['id']+'/artifacts/'+entry['name'],max_response_bytes=12*1024*1024)
            if not response.get('ok') or hashlib.sha256(base64.b64decode(response['artifact']['data'])).hexdigest()!=entry['sha']:raise ValueError('persistent evidence recovery failed')
            fetched.append(entry['name'])
        (args.output/'manager-recovery.json').write_text(json.dumps(dict(job_id=job['id'],state=jobs.get(owner,job['id'])['state'],sealed=sealed,after_process_restart_verified=fetched,raw_tokens_persisted=False,remote_server_connected=False),ensure_ascii=False,indent=2)+'\n')
        (args.output/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in sorted(args.output.iterdir()) if p.is_file() and p.name!='SHA256SUMS'))
        if any(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=v for p,v in hashes.items()):raise ValueError('execution code changed during run')
        print(json.dumps(dict(counts=last['counts'],manager=outcome['state'],customer_counts=report['summary']['counts'],recovered_after_restart=len(fetched),source=source.revision),ensure_ascii=False))
        return 0 if final['decision']['admitted'] else 1

if __name__=='__main__':raise SystemExit(main())
