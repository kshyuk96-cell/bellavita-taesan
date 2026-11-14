"""Synthetic clarification/schedule/fragment/status/triage integration checks."""
import contextlib,datetime as dt,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import order_assistance as assist
import order_operations as ops


def run():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;ops.setup(c);c.commit()
    @contextlib.contextmanager
    def db():
        with c:yield c
    now=dt.datetime(2026,11,14,12,tzinfo=assist.SEOUL)
    counts={key:0 for key in ('missing_only','schedule_hold','no_shipping_inference','fragment_replay','status_no_fabricated_receipt','owner_isolation','review_list')}
    for i in range(1000):
        base={'sender':f'모의조직{i}','recipient':'모의수신자','event_type':'결혼','delivery_date':'2026-11-15','delivery_time':'10:00','event_date':'2026-11-15','event_time':'14:00','venue':'모의홀','address':'서울특별시 강남구','artwork':'모의작품','size':'2호'}
        missing=dict(base);missing.pop('size');assert assist.question(missing,now)=='이번 주문에서 규격만 알려주세요.';counts['missing_only']+=1
        bad={**base,'delivery_time':f'{14+i%10:02d}:{i%60:02d}'}
        assert assist.assess(bad,now)['hold'];counts['schedule_hold']+=1
        no_time=dict(base);no_time.pop('delivery_time');assert '배송시각' in assist.assess(no_time,now)['missing'];counts['no_shipping_inference']+=1
        owner=f'staff{i}';key=f'draft{i}';number=10000+i
        with db() as con:
            # No wall-clock-dependent dates in command replay; these orders need clarification.
            data={k:v for k,v in base.items() if not k.endswith('_date')}
            ops.stage(con,key,owner,'kakao','synthetic',data,ops.now())
            con.execute("UPDATE order_operations SET state='saved',order_number=? WHERE id=?",(number,key))
        cmd=f'주문 문구추가 {number}\n메시지ID: fragment{i}\n보내는분: 모의직함{i}'
        assert '합쳤' in assist.command(db,owner,cmd)
        assert '이미' in assist.command(db,owner,cmd)
        assert c.execute('SELECT COUNT(*) FROM order_fragments WHERE order_id=?',(key,)).fetchone()[0]==1;counts['fragment_replay']+=1
        assert '카톡 공유: 미확인' in assist.command(db,owner,f'주문 상태 {number}');counts['status_no_fabricated_receipt']+=1
        assert '확인하지 못' in assist.command(db,'other',f'주문 상태 {number}');counts['owner_isolation']+=1
        backlog=assist.command(db,owner,'주문 검토 대기');assert str(number) in backlog and len(backlog)<950;counts['review_list']+=1
    c.close();result={'synthetic_checks':sum(counts.values()),'passed':counts,'external_calls':0,'live_side_effects':False}
    if len(sys.argv)>1:Path(sys.argv[1]).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
if __name__=='__main__':run()
