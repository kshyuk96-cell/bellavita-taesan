"""Generate customer-format reports and optional isolated Hope review observations."""
import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from taesan_core.hope_tour_qa import formula_suite,fixture_adapters,REFERENCE_SHA256,FORMAT_SHA256,POLICY_VERSION
from taesan_core.universal_qa import run_scenarios,build_contract
from manager_bridge.qa_admission import admit_qa_report
from manager_bridge.hope_review_client import HopeReviewClient,ORIGIN
from manager_bridge.hope_tour_acceptance import collect,evaluate
from manager_bridge.hope_tour_reporting import final_report
from manager_bridge.hope_tour_exports import document,spreadsheet

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--sut-revision',required=True)
    parser.add_argument('--seed',type=int,default=20261116)
    parser.add_argument('--live-review',action='store_true')
    parser.add_argument('--token-file',type=Path)
    parser.add_argument('--target-manifest',type=Path)
    parser.add_argument('--evidence-json',type=Path)
    args=parser.parse_args()
    if not re.fullmatch('[a-f0-9]{40}',args.sut_revision):parser.error('exact target source SHA required')
    if args.live_review and args.evidence_json:parser.error('choose live review or observation import')
    engine=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    paths=['scripts/run_hope_tour_qa.py','taesan_core/universal_qa.py','taesan_core/hope_tour_qa.py',
           'manager_bridge/qa_admission.py','manager_bridge/hope_review_client.py','manager_bridge/hope_tour_acceptance.py',
           'manager_bridge/hope_tour_reporting.py','manager_bridge/hope_tour_exports.py','manager_bridge/store.py']
    for path in paths:
        if subprocess.check_output(['git','show',engine+':'+path],cwd=ROOT)!=(ROOT/path).read_bytes():
            raise ValueError('commit executable source before reporting: '+path)
    target=json.loads(args.target_manifest.read_text()) if args.target_manifest else None
    if args.live_review and (not target or target.get('source_revision')!=args.sut_revision or
                            target.get('origin')!=ORIGIN or target.get('deployment_status')!='succeeded'):
        parser.error('operator-reviewed source/version/deployment manifest required')
    output=args.output;output.mkdir(parents=True,exist_ok=True)
    def save(name,value):
        with gzip.GzipFile(filename=str(output/name),mode='wb',mtime=0) as f:
            f.write(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
    scenarios=formula_suite(args.seed)
    coverage=['principal-first','principal-refund-ceiling','grade-category-cap','grade-initial-charge']
    adapters=fixture_adapters();contract=build_contract(scenarios,adapters)
    reference=run_scenarios(scenarios,adapters,source_revision=engine,seed=args.seed,required_coverage=coverage)
    assert admit_qa_report(reference,expected_source_revision=engine,required_coverage=coverage,expected_contract=contract)['admitted']
    save('reference-selfcheck.json.gz',reference);save('reference-contract.json.gz',contract)
    mutations=[]
    for mutation in ('bonus_first','ignore_bp_cap','refund_bonus','refund_signup_fee'):
        report=run_scenarios(scenarios,fixture_adapters(mutation),source_revision=engine,seed=args.seed,required_coverage=coverage)
        decision=admit_qa_report(report,expected_source_revision=engine,required_coverage=coverage,expected_contract=contract)
        assert report['counts']['fail']>0 and not decision['admitted']
        mutations.append(dict(mutation=mutation,counts=report['counts'],admitted=decision['admitted']))
        save('mutation-'+mutation+'.json.gz',report)
    stream=io.StringIO();writer=csv.writer(stream)
    writer.writerow(['scenario_id','priority','status','formula_contract','calculation_conditions','approval_state','expected','actual'])
    for row in reference['scenarios']:
        writer.writerow([row['id'],row['priority'],row['status'],json.dumps(row.get('formula'),ensure_ascii=False),
            'individual DP first; BP/SP order explicit example parameter; corporate/suspension not inferred',
            'customer-reference; final operational approval pending',json.dumps(row['expected']),json.dumps(row['actual'])])
    with gzip.GzipFile(filename=str(output/'formulas.csv.gz'),mode='wb',mtime=0) as f:f.write(stream.getvalue().encode())
    collected={'observations':{},'developer_claims':{},'transport_events':[],'errors':[]}
    if args.live_review:
        collected=collect(lambda:HopeReviewClient(token_file=args.token_file),sut_revision=args.sut_revision)
    elif args.evidence_json:collected=json.loads(args.evidence_json.read_text())
    evaluation=evaluate(collected['observations'],sut_revision=args.sut_revision,seed=args.seed)
    customer=final_report(evaluation,collected,engine_revision=engine,sut_revision=args.sut_revision)
    save('customer-observations.json.gz',collected)
    save('service-report.json.gz',evaluation['report']);save('service-contract.json.gz',evaluation['contract'])
    (output/'customer-report.json').write_text(json.dumps(customer,ensure_ascii=False,indent=2)+'\n')
    spreadsheet(customer,output/'customer-report.xlsx');document(customer,output/'customer-report.docx')
    summary=dict(engine_revision=engine,sut_revision=args.sut_revision,policy_version=POLICY_VERSION,
        reference_documents=[REFERENCE_SHA256,FORMAT_SHA256],source_file_hashes={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths},
        target_manifest=target,seed=args.seed,reference_selfcheck=reference['counts'],
        reference_scope='test-system self-check; NOT 2060 executions of the customer service',
        mutations=mutations,service_counts=evaluation['report']['counts'],
        service_admission=evaluation['admission'],customer_summary=customer['summary'],
        collector_errors=collected.get('errors',[]),operational_authorization=False)
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (output/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n'
        for p in sorted(output.iterdir()) if p.is_file() and p.name!='SHA256SUMS'))
    print(json.dumps({k:summary[k] for k in ('reference_selfcheck','service_counts','customer_summary','collector_errors')},ensure_ascii=False))

if __name__=='__main__':main()
