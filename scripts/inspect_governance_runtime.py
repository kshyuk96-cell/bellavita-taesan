"""Read ten retained-state summaries from an already running authenticated API."""
import argparse
import json
import math
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.control import request,secret,validate_url
from manager_bridge.continuous_host import _private_file
from manager_bridge.maintenance_registry import name
from manager_bridge.operations_control_api import PREFIX

SECTIONS={'registration','roles','tasks','actions','schedules','authentication_records',
          'host_requirements','transitions','restore_quarantine','backup_records'}
FLAGS={'automatic_replay','credential_refresh_performed','providers_invoked',
       'runtime_records_written','authentication_verified','worker_execution_verified',
       'scheduler_running_verified','always_on_verified','execution_authorized'}
STATES={'taesan','developer','reviewer','pending_approval','queued','running','reported',
        'reviewed','cancelled','needs_verification','waiting','uncertain','complete',
        'blocked','confirmed','reconciled','observed','unavailable','retention_required','ready','expired',
        'unknown','accepting','draining','preparing','uploading','stored'}


def validate_snapshot(value):
    if (type(value) is not dict or set(value)!={'scope','observed_at','project_count','sections','section_count','models_started',*FLAGS}
            or value['scope']!='retained_database_metadata_only'
            or type(value['observed_at']) not in (int,float) or not math.isfinite(value['observed_at']) or value['observed_at']<=0
            or type(value['project_count']) is not int or not 1<=value['project_count']<=100
            or any(value[k] is not False for k in FLAGS)
            or type(value['models_started']) is not int or value['models_started']!=0
            or value['section_count']!=10 or type(value['sections']) is not list or len(value['sections'])!=10):raise ValueError()
    seen=set()
    for item in value['sections']:
        if (type(item) is not dict or not {'section','availability'}<=set(item)
                or set(item)-{'section','availability','counts','other_count','project_count','maintenance_enabled_count','requirement_count','reconciliation_required'}
                or type(item['section']) is not str or item['section'] not in SECTIONS or item['section'] in seen
                or item['availability'] not in ('recorded','not_recorded','restricted')):raise ValueError()
        seen.add(item['section'])
        for k,v in item.items():
            if k in ('section','availability'):continue
            if k=='counts':
                if type(v) is not dict or any(key not in STATES or type(n) is not int or n<0 for key,n in v.items()):raise ValueError()
            elif k=='reconciliation_required':
                if type(v) is not bool:raise ValueError()
            elif type(v) is not int or v<0:raise ValueError()
    return value


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--token-file',type=Path,required=True)
    parser.add_argument('--project',action='append',required=True)
    args=parser.parse_args(argv)
    try:
        base=validate_url(args.base_url)
        if not 1<=len(args.project)<=100 or len(set(args.project))!=len(args.project):raise ValueError()
        projects=[name(p) for p in args.project]
        token=secret(_private_file(args.token_file))
        result=request(base,token,'POST',PREFIX+'diagnostics',{'projects':projects})
        if type(result) is not dict or result.get('ok') is not True:raise ValueError()
        value=validate_snapshot(result['result'])
        print(json.dumps(value,allow_nan=False));return 0
    except Exception:
        print(json.dumps({'state':'diagnostics_unavailable','retry_performed':False,'execution_authorized':False}));return 2


if __name__=='__main__':raise SystemExit(main())
