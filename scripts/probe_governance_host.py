"""Run on the target host; it does not SSH, deploy, refresh auth or start models."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.host_capabilities import inspect_host


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--expected-service-id')
    parser.add_argument('--state-root',type=Path)
    args=parser.parse_args()
    result=inspect_host(expected_service_id=args.expected_service_id,state_root=args.state_root)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['namespace_isolation_verified'] else 2


if __name__=='__main__':raise SystemExit(main())
