"""Local maintenance inventory/report entry point; never starts a daemon or deploys."""
import argparse
import json
import os
from pathlib import Path
import sys
from .control import secret, write_secret
from .store import Store
from .maintenance_registry import Registry


def main(argv=None):
    parser=argparse.ArgumentParser(description='태산이 유지보수 등록부·통합 보고')
    parser.add_argument('--data',required=True,type=Path)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init')
    for action in ('inventory','report','inspect'):
        item=sub.add_parser(action)
        item.add_argument('--token-file',required=True,type=Path)
        if action=='inspect':item.add_argument('--project',required=True)
    args=parser.parse_args(argv)
    try:
        path=args.data.absolute()
        if any(p.is_symlink() for p in (path,*path.parents)):
            raise ValueError('private data directory required')
        if args.command=='init':
            path.mkdir(mode=0o700,parents=True,exist_ok=True)
            if path.stat().st_mode & 0o077:raise ValueError('private data directory required')
            # Reserve bootstrap so two initializers cannot both issue owners.
            lock=path/'bootstrap.lock'
            fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            os.close(fd)
            if (path/'maintenance.sqlite3').exists() or (path/'owner.token').exists() or (path/'manager.token').exists():
                raise ValueError('existing data must not be overwritten')
            configs=json.loads(Path(__file__).with_name('maintenance_projects.json').read_text())
            store=Store(path/'maintenance.sqlite3');registry=Registry(store)
            owner=store.add_token('maintenance-owner','owner',list(configs))
            for project,config in configs.items():registry.register(owner,project,config)
            manager=store.add_token('taesan-maintenance','manager',list(configs))
            write_secret(path/'owner.token',owner);write_secret(path/'manager.token',manager)
            print(json.dumps({'projects':sorted(configs),'credentials_written':True,
                'maintenance_enabled':False,'schedules_changed':False,'operational_change':False}))
            return 0
        if (path/'maintenance.sqlite3').is_symlink():raise ValueError('database symlink forbidden')
        if not (path/'maintenance.sqlite3').is_file():raise ValueError('initialize the local inventory first')
        token=secret(args.token_file)
        store=Store(path/'maintenance.sqlite3');registry=Registry(store)
        if args.command=='inventory':result=registry.list(token)
        elif args.command=='inspect':result=registry.inspect(token,args.project)
        else:
            from .maintenance_cycle import Digest
            result=Digest(store,registry).report(token)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return 0
    except Exception:
        # Exceptions may contain provider output or credential paths; do not echo them.
        print(json.dumps({'ok':False,'error':'maintenance_request_rejected','operational_change':False}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
