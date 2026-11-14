"""Synthetic order lifecycle replay with an in-memory ledger writer, no external calls."""
import contextlib,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import order_operations as ops
import order_origin
import order_lifecycle as life

def run():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;ops.setup(c);c.commit()
    @contextlib.contextmanager
    def db():
        with c:yield c
    counts={'amend_existing_row':0,'repeat_amendment_held':0,'additional_idempotent':0,'distinct_additional':0,'wrong_owner_held':0,'cancellation_idempotent':0,'cancel_blocks_writer':0}
    for i in range(1000):
        owner=f'staff{i}';key=f'parent{i}';number=10000+i
        details={'sender':'모의회사','venue':'모의홀','address':'서울특별시 강남구','event_time':'14:00'}
        with db() as con:
            ops.stage(con,key,owner,'kakao','synthetic',details,ops.now());order_origin.record_manual(con,key,owner,'synthetic staff confirmation')
            con.execute("UPDATE order_operations SET state='saved',order_number=? WHERE id=?",(number,key))
        life.command(db,owner,f'주문 수정 {number}\n설치장소: 모의홀\n주소: 부산광역시 해운대구')
        life.command(db,owner,f'주문 수정 {number}\n설치장소: 모의홀\n주소: 부산광역시 해운대구')
        assert c.execute('SELECT COUNT(*) FROM order_changes WHERE order_id=?',(key,)).fetchone()[0]==1
        counts['repeat_amendment_held']+=1
        def writer(marker,data,created,update):
            assert marker==key and update and data['event_time']=='14:00';return number,data
        ops.save(db,key,lambda _:None,writer);counts['amend_existing_row']+=1
        for rid in ('request1','request1'):
            life.command(db,owner,f'추가 주문 {number}\n요청ID: {rid}')
        assert c.execute('SELECT COUNT(*) FROM order_operations WHERE owner=?',(owner,)).fetchone()[0]==2
        counts['additional_idempotent']+=1
        life.command(db,owner,f'추가 주문 {number}\n요청ID: request2')
        assert c.execute('SELECT COUNT(*) FROM order_operations WHERE owner=?',(owner,)).fetchone()[0]==3
        counts['distinct_additional']+=1
        life.command(db,'other',f'주문 취소 {number}')
        assert c.execute('SELECT state FROM order_operations WHERE id=?',(key,)).fetchone()[0]=='saved'
        counts['wrong_owner_held']+=1
        for _ in range(2):life.command(db,owner,f'주문 취소 {number}')
        assert c.execute('SELECT COUNT(*) FROM order_changes WHERE order_id=?',(key,)).fetchone()[0]==2
        counts['cancellation_idempotent']+=1
        def forbidden(*a,**kw):raise AssertionError('Cancelled order reached writer')
        try:ops.save(db,key,forbidden,forbidden)
        except ValueError:counts['cancel_blocks_writer']+=1
        else:raise AssertionError('Cancelled order saved')
    c.close()
    report={'synthetic_checks':sum(counts.values()),'passed':counts,'external_calls':0,'live_side_effects':False}
    if len(sys.argv)>1:Path(sys.argv[1]).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
if __name__=='__main__':run()
