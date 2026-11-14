"""1,000 synthetic complete flows using production queue/ledger logic and a fake sheet."""
import contextlib,json,sqlite3,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from sheet_fake import MemoryLedger
from native_form_fake import native_payload
import order_operations as ops
import order_intake,order_lifecycle as life,order_assistance as assist,order_sharing as sharing

def run():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;ops.setup(c);c.commit()
    @contextlib.contextmanager
    def db():
        with c:yield c
    book=MemoryLedger()
    counts={key:0 for key in ('intake_full_verification','duplicate_no_new_row','same_row_amendment','fragment_replay','both_sharing_receipts','additional_replay','cancellation_invalidates_share','cancelled_save_blocked')}
    for i in range(1000):
        owner=f'staff{i}';text=f'주문 접수\n설치장소: 모의홀{i}\n주소: 서울특별시 강남구\n보내는분: 모의회사{i}\n받는분: 모의수신자{i}\n행사구분: 결혼\n작품: 모의작품{i}\n규격: 2호\n배송일: 2099-12-01\n배송시간: {i%24:02d}:{i%60:02d}\n문구: 모의문구{i}'
        key,_=ops.intake(db,owner,text);number,data=ops.save(db,key,order_intake.extract,book.save)
        assert data['sender']==f'모의회사{i}' and book.rows[number][3]['stringValue']==f'모의작품{i}';counts['intake_full_verification']+=1
        before=len(book.rows);again,_=ops.intake(db,owner,text);ops.save(db,again,order_intake.extract,book.save)
        assert len(book.rows)==before;counts['duplicate_no_new_row']+=1
        life.command(db,owner,f'주문 수정 {number}\n설치장소: 새 모의홀{i}\n주소: 부산광역시 해운대구')
        saved,_=ops.save(db,key,order_intake.extract,book.save);assert saved==number and len(book.rows)==before;counts['same_row_amendment']+=1
        command=f'주문 문구추가 {number}\n메시지ID: fragment-{i}\n보내는분: 모의협회{i} 회장'
        assist.command(db,owner,command);assist.command(db,owner,command);ops.save(db,key,order_intake.extract,book.save)
        assert book.rows[number][15]['stringValue'].count(f'모의협회{i} 회장')==1;counts['fragment_replay']+=1
        with db() as con:
            details=json.loads(con.execute('SELECT details FROM order_operations WHERE id=?',(key,)).fetchone()[0])
            assert sharing.attach_native_form(con,key,native_payload(number,details),'synthetic',5)['ready_for_final_share']
            jobs=con.execute("SELECT * FROM order_share_jobs WHERE order_id=? AND state='waiting'",(key,)).fetchall();assert len(jobs)==2
            for j,job in enumerate(jobs):sharing.record_verified_receipt(con,job['id'],'기획방',job['body_digest'],f'synthetic-{i}-{j}','2026-11-14T12:00:00+09:00')
        assert '카톡 공유: 기획방' in assist.command(db,owner,f'주문 상태 {number}');counts['both_sharing_receipts']+=1
        for _ in range(2):life.command(db,owner,f'추가 주문 {number}\n요청ID: additional-{i}')
        extra=c.execute("SELECT * FROM order_operations WHERE owner=? AND raw=''",(owner,)).fetchall();assert len(extra)==1
        ops.save(db,extra[0]['id'],order_intake.extract,book.save);assert len(book.rows)==before+1;counts['additional_replay']+=1
        life.command(db,owner,f'주문 취소 {number}')
        assert c.execute('SELECT COUNT(*) FROM order_transmissions WHERE order_id=?',(key,)).fetchone()[0]==0;counts['cancellation_invalidates_share']+=1
        try:ops.save(db,key,order_intake.extract,book.save)
        except ValueError:counts['cancelled_save_blocked']+=1
        else:raise AssertionError('Cancelled order saved')
    result={'synthetic_complete_flows':1000,'phase_checks':sum(counts.values()),'passed':counts,'external_calls':0,'live_side_effects':False,'sheet_transport':'in-memory; production Ledger.save logic','receipts':'synthetic test receipts, not actual Kakao delivery'}
    c.close()
    if len(sys.argv)>1:Path(sys.argv[1]).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
if __name__=='__main__':run()
