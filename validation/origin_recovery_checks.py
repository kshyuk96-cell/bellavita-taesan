"""Synthetic legacy quarantine and explicit restoration; no real state DB or network."""
import contextlib,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import order_operations as ops,order_origin,order_assistance as assist

def run():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;ops.setup(c);c.commit()
    @contextlib.contextmanager
    def db():
        with c:yield c
    counts={'legacy_quarantined':0,'wrong_owner_held':0,'explicit_manual_restored':0,'replay_no_extra_audit':0,'same_intake_id_preserved':0,'one_writer_call':0}
    for i in range(500):
        with db() as con:
            ops.stage(con,f'legacy{i}',f'staff{i}','kakao',f'주문 접수\n설치장소: 모의홀{i}\n보내는분: 모의회사{i}',{'venue':f'모의홀{i}','sender':f'모의회사{i}'},ops.now())
    assert order_origin.classify_untracked(db)==500
    for i in range(500):
        key=f'legacy{i}';owner=f'staff{i}'
        assert c.execute('SELECT state FROM order_operations WHERE id=?',(key,)).fetchone()[0]=='origin_hold';counts['legacy_quarantined']+=1
        command=f'접수 직접입력확인 {key}\n보내는분: 모의회사{i}\n확인근거: 직원이 원문을 현재 직접 확인함'
        assert '확인하지 못' in assist.command(db,'other',command);counts['wrong_owner_held']+=1
        assert '재개 요청' in assist.command(db,owner,command);counts['explicit_manual_restored']+=1
        prior=c.execute('SELECT COUNT(*) FROM order_changes WHERE order_id=?',(key,)).fetchone()[0]
        assert '이미 재개' in assist.command(db,owner,command)
        assert c.execute('SELECT COUNT(*) FROM order_changes WHERE order_id=?',(key,)).fetchone()[0]==prior;counts['replay_no_extra_audit']+=1
        raw=c.execute('SELECT raw FROM order_operations WHERE id=?',(key,)).fetchone()[0]
        again,_=ops.intake(db,owner,raw,trusted_manual=True);assert again==key;counts['same_intake_id_preserved']+=1
        writes=[]
        def writer(marker,data,created,update):writes.append(marker);return 10000+i,data
        ops.save(db,key,None,writer);ops.save(db,key,None,writer);assert writes==[key];counts['one_writer_call']+=1
    c.close();result={'synthetic_legacy_orders':500,'phase_checks':sum(counts.values()),'passed':counts,'external_calls':0,'live_state_database_changed':False}
    if len(sys.argv)>1:Path(sys.argv[1]).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
if __name__=='__main__':run()
