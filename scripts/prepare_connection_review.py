"""Prepare one local connection packet from fixed source evidence, no activation."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.control import secret
from manager_bridge.store import Store
from manager_bridge.maintenance_registry import Registry
from manager_bridge.connection_readiness import Connections
from manager_bridge.source_rehearsal import snapshot_profile


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--data',required=True,type=Path)
    parser.add_argument('--token-file',required=True,type=Path)
    parser.add_argument('--sources',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--request-key',required=True)
    args=parser.parse_args(argv)
    evidence=Path(__file__).resolve().parents[1]/'docs'/'connection-evidence'/'source-baselines.json'
    profiles=json.loads(evidence.read_text())['profiles']
    database=args.data/'maintenance.sqlite3'
    if any(p.is_symlink() for p in (database,*database.parents)) or not database.is_file():
        raise ValueError('existing private maintenance database required')
    if database.stat().st_mode & 0o077:
        raise ValueError('private maintenance database required')
    store=Store(database);registry=Registry(store)
    token=secret(args.token_file)
    hosts={row['project']:snapshot_profile(row,args.sources/row['project']) for row in profiles}
    review=Connections(store,registry,hosts)
    for project in hosts:review.inspect(token,project)
    packet=review.owner_packet(token,args.request_key,list(hosts))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(packet,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'projects':len(hosts),'activation_performed':False,'deployment_performed':False,
        'secure_workers_ready':sum(p.get('secure_worker_ready') is True for p in packet['packet']['projects']),
        'owner_actions':packet['packet']['owner_actions']}))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,RuntimeError):
        print(json.dumps({'ok':False,'error':'connection_review_not_verified'}));raise SystemExit(1)
