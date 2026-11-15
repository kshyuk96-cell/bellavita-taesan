#!/usr/bin/env python3
"""Reproduce pure synthetic QA and mutation demonstrations without external calls."""
import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from taesan_core.qa_fixtures import build_suite, adapters, REQUIRED_COVERAGE
from taesan_core.universal_qa import run_scenarios, fingerprint
from manager_bridge.qa_admission import admit_qa_report


def run(seed, output):
    revision = subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip()
    paths = ['taesan_core/universal_qa.py','taesan_core/qa_fixtures.py','manager_bridge/qa_admission.py',
             'taesan_core/commerce.py','taesan_core/core.py','scripts/run_universal_qa.py']
    # Source revision is meaningful only if all executable inputs match committed files.
    for path in paths:
        committed = subprocess.check_output(['git','show', revision+':'+path], cwd=ROOT)
        if committed != (ROOT/path).read_bytes():
            raise ValueError('commit executable source before reporting: '+path)
    source_files = {path: hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in paths}
    suite = build_suite(seed)
    report = run_scenarios(suite, adapters(), source_revision=revision, seed=seed, required_coverage=REQUIRED_COVERAGE)
    report['source_files'] = source_files
    report.pop('report_digest')
    report['report_digest'] = fingerprint(report)
    admission = admit_qa_report(report, expected_source_revision=revision, required_coverage=REQUIRED_COVERAGE)
    mutations = []
    for mutant in ('allow_zero_quantity','skip_tenant','replay_unknown','truncate_won'):
        altered = run_scenarios(suite, adapters(mutant), source_revision=revision, seed=seed, required_coverage=REQUIRED_COVERAGE)
        decision = admit_qa_report(altered, expected_source_revision=revision, required_coverage=REQUIRED_COVERAGE)
        failed = [row for row in altered['scenarios'] if row['status']=='fail']
        mutations.append({'mutant': mutant, 'counts': altered['counts'], 'admitted': decision['admitted'],
                          'samples': failed[:2]})
        if not failed or decision['admitted']:
            raise AssertionError('mutation was not caught: '+mutant)
    output.mkdir(parents=True, exist_ok=True)
    # Fixed mtime makes exports byte-reproducible. Full scenario evidence stays compressed.
    with gzip.GzipFile(filename=str(output/'scenarios.json.gz'), mode='wb', mtime=0) as file:
        file.write(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode())
    text = io.StringIO(newline='')
    writer = csv.DictWriter(text, fieldnames=['scenario_id','priority','status','formula_id','expression','variables','units','rounding','expected','actual'])
    writer.writeheader()
    for row in report['scenarios']:
        if 'formula' in row:
            f = row['formula']
            writer.writerow({'scenario_id':row['id'],'priority':row['priority'],'status':row['status'],
                             'formula_id':f['id'], **{k:json.dumps(f[k],ensure_ascii=False,sort_keys=True) if isinstance(f[k],dict) else f[k]
                             for k in ('expression','variables','units','rounding','expected','actual')}})
    with gzip.GzipFile(filename=str(output/'formulas.csv.gz'), mode='wb', mtime=0) as file:
        file.write(text.getvalue().encode())
    summary = {k:v for k,v in report.items() if k!='scenarios'}
    summary['admission'] = admission
    summary['mutations'] = mutations
    summary['exports'] = {name:hashlib.sha256((output/name).read_bytes()).hexdigest() for name in ('scenarios.json.gz','formulas.csv.gz')}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'counts':report['counts'],'admitted':admission['admitted'],
                      'mutations':[{k:m[k] for k in ('mutant','counts','admitted')} for m in mutations]},ensure_ascii=False))
    if not admission['admitted']:
        raise AssertionError('baseline admission failed: '+str(admission))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=20261116)
    parser.add_argument('--output', type=Path, default=ROOT/'docs/qa/run-20261116')
    args = parser.parse_args()
    run(args.seed, args.output)
