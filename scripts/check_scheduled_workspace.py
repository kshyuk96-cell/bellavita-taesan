#!/usr/bin/env python3
"""Run a bounded private continuity probe; never starts development work."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager_bridge.scheduled_workspace_probe import prepare, respond, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'respond', 'verify'))
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--expected-hash')
    args = parser.parse_args()
    if args.action != 'prepare' and not args.expected_hash:
        parser.error('--expected-hash is required for respond and verify')
    try:
        result = (prepare(args.state_root) if args.action == 'prepare'
                  else {'respond': respond, 'verify': verify}[args.action](
                      args.state_root, expected_hash=args.expected_hash))
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({'verified': False, 'reason': 'probe_unavailable_or_invalid',
                          'execution_authorized': False}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
