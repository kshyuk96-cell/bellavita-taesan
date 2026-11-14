"""Offline validation. Private real data stays outside the repository.

Generated messages derived from a ledger row are not raw Kakao conversations.
No network, live Sheets writes, chat sends, or payments are used.
"""
import argparse,collections,json,re,sys,time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import order_intake

ALIASES={'생일,공연':'생일/공연','개업,이전':'개업/이전','취임,영전':'취임/영전','기념,선물':'기념/선물'}
EVENTS={'근조','결혼','개업/이전','취임/영전','생일/공연','기념/선물'}
FIELDS={'event_type':'행사 구분','venue':'설치장소','recipient':'받는 분','sender':'보내는 분','artwork':'작품','size':'규격','message':'축하 문구'}

def normalized(value):return re.sub(r'\s+','',str(value or ''))

def cases(record):
    # A literal copy of confirmed fields is the expectation; variants only change input syntax.
    kind=ALIASES.get(record['event_type'],record['event_type'])
    expected={'event_type':kind,'venue':record['location'],'recipient':record['recipient'],'sender':record['sender'],'artwork':record['artwork'],'size':record['size'],'message':record['message']}
    if not expected['venue'] or not expected['sender']:return
    for style in ['canonical','compact','wide-colon','heading-block','spoken-labels']:
        lines=['주문 접수']
        for field,label in FIELDS.items():
            if not expected[field]:continue
            value=expected[field]
            if style=='compact':label=label.replace(' ','')
            if style=='heading-block':lines.extend([label,value]);continue
            if style=='spoken-labels':lines.append(label+'은 '+value);continue
            sep='：' if style=='wide-colon' else ': '
            lines.append(label+sep+value)
        yield style,'\n'.join(lines),expected

def run(path,report_path,limit=None):
    started=time.monotonic();data=json.loads(Path(path).read_text());counts=collections.Counter();groups=collections.defaultdict(collections.Counter);failures=[]
    for record in data['ledger'][:limit]:
        event=ALIASES.get(record['event_type'],record['event_type'])
        if event not in EVENTS:continue
        for style,message,expected in cases(record):
            counts['cases']+=1;groups[event]['cases']+=1
            try:
                got=order_intake.extract(message)
                bad=[field for field,val in expected.items() if normalized(got.get(field))!=normalized(val)]
                if bad:
                    semantic_bad=[]
                    for field in bad:
                        original=expected[field]
                        actual=got.get(field,'')
                        if field=='venue':
                            original=re.sub(r'(?m)^\s*주소\s*[:：]\s*','',original)
                            actual='\n'.join(filter(None,(got.get('venue'),got.get('address'))))
                        elif field=='recipient':original=re.sub(r'^\s*받으시는\s*분\s*[:：]\s*','',original)
                        if normalized(original)!=normalized(actual):semantic_bad.append(field)
                    if not semantic_bad:counts['strict_mismatch_but_information_preserved']+=1
                    else:counts['semantic_failures']+=1
                    raise AssertionError(','.join(bad))
                counts['passed']+=1;groups[event]['passed']+=1
            except Exception as error:
                counts['failed']+=1;groups[event]['failed']+=1
                counts['failed_'+style]+=1
                if len(failures)<80:failures.append({'row':record['row'],'style':style,'event_type':event,'error':type(error).__name__,'fields':str(error) if isinstance(error,AssertionError) else 'parser_rejected'})
    # Only raw, self-contained source rows are eligible without fetching or stubbing invitation HTML.
    raw={'total':len(data.get('raw_orders',[])),'eligible':0,'passed':0,'needs_link_or_context':0,'failed':0}
    for record in data.get('raw_orders',[]):
        text=record['text'];urls=order_intake.LINK.findall(text)
        if urls or not re.search(r'(?:설치\s*장소|주소)\s*[:：]',text):raw['needs_link_or_context']+=1;continue
        raw['eligible']+=1
        try:
            got=order_intake.extract(text)
            if record.get('sender') and normalized(got.get('sender'))!=normalized(record['sender']):raw['failed']+=1
            else:raw['passed']+=1
        except Exception:raw['failed']+=1
    report={'revision_scope':'local offline parser','source_ledger_records':len(data['ledger']),'ledger_derived_syntax_cases':dict(counts),'by_event_type':{k:dict(v) for k,v in groups.items()},'raw_kakao':raw,'failure_samples_without_customer_text':failures,'seconds':round(time.monotonic()-started,3),'live_side_effects':False,'limitations':['Ledger-derived inputs are generated syntax variations, not independent human conversations.','Invitation URL extraction and true customer identity/context require separate integration tests.']}
    Path(report_path).write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in report.items() if k!='failure_samples_without_customer_text'},ensure_ascii=False,indent=2))
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--report',required=True);p.add_argument('--limit',type=int);a=p.parse_args();run(a.source,a.report,a.limit)
