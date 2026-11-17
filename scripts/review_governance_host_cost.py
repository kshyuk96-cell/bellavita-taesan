"""Report-only host cost review. Does not contact a provider or stop resources."""
import argparse,json,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.host_cost_review import ReportedHostState,review_host_cost


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--outcome',choices=('ongoing','blocked','failed','abandoned','not_needed'),required=True)
    p.add_argument('--resource-state',choices=('active','stopped','deleted','unknown'),required=True)
    p.add_argument('--extras',choices=('present','none','unknown'),default='unknown')
    p.add_argument('--preservation',choices=('preserved','not_preserved','unknown'),default='unknown')
    p.add_argument('--observed-at',type=float,required=True)
    p.add_argument('--hourly-usd',default='0.036');p.add_argument('--projection-hours',type=int,default=24)
    a=p.parse_args(argv)
    try:
        v=review_host_cost(ReportedHostState(a.outcome,a.resource_state,a.extras,a.observed_at,a.preservation),now=time.time(),quoted_hourly_usd=a.hourly_usd,projection_hours=a.projection_hours)
        print(json.dumps(v,allow_nan=False));return 0
    except (ValueError,TypeError):
        print(json.dumps({'state':'cost_review_unavailable','automatic_deletion':False}));return 2


if __name__=='__main__':raise SystemExit(main())
