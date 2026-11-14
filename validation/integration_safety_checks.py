"""Synthetic shipping-clock, source-grant and manual-cancellation checks."""
import contextlib,json,sqlite3,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shipping_formula_proposal import model
from customer_memory import CustomerMemory
import customer_sources,order_operations as ops,order_lifecycle as life,order_assistance as assist

def run():
    counts={'valid_shipping_minutes':0,'invalid_clock_held':0,'multiple_clocks_held':0,'revoked_source_held':0,'reenabled_old_source_held':0,'billing_not_delivery':0,'manual_delivery_record_only':0,'confirmation_replay':0}
    for minute in range(1440):
        clock=f'{minute//60:02d}:{minute%60:02d}';assert model('2099-12-01','배송시각 '+clock)=='12.01 '+clock;counts['valid_shipping_minutes']+=1
    for i in range(1000):
        assert model('2099-12-01',f'배송시각 {24+i%60:02d}:{i%100:02d}')=='확인 필요';counts['invalid_clock_held']+=1
        assert model('2099-12-01',f'배송시각 10:{i%60:02d} / 배송시각 11:{i%60:02d}')=='확인 필요';counts['multiple_clocks_held']+=1
    with tempfile.TemporaryDirectory() as tmp:
        m=CustomerMemory(Path(tmp)/'memory.db')
        for i in range(250):
            room=f'room{i}';customer=f'customer{i}';m.confirm_profile(customer,'모의고객',f'모의회사{i}','synthetic');m.allow_room(room,'synthetic');m.bind(room,'sender',customer,'synthetic')
            before=customer_sources.context(m,room,'sender');m.disable_room(room,'synthetic')
            assert not customer_sources.valid(m,before);counts['revoked_source_held']+=1
            m.allow_room(room,'synthetic');m.bind(room,'sender',customer,'synthetic')
            assert not customer_sources.valid(m,before);counts['reenabled_old_source_held']+=1
        m.db.close()
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;ops.setup(c);c.commit()
    @contextlib.contextmanager
    def db():
        with c:yield c
    for i in range(250):
        key=f'order{i}';number=10000+i;owner=f'staff{i}'
        with db() as con:
            ops.stage(con,key,owner,'kakao','synthetic',{'sender':'모의회사'},ops.now());con.execute("UPDATE order_operations SET state='saved',order_number=? WHERE id=?",(number,key))
        life.command(db,owner,f'주문 취소 {number}')
        def confirmation(target,evidence):return f'주문 취소확인 {number}\n대상: {target}\n확인ID: synthetic-{i}\n근거: {evidence}'
        life.command(db,owner,confirmation('청구','청구 담당자에게 청구 없음 확인'))
        assert '배송 미확인' in assist.command(db,owner,f'주문 상태 {number}');counts['billing_not_delivery']+=1
        text=confirmation('배송','배송 담당자에게 중단 확인');life.command(db,owner,text)
        status=assist.command(db,owner,f'주문 상태 {number}');assert '배송 확인 기록 있음' in status and '공유 미확인' in status;counts['manual_delivery_record_only']+=1
        life.command(db,owner,text);assert c.execute('SELECT COUNT(*) FROM order_cancel_confirmations WHERE order_id=?',(key,)).fetchone()[0]==2;counts['confirmation_replay']+=1
    c.close();result={'synthetic_checks':sum(counts.values()),'passed':counts,'external_calls':0,'live_side_effects':False,'formula_validation':'Python reference behavior; not Google Sheets engine execution','cancellation_records':'Synthetic staff confirmations, not real-world cancellations'}
    if len(sys.argv)>1:Path(sys.argv[1]).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
if __name__=='__main__':run()
