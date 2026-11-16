"""Read-only point-in-time source handoff inventory; never fetches or executes code."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.connection_readiness import SourcePin
from manager_bridge.handoff_inventory import inspect_handoff
from manager_bridge.source_watch import SourceHead


def read_json(path):
    path=Path(path)
    if path.stat().st_size>2_000_000:raise ValueError('bounded host-side metadata required')
    def pairs(items):
        out={}
        for k,v in items:
            if k in out:raise ValueError('duplicate metadata field')
            out[k]=v
        return out
    return json.loads(path.read_text(),object_pairs_hook=pairs)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Inspect source copies without modifying them')
    parser.add_argument('--catalog',type=Path,required=True)
    parser.add_argument('--baselines',type=Path,required=True)
    parser.add_argument('--heads',type=Path,required=True)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        catalog=read_json(args.catalog);raw=read_json(args.baselines);heads=read_json(args.heads)
        profiles=raw.get('profiles') if type(raw) is dict else None
        if type(catalog) is not dict or type(profiles) is not list or not 1<=len(profiles)<=100 or type(heads) is not list or len(heads)>100:raise ValueError('bounded project inventory required')
        pins=[SourcePin(**row['pin']) for row in profiles]
        observations=[SourceHead(**row) for row in heads]
        if len({p.project for p in pins})!=len(pins) or len({h.project for h in observations})!=len(observations):raise ValueError('duplicate project metadata')
        if any(p.project not in catalog for p in pins) or any(h.project not in {p.project for p in pins} for h in observations):raise ValueError('metadata project scope differs')
        root=args.source_root.absolute();destination=args.output.absolute()
        if '..' in root.parts or any(p.is_symlink() for p in (root,*root.parents)) or not root.is_dir():raise ValueError('physical source inventory root required')
        inputs={p.absolute() for p in (args.catalog,args.baselines,args.heads)}
        if '..' in destination.parts or destination in inputs or destination==root or root in destination.parents or any(p.is_symlink() for p in (destination,*destination.parents)):raise ValueError('output must be outside source inventory and metadata inputs')
        by_project={h.project:h for h in observations}
        results=[inspect_handoff(catalog[p.project],root/p.project,p,by_project.get(p.project)) for p in pins]
        report={'schema_version':1,'scope':'read_only_customer_source_handoff_inventory',
            'projects':results,'source_aligned_count':sum(r['source_ready_for_handoff'] for r in results),
            'execution_enabled_count':0,'source_modified':False,'tests_executed':0,'operational_change':False}
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'state':'source_inventory_only','projects':len(results),'source_aligned':report['source_aligned_count'],'execution_enabled':0}));return 0
    except (KeyError,TypeError,ValueError,OSError):
        print(json.dumps({'state':'inventory_unavailable','source_modified':False,'execution_enabled':0}));return 2


if __name__=='__main__':raise SystemExit(main())
