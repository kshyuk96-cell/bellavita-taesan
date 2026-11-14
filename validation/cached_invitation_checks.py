"""Actual retrieved invitation HTML + recorded order text. No live network in this runner."""
import collections,hashlib,json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import order_intake,invitation

def run(private,output):
    root=Path(private);data=json.loads((root/'source-orders.json').read_text());counts=collections.Counter();out=[]
    gold={row['row']:row['values'] for row in json.loads((root/'raw-gold.json').read_text())} if (root/'raw-gold.json').exists() else {}
    def get_page(url,**kwargs):
        key=hashlib.sha256(url.encode()).hexdigest();return (root/'pages'/(key+'.html')).read_text(errors='replace')
    for record in data['raw_orders']:
        counts['total']+=1;message='주문 접수\n'+record['source']+'\n'+record['text']
        try:
            with patch.object(invitation,'get_page',side_effect=get_page), patch.object(invitation,'read_itscard_view',return_value=json.loads((root/'itscard-view.json').read_text())):
                result=order_intake.extract(message)
            match=''.join(result['sender'].split())==''.join(record['sender'].split())
            counts['parsed']+=1;counts['sender_matched' if match else 'sender_mismatch']+=1
            reference=gold.get(record['row'],[])
            if result.get('event_type')=='결혼' and reference:
                import datetime as dt
                day=reference[0]['entered'].get('numberValue');clock=reference[1]['entered'].get('numberValue')
                if day is not None:
                    counts['reference_date_checked']+=1
                    expected=(dt.date(1899,12,30)+dt.timedelta(days=int(day))).isoformat()
                    counts['reference_date_matched' if result.get('event_date')==expected else 'reference_date_mismatch']+=1
                if clock is not None:
                    counts['reference_time_checked']+=1
                    total=round(clock*1440);expected=f'{total//60:02d}:{total%60:02d}'
                    counts['reference_time_matched' if result.get('event_time')==expected else 'reference_time_mismatch']+=1
            out.append({'row':record['row'],'parsed':True,'sender_matched':match,'event_type':result.get('event_type'),'has_date':bool(result.get('event_date')),'has_time':bool(result.get('event_time')),'has_address':bool(result.get('address'))})
        except Exception as e:
            counts['held']+=1;out.append({'row':record['row'],'parsed':False,'error_type':type(e).__name__,'reason':str(e)[:100]})
    report={'actual_page_checks':dict(counts),'cases_without_customer_text':out,'invitation_metadata_stubbed':False,'network_disabled_during_replay':True}
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2));return report
if __name__=='__main__':run(sys.argv[1],sys.argv[2])
