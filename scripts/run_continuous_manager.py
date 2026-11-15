"""Foreground trusted-host controller. A service supervisor owns uptime."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.continuous_host import load_host


def main(argv=None):
    parser=argparse.ArgumentParser(description='Registered cloud development supervisor')
    parser.add_argument('--config',type=Path,required=True)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--once',action='store_true')
    group.add_argument('--max-ticks',type=int)
    group.add_argument('--status',action='store_true')
    args=parser.parse_args(argv)
    if args.max_ticks is not None and not 1<=args.max_ticks<=100000:parser.error('max-ticks must be 1..100000')
    stopped=threading.Event()
    previous={}
    try:
        host,controller,config=load_host(args.config)
        for signum in (signal.SIGINT,signal.SIGTERM):
            previous[signum]=signal.signal(signum,lambda *_:stopped.set())
        token=host.manager_token()
        if args.status:result=controller.status(token,config['projects'])
        else:result=controller.run(token,config['projects'],config['instance_id'],stop_event=stopped,
            poll_seconds=config['poll_seconds'],max_ticks=1 if args.once else args.max_ticks)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False));return 0
    except Exception:
        # Config paths, provider stdout and credential errors are host-only.
        print(json.dumps({'state':'host_unavailable','automatic_replay':False,'operational_change':False}));return 2
    finally:
        for signum,handler in previous.items():signal.signal(signum,handler)


if __name__=='__main__':raise SystemExit(main())
