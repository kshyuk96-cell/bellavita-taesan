"""Development URL + secret test accounts on stdin + developer checklist -> review package."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.handoff_intake import TestAccounts,read_checklist,prepare,draft_report
from manager_bridge.handoff_exports import export_package
from manager_bridge.handoff_probe import probe_url
from manager_bridge.hope_browser_contract import browser_plan


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--url',required=True);parser.add_argument('--checklist',type=Path,required=True);parser.add_argument('--project',default='검수 대상 서비스');parser.add_argument('--template',choices=['generic','hope-tour'],default='generic');parser.add_argument('--output',type=Path,required=True);parser.add_argument('--accounts-stdin',action='store_true');parser.add_argument('--probe',action='store_true');args=parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):raise ValueError('fresh review output directory required')
    accounts=TestAccounts(json.load(sys.stdin) if args.accounts_stdin else [])
    intake=prepare(args.url,read_checklist(args.checklist),accounts,project=args.project,template=args.template)
    probe=probe_url(intake['target']) if args.probe else None
    state=browser_plan('0'*40)
    readiness={k:v for k,v in state.items() if k in ('environment','supported_tool_present','status','instruction','automatic_fallback','preview_started','browser_installed','actual_browser_verified')}
    readiness.update(source_revision=None,reason='candidate_identity_pending; '+str(state['reason']),planned_widths=[360,390,768,1280],target=intake['target'])
    report=draft_report(intake,probe=probe,browser=readiness)
    exported=export_package(intake,report,args.output)
    for path in args.output.iterdir():
        if path.suffix in ('.json','.txt'):
            body=path.read_text()
            if accounts.redact(body)!=body:raise ValueError('private account data escaped report redaction')
    (args.output/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in sorted(args.output.iterdir()) if p.is_file() and p.name!='SHA256SUMS'))
    print(json.dumps(dict(mode=intake['mode'],developer_items=len(intake['developer_claims']),customer_rows=exported['rows'],customer_ready=False,document_status=report['document_status']),ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
