"""Approved batch 41–60: local candidate + real manager HTTP + fenced Docker worker."""
import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.hope_service_runtime import ActualServiceProfile,execute_actual_service,_docker_env
from manager_bridge.hope_registered_runtime import HopeProfile,HopeRegistry,HopeEngine,HopeArtifacts
from manager_bridge.hope_manager_probe import integration_checks
from manager_bridge.hope_batch03 import contract,evaluate,customer_report
from manager_bridge.qa_jobs import QAJobs
from manager_bridge.qa_worker import QAClient,QAWorker
from manager_bridge.qa_runtime import QAInterrupted
from manager_bridge.store import Store
from manager_bridge.server import make_server
from manager_bridge.control import request
from manager_bridge.hope_tour_exports import spreadsheet,document,text
ROOT=Path(__file__).resolve().parents[1]


def export(output,raw,result,report,patch,manager_state):
    from openpyxl import load_workbook
    from docx import Document
    output.mkdir(parents=True,exist_ok=True)
    spreadsheet(report,output/'customer-report.xlsx');document(report,output/'customer-report.docx')
    book=load_workbook(output/'customer-report.xlsx');sheet=book.create_sheet('개발41-60')
    sheet.append(['항목','그룹','우선순위','판정','입력','기대값','실제값','증거'])
    for row in result['common_report']['scenarios']:sheet.append([row['inputs']['item'],row['group'],row['priority'],row['status'],text(row['inputs']),text(row['expected']),text(row['actual']),text(row['evidence'])])
    formulas=[];caps={'hope':{'domestic':1500,'overseas':1000,'culture':5000},'happy':{'domestic':2000,'overseas':1500,'culture':7000},'premium':{'domestic':2500,'overseas':1800,'culture':10000}}
    for r in raw['artifacts']['bpMatrix']:
        cap=caps[r['grade']][r['category']];expected=min(r['total']-r['DP'],r['total']*cap//10000)
        formulas.append(dict(id=r['grade']+'-'+r['category'],expression='min(total-DP,floor(total*capBps/10000))',variables=dict(total=r['total'],DP=r['DP'],capBps=cap),units='KRW / point TEST 1:1',rounding='integer floor',expected=expected,actual=r['BP'],status='PASS' if expected==r['BP'] else 'FAIL',policy_approval='customer example; operational approval pending'))
    fsheet=book.create_sheet('독립BP계산식');fsheet.append(['ID','式','변수','기대값','실제값','판정','정책승인'])
    for f in formulas:fsheet.append([f['id'],f['expression'],text(f['variables']),f['expected'],f['actual'],f['status'],f['policy_approval']])
    book.save(output/'customer-report.xlsx')
    doc=Document(output/'customer-report.docx');doc.add_heading('41–60 실제 소스·중앙 관리기 비운영 검증',1)
    doc.add_paragraph('수정 후보 소스 '+raw['source_revision']+'; 게시하지 않음. '+text(result['counts']))
    doc.add_paragraph('용어 HTML 수정 확인은 실제 브라우저 조작 검증과 다릅니다. 검수 공간별 재고는 공유 재고의 동시성 검증을 대체하지 않습니다. 실제 PG·운영 서버는 미연결입니다.')
    for f in formulas:doc.add_paragraph(f['id']+': '+f['expression']+' expected='+str(f['expected'])+' actual='+str(f['actual'])+' '+f['status'])
    doc.save(output/'customer-report.docx')
    if load_workbook(output/'customer-report.xlsx',read_only=True)['시나리오결과'].max_row!=39 or not Document(output/'customer-report.docx').paragraphs:raise ValueError('38-row final format lost')
    for name,value in [('actual-source-evidence',raw),('common-report',result['common_report']),('trusted-contract',result['trusted_contract'])]:
        with gzip.GzipFile(filename=str(output/(name+'.json.gz')),mode='wb',mtime=0) as stream:stream.write(json.dumps(value,ensure_ascii=False,sort_keys=True).encode())
    for name,value in [('customer-report',report),('formula-report',dict(records=formulas,source_revision=raw['source_revision'],operational_authorization=False)),('batch-summary',dict(counts=result['counts'],admission=result['admission'],manager=manager_state,source_revision=raw['source_revision'],source_provenance=raw['source_provenance'],runtime_provenance=raw['runtime_provenance'])),('defect-retest',dict(original=raw['baseline_revision'],candidate=raw['source_revision'],baseline_glossary=result['baseline_glossary'],candidate_glossary=result['glossary'],browser_verified=False,defects=report['defects']))]:
        (output/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    (output/'source-candidate.patch').write_bytes(patch)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):raise ValueError('fresh output directory required')
    config=json.loads((ROOT/'deploy/hope-batch03/source-profile.json').read_text());revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    # All tracked executable/config inputs must match this engine commit.
    if subprocess.run(['git','diff','--quiet','HEAD'],cwd=ROOT).returncode:raise ValueError('commit tracked inputs first')
    paths=sorted({*ROOT.glob('manager_bridge/*.py'),*ROOT.glob('taesan_core/*.py'),*ROOT.glob('service_adapters/hope_tour/*.mjs'),ROOT/'scripts/run_hope_batch03.py',ROOT/'deploy/hope-batch03/source-profile.json'})
    engine_files={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    for p in paths:
        if subprocess.check_output(['git','show',revision+':'+str(p.relative_to(ROOT))],cwd=ROOT)!=p.read_bytes():raise ValueError('uncommitted execution input')
    service=ActualServiceProfile(args.source,config['source_revision'],config['docker_image']);service.source_check()
    patch=subprocess.check_output(['git','diff','--binary',config['baseline_revision'],config['source_revision']],cwd=service.source)
    # Original evidence is a committed source33 observation, never a claim about current deployed source.
    baseline_path=ROOT/'docs/qa/hope-tour/batch02/run-20261116/actual-source-evidence.json.gz'
    baseline=json.loads(gzip.decompress(baseline_path.read_bytes()))
    if baseline['source_revision']!=config['baseline_revision']:raise ValueError('baseline source mismatch')
    trusted=contract(service.revision)
    with tempfile.TemporaryDirectory(prefix='hope-manager-integration-') as tmp:
        store=Store(Path(tmp)/'manager.sqlite');owner=store.add_token('local-owner','owner',['*']);store.register_project(owner,'hope-tour','local/hope-tour-source')
        worker=store.add_token('local-worker','worker',['hope-tour']);store.register_project(owner,'outside','local/other');outsider=store.add_token('outside','worker',['outside'])
        profile=HopeProfile('hope-tour','actual-source-terms-manager-v1',service,ROOT/'service_adapters/hope_tour')
        registry=HopeRegistry(store,[profile]);registry.provision(owner,profile)
        server=make_server(store,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_address[1])
        try:
            def containers():return subprocess.check_output(['docker','ps','-a','--filter','name=hope-qa-','--format','{{.Names}}'],env=_docker_env(),text=True).splitlines()
            def stop_probe():
                prior=containers();calls=0
                def fence():
                    nonlocal calls
                    calls+=1
                    if calls>=2:raise QAInterrupted('local_stop_probe')
                stopped=False
                try:execute_actual_service(service,profile.adapter_root,entrypoint='run-batch03.mjs',heartbeat=fence)
                except QAInterrupted:stopped=True
                return stopped and calls>=2 and containers()==prior
            host,probe=integration_checks(store,base,owner,worker,outsider,registry,profile,stop_probe)
            jobs=QAJobs(store);approval=jobs.approve(owner,project=profile.project,source_revision=profile.revision,contract=trusted,coverage=['batch03-'+str(n) for n in range(41,61)],adapter_release=profile.release,max_seconds=90,execution_profile_digest=profile.fingerprint)
            job=jobs.receive(owner,approval,'approved-batch03','hope-batch03-source-candidate')
            manifest=jobs.inspect(owner,job['id'])['manifest'];last={}
            def evaluator(raw):
                result=evaluate(raw,service.revision,trusted,host,baseline);last.update(result);return result['common_report']
            engine=HopeEngine(registry,manifest,evaluator);outcome=QAWorker(QAClient(base,worker),registry,engine=engine).run_once(job['id'])
            if not engine.raw or not last or outcome['state'] not in ('accepted','blocked'):raise ValueError('manager execution uncertain; do not retry automatically')
            raw=engine.raw;raw.update(engine_revision=revision,engine_files=engine_files,baseline_revision=config['baseline_revision'],manager_probe=probe)
            # Recover the actual centrally stored report including job binding/receipt.
            final=jobs.get(owner,job['id']);last['common_report']=final['report'];last['admission']=final['decision']
            prior=json.loads((ROOT/'docs/qa/hope-tour/batch02/run-20261116/customer-report.json').read_text());report=customer_report(raw,last,revision,prior)
            export(args.output,raw,last,report,patch,dict(job_id=job['id'],state=outcome['state'],decision=outcome['decision'],adapter_release=profile.release,profile_digest=profile.fingerprint,transport='real HTTP loopback, temporary manager DB',remote_central_server_connected=False))
            artifacts=HopeArtifacts(store);inventory=artifacts.collect(owner,job['id'],args.output)
            fetched=[]
            for item in inventory:
                response=request(base,owner,'GET','/v1/qa/jobs/'+job['id']+'/artifacts/'+item['name'],max_response_bytes=12*1024*1024)
                if not response.get('ok') or hashlib.sha256(base64.b64decode(response['artifact']['data'])).hexdigest()!=item['sha']:raise ValueError('final evidence recovery failed')
                fetched.append(item['name'])
            (args.output/'manager-recovery.json').write_text(json.dumps(dict(job_id=job['id'],state=final['state'],artifacts=inventory,http_round_trip_verified=fetched,remote_server_connected=False),ensure_ascii=False,indent=2)+'\n')
            (args.output/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in sorted(args.output.iterdir()) if p.is_file() and p.name!='SHA256SUMS'))
            if any(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=value for p,value in engine_files.items()):raise ValueError('engine changed during execution')
            print(json.dumps(dict(counts=last['counts'],manager=outcome['state'],customer_counts=report['summary']['counts'],artifacts_recovered=len(fetched),source=profile.revision),ensure_ascii=False))
            return 0 if final['decision']['admitted'] else 1
        finally:server.shutdown();thread.join(timeout=3);server.server_close()

if __name__=='__main__':raise SystemExit(main())
