"""Foreground authenticated governance HTTP and continuous manager host."""
import argparse
import json
from pathlib import Path
import signal
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.governance_service import load_service


def main(argv=None):
    parser=argparse.ArgumentParser(description='Trusted governance service')
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--subscription-config',type=Path,help='Private fixed native subscription bootstrap settings')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--once',action='store_true')
    group.add_argument('--max-ticks',type=int)
    group.add_argument('--status',action='store_true')
    args=parser.parse_args(argv)
    if args.max_ticks is not None and not 1<=args.max_ticks<=100000:parser.error('max-ticks must be 1..100000')
    service=None;previous={}
    try:
        if args.subscription_config is None:
            service=load_service(args.config)
        else:
            from manager_bridge.managed_service_launch import load_managed_service
            service=load_managed_service(args.config,args.subscription_config)
        for sig in (signal.SIGTERM,signal.SIGINT):previous[sig]=signal.signal(sig,lambda *_:service.stop())
        result=service.status() if args.status else service.run(max_ticks=1 if args.once else args.max_ticks)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False));return 0
    except Exception:
        print(json.dumps({'state':'host_unavailable','automatic_replay':False,'operational_change':False}));return 2
    finally:
        if service is not None:service.close()
        for sig,handler in previous.items():signal.signal(sig,handler)


if __name__=='__main__':raise SystemExit(main())
