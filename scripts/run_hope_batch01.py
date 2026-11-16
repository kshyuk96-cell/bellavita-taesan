"""Run approved batch-01 against a dedicated local reference adapter only."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.hope_batch01 import execute_batch
from manager_bridge.hope_batch01_reports import export
from manager_bridge.hope_lab_transport import remote_fixture_readiness
from manager_bridge.hope_review_client import HopeReviewClient

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--review-token-file',type=Path)
    args=parser.parse_args()
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    paths=['scripts/run_hope_batch01.py','taesan_core/hope_test_data.py','taesan_core/hope_tour_qa.py',
        'manager_bridge/hope_lab_model.py','manager_bridge/hope_lab_store.py','manager_bridge/hope_lab_transport.py',
        'manager_bridge/hope_batch01.py','manager_bridge/hope_batch01_contract.py','manager_bridge/hope_batch01_reports.py','manager_bridge/hope_tour_reporting.py',
        'manager_bridge/hope_tour_exports.py','manager_bridge/hope_tour_acceptance.py','manager_bridge/hope_review_client.py',
        'manager_bridge/store.py','docs/qa/hope-tour/run-20261116/customer-report.json']
    for path in paths:
        if subprocess.check_output(['git','show',revision+':'+path],cwd=ROOT)!=(ROOT/path).read_bytes():
            raise ValueError('commit executable source first: '+path)
    prior=json.loads((ROOT/'docs/qa/hope-tour/run-20261116/customer-report.json').read_text())
    def probe(items):
        preview=dict(source_revision=revision,items=items,counts={},evidence=[])
        return export(preview,args.output,prior=prior)
    batch=execute_batch(revision,report_probe=probe)
    batch['source_files']={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
    batch['remote_adapter']=dict(connected=False,setup_allowed=False,reason='remote_capability_not_observed',operational_authorization=False)
    if args.review_token_file:
        try:
            client=HopeReviewClient(token_file=args.review_token_file);caps=client.capabilities()
            batch['remote_adapter']=dict(remote_fixture_readiness(caps),observed_capabilities={k:caps.get(k) for k in
                ('mode','testDataProtocol','operationalWrites','realCharges','realNotifications','cardProvider')},
                read_only=True,transport=client.events,source_binding='not_verified_readiness_only')
        except Exception:batch['remote_adapter']=dict(connected=False,setup_allowed=False,reason='readiness_unverified',retry_performed=False)
    for path in paths:
        if hashlib.sha256((ROOT/path).read_bytes()).hexdigest()!=batch['source_files'][path]:
            raise ValueError('execution source changed during run')
    if subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()!=revision:
        raise ValueError('source revision changed during run')
    final=export(batch,args.output,prior=prior)
    if final['customer_promoted']:raise AssertionError('reference evidence promoted to customer PASS')
    with gzip.GzipFile(filename=str(args.output/'local-evidence.json.gz'),mode='wb',mtime=0) as f:
        f.write(json.dumps(batch,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
    for name,value in [('local-common-report.json.gz',batch['common_report']),('local-trusted-contract.json.gz',batch['trusted_contract'])]:
        with gzip.GzipFile(filename=str(args.output/name),mode='wb',mtime=0) as f:
            f.write(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
    summary={k:v for k,v in batch.items() if k not in ('evidence','common_report','trusted_contract')}
    (args.output/'batch-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (args.output/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n'
        for p in sorted(args.output.iterdir()) if p.is_file() and p.name!='SHA256SUMS'))
    print(json.dumps(dict(counts=batch['counts'],scope=batch['scope'],remote=batch['remote_adapter'],customer_site_verified=False),ensure_ascii=False))
    return 0 if batch['counts']['PASS']==20 and batch['local_admission']['admitted'] else 1

if __name__=='__main__':raise SystemExit(main())
