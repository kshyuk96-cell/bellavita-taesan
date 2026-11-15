"""Export public cross-project planning cases without answer keys or model calls."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.conversation_benchmark import public_cases


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--start',type=int,default=0);parser.add_argument('--count',type=int,default=100)
    args=parser.parse_args(argv);value=public_cases(args.start,args.count)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'cases':len(value['cases']),'catalog_hash':value['catalog_hash'],'model_called':False}))
    return 0


if __name__=='__main__':raise SystemExit(main())
