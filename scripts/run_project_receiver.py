"""Opt-in existing project receiver; defaults to inspection, never a daemon."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager_bridge.agent_directory import AgentDirectory
from manager_bridge.control import secret
from manager_bridge.project_runtime import ControlChecks, ProjectReceiver, ProjectTestProfile
from manager_bridge.store import Store


def private(path):
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('private non-symlink file required')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private existing host file required')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--agent', required=True)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--max-cycles', type=int, default=1)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_cycles <= 100 or (args.run and args.recover):
            raise ValueError('bounded explicit mode required')
        database = private(args.database)
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as c:
            tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'bridge_tokens','bridge_tasks','maintenance_projects','manager_agents','manager_agent_inbox'} <= tables:
                raise ValueError('existing registered receiver database required')
        profile_path = private(args.profile)
        if profile_path.stat().st_size > 10000:
            raise ValueError('bounded host profile required')
        config = json.loads(profile_path.read_text())
        if type(config) is not dict or not {'project','repository','base_sha','source','runtime'} <= set(config) or not set(config) <= {
                'project','repository','base_sha','source','runtime','dependencies','timeout','control'}:
            raise ValueError('exact registered host profile required')
        if config.get('control') is not None:
            config['control'] = ControlChecks(**config['control'])
        profile = ProjectTestProfile(**config)
        token = secret(private(args.token_file))
        store = Store(database)
        receiver = ProjectReceiver(store, AgentDirectory(store), profile)
        if args.recover:
            result = receiver.recovery(token, args.agent)
        elif not args.run:
            result = receiver.check(token, args.agent)
        else:
            results = []
            for _ in range(args.max_cycles):
                value = receiver.run_once(token, args.agent); results.append(value)
                if value['state'] != 'reported': break
            result = {'cycles': len(results), 'results': results, 'background_process_started': False,
                      'schedule_created': False, 'operational_change': False}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({'ok': False, 'error': 'project_receiver_not_verified',
                          'execution_started': False, 'operational_change': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
