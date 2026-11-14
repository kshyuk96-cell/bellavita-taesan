"""Synthetic identity collision, negation and quantity checks; no customer data/network."""
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from customer_memory import CustomerMemory
from customer_orders import allow_room,prepare
import order_intake

def run():
    counts={'bound_defaults':0,'identity_held':0,'negative_instructions_held':0,'quantity_held':0}
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'memory.db';m=CustomerMemory(path)
        for i in range(200):
            customer=f'c{i}';room=f'r{i}';sender=f's{i}'
            m.confirm_profile(customer,'동일 표시이름',f'모의조직 {i} 회장','synthetic')
            allow_room(m,room,'synthetic');m.bind(room,sender,customer,'synthetic')
            m.confirm_variant(f'v{i}',customer,f'모의모임 {i} 대표','모의모임','synthetic')
        for i in range(200):
            room=f'r{i}';sender=f's{i}'
            _,decision=prepare(f'고객 주문 {room} {sender}\nhttps://bojagicard.com/synthetic',path)
            assert decision.sender==f'모의조직 {i} 회장';counts['bound_defaults']+=1
            for other in range(1,12):
                try:prepare(f'고객 주문 {room} s{(i+other)%200}\nhttps://bojagicard.com/synthetic',path)
                except ValueError:counts['identity_held']+=1
                else:raise AssertionError('Cross-customer default')
            for negative in ('말고','아니고','제외','빼고','쓰지','넣지','하지','취소','이름 빼고','모임 제외'):
                assert m.resolve(room,sender,current_text='모의모임 '+negative).status=='review'
                counts['negative_instructions_held']+=1
            for quantity in ('2개','10','0','-1','미정','1 또는 2'):
                try:order_intake.extract('주문 접수\n설치장소: 모의홀\n보내는분: 모의회사\n수량: '+quantity)
                except ValueError:counts['quantity_held']+=1
                else:raise AssertionError('Unsupported quantity accepted')
        m.db.close()
    report={'synthetic_cases':sum(counts.values()),'passed':counts,'network_calls':0,'live_side_effects':False}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if len(sys.argv)>1:Path(sys.argv[1]).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':run()
