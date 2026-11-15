"""Export synthetic model cases; no answer key, model call or credentials."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.model_readiness import BANK_VERSION,CATALOG_HASH,INSTRUCTIONS,public_cases


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--start',type=int,default=0);parser.add_argument('--count',type=int,default=100)
    args=parser.parse_args(argv)
    value={'bank_version':BANK_VERSION,'catalog_hash':CATALOG_HASH,'instructions':INSTRUCTIONS,
           'cases':public_cases(args.start,args.count),'measurement':'synthetic_text_only',
           'actual_model_called':False,'operational_test':False}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'cases':len(value['cases']),'catalog_hash':CATALOG_HASH,'answer_key_exported':False}))


if __name__=='__main__':main()
