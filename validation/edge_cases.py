"""Independent synthetic edge cases. Does not use or publish customer text."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import order_intake,order_ledger
from delivery_regions import delivery_alert

# Static expected jurisdictions, independent of implementation aliases/gazetteer.
REGIONS=[('서울특별시 강남구','서울','local'),('경기도 광주시','경기','local'),('인천광역시 서구','인천','local'),('부산광역시 해운대구','부산','nonlocal'),('대구광역시 중구','대구','nonlocal'),('광주광역시 북구','광주','nonlocal'),('대전광역시 서구','대전','nonlocal'),('울산광역시 남구','울산','nonlocal'),('세종특별자치시','세종','nonlocal'),('강원특별자치도 춘천시','강원','nonlocal'),('충청북도 청주시','충북','nonlocal'),('충청남도 천안시','충남','nonlocal'),('전북특별자치도 전주시','전북','nonlocal'),('전라남도 순천시','전남','nonlocal'),('경상북도 청도군','경북','nonlocal'),('경상남도 김해시','경남','nonlocal'),('제주특별자치도 제주시','제주','nonlocal')]

def run():
    counters={'regional_cases':0,'regional_passed':0,'ambiguous_cases':0,'ambiguous_held':0,'invalid_shipping_cases':0,'invalid_shipping_held':0}
    errors=[]
    for prefix,region,status in REGIONS:
        for number in range(1,101):
            for layout in ['{}','가상 예식장 ({})','가상 홀\n{}']:
                address=layout.format(prefix+' 검증로 '+str(number))
                result=delivery_alert(address,scope='outside_capital');counters['regional_cases']+=1
                if (result['region'],result['status'])==(region,status):counters['regional_passed']+=1
                else:errors.append({'kind':'region','expected_region':region,'layout':layout})
    for number in range(1,501):
        for address in ['광주시 검증로 '+str(number),'고성군 검증로 '+str(number),'가상호텔 '+str(number),'경기도 수원시 / 부산광역시 해운대구 '+str(number)]:
            counters['ambiguous_cases']+=1
            if delivery_alert(address)['status']=='unknown':counters['ambiguous_held']+=1
            else:errors.append({'kind':'ambiguous_region'})
    for number in range(1,1001):
        details=order_intake.extract(f'주문 접수\n설치장소: 부산광역시 해운대구 검증로 {number}\n보내는분: 가상 회사\n배송일: 2026-99-{number%28+1:02d}\n배송시간: 25:{number%60:02d}')
        saved=order_ledger.details_for_sheet(details,[{}]*29,[])
        counters['invalid_shipping_cases']+=1
        if not saved['delivery_date'] and not saved['delivery_time'] and '배송일' in saved['notes']:counters['invalid_shipping_held']+=1
        else:errors.append({'kind':'invalid_shipping'})
    report={'synthetic_edge_cases':counters,'errors':errors[:30],'error_count':len(errors),'live_side_effects':False}
    print(json.dumps(report,ensure_ascii=False,indent=2));return report

if __name__=='__main__':
    report=run()
    if len(sys.argv)>1:Path(sys.argv[1]).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(bool(report['error_count']))
