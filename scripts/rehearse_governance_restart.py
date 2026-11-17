"""Disposable real process-restart rehearsal, not an OS restart or daemon."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.governance_restart_rehearsal import rehearse_governance_restart


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--work-parent',type=Path);args=parser.parse_args(argv)
    try:
        output=args.output.absolute()
        if '..' in output.parts or any(p.is_symlink() for p in (output,*output.parents)) or output.exists():
            raise ValueError('new physical output required')
        report=rehearse_governance_restart(work_parent=args.work_parent)
        output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:
            stream.write(json.dumps(report,ensure_ascii=False,indent=2)+'\n');stream.flush();os.fsync(stream.fileno())
        print(json.dumps({'state':report['state'],'completed':report['completed'],
            'clean':report['scenarios']['clean']['state'],'uncertain':report['scenarios']['uncertain']['state'],
            'true_os_reboot_verified':False,'operational_change':False}))
        return 0 if report['completed'] else 2
    except Exception:
        print(json.dumps({'state':'unverified','error':'restart_rehearsal_not_verified','operational_change':False}));return 1


if __name__=='__main__':raise SystemExit(main())
