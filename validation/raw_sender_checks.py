"""Recorded raw sender-text checks. Invitation metadata is a fixture, not a live fetch."""
import collections,json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import order_intake

def normalize(value):return ''.join(str(value or '').split())

def run(source,output):
    data=json.loads(Path(source).read_text());counts=collections.Counter();issues=[]
    for record in data['raw_orders']:
        counts['total']+=1
        message='주문 접수\n'+record['source']+'\n'+record['text']
        event={'event_type':record['event_type'],'recipient':record['recipient'],'venue':'검증용 행사장','address':''}
        try:
            with patch.object(order_intake,'wedding',return_value=event):result=order_intake.extract(message)
            if normalize(result.get('sender'))==normalize(record['sender']):counts['sender_matched']+=1
            else:counts['sender_mismatch']+=1;issues.append({'source_row':record['row'],'reason':'sender_mismatch'})
        except ValueError:
            counts['held']+=1;issues.append({'source_row':record['row'],'reason':'unsupported_link_or_context'})
    report={'recorded_raw_sender_text':dict(counts),'issues_without_customer_text':issues,'invitation_metadata_stubbed':True,'live_invitation_extraction_tested':False,'scope':'sender text only; unsupported vendor detection retained'}
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':run(sys.argv[1],sys.argv[2])
