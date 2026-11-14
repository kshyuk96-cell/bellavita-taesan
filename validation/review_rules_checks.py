"""Synthetic location, multiple-order and sender-choice validation; no external calls."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import order_review_rules as rules

def run():
    counts={'partial_location_held':0,'paired_location_allowed':0,'explicit_orders_isolated':0,'all_titles_preserved':0,'single_title_selected':0,'ambiguous_choice_held':0}
    for i in range(1000):
        before={'venue':f'모의홀{i}','address':'서울특별시 강남구'}
        try:rules.check_location_change(before,{'venue':f'다른홀{i}'})
        except ValueError:counts['partial_location_held']+=1
        else:raise AssertionError('Stale address retained')
        rules.check_location_change(before,{'venue':f'다른홀{i}','address':'부산광역시 해운대구'});counts['paired_location_allowed']+=1
        text=f'[주문1]\n보내는분: 모의A{i}\n[주문2]\n보내는분: 모의B{i}'
        parts=rules.split_orders(text);assert len(parts)==2 and f'모의B{i}' not in parts[0] and f'모의A{i}' not in parts[1];counts['explicit_orders_isolated']+=1
        titles=f'모의협회{i} 회장\n모의회사{i} 대표'
        assert rules.sender_text('[모두 인쇄]\n'+titles)==titles;counts['all_titles_preserved']+=1
        assert rules.sender_text('[선택: 2]\n'+titles)==f'모의회사{i} 대표';counts['single_title_selected']+=1
        try:rules.sender_text(f'모의협회{i} 회장 또는 모의회사{i} 대표')
        except ValueError:counts['ambiguous_choice_held']+=1
        else:raise AssertionError('Ambiguous title printed')
    result={'synthetic_checks':sum(counts.values()),'passed':counts,'external_calls':0,'live_side_effects':False}
    if len(sys.argv)>1:Path(sys.argv[1]).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
if __name__=='__main__':run()
