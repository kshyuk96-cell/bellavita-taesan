"""Run the disposable command-worker governance rehearsal; no model/operational work."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.governance_flow_rehearsal import rehearse_governance_flow


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--work-parent',type=Path)
    parser.add_argument('--max-ticks',type=int,default=30)
    args=parser.parse_args(argv)
    try:
        output=args.output.absolute()
        if '..' in output.parts or any(p.is_symlink() for p in (output,*output.parents)) or output.exists():
            raise ValueError('new physical proof output required')
        report=rehearse_governance_flow(work_parent=args.work_parent,max_ticks=args.max_ticks)
        output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        descriptor=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'w') as stream:
            stream.write(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            stream.flush();os.fsync(stream.fileno())
        print(json.dumps({'state':report['state'],'completed_full_flow':report['completed_full_flow'],
            'blocker':report['blocker'],'developer_commands':len(report['developer_commands']),
            'semantic_commands':len(report['semantic_commands']),'operational_change':False}))
        return 0 if report['completed_full_flow'] else 2
    except Exception:
        print(json.dumps({'state':'unverified','completed_full_flow':False,'error':'governance_rehearsal_not_verified',
            'operational_change':False}));return 1


if __name__=='__main__':raise SystemExit(main())
