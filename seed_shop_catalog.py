"""Insert the approved four products without overwriting any existing edits."""
import argparse
import json
import os


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);args=parser.parse_args()
    os.environ['TAESAN_DATA']=args.data
    import agent
    import shop_catalog
    with agent.db() as c:inserted=shop_catalog.seed(c)
    print(json.dumps({'inserted':len(inserted),'ids':inserted},ensure_ascii=False))


if __name__=='__main__':main()
