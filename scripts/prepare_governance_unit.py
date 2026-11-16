"""Generate one reviewable private systemd draft; never install or operate it."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.runtime_transition import UnitDraft,write_unit_draft


def main(argv=None):
    parser=argparse.ArgumentParser(description='Prepare a governance systemd draft only')
    for field in ('application-root','python','config-file','state-directory','output'):
        parser.add_argument('--'+field,required=True)
    parser.add_argument('--user',default='taesan-manager');parser.add_argument('--group',default='taesan-manager')
    parser.add_argument('--unit-name',default='taesan-governance')
    args=parser.parse_args(argv)
    try:
        draft=UnitDraft(args.application_root,args.python,args.config_file,args.state_directory,args.user,args.group,args.unit_name)
        result=write_unit_draft(draft,args.output)
        print(json.dumps(result,allow_nan=False));return 0
    except Exception:
        print(json.dumps({'state':'unit_draft_unavailable','installed':False,'started':False,'system_service_changed':False}));return 2


if __name__=='__main__':raise SystemExit(main())
