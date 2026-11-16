"""Customer-derived Hope Tour QA contract; independent oracles, never business writes."""
from decimal import Decimal, ROUND_FLOOR
import random
from .universal_qa import Adapter, Risk, Scenario, EvidenceUnavailable

REFERENCE_SHA256='eda84f0a643fd4feaa5e287cc2065e98304c70478291af37267cff2e00c8a9d0'
POLICY_VERSION='hope-customer-20261116-v2'
FORMAT_SHA256='d3de4f429c4c080056d3eec2a14380827d3af0b18b16b9a0107572e33e0437a0'
BP_LIMITS={'hope':{'domestic':1500,'overseas':1000,'culture':5000},
 'happy':{'domestic':2000,'overseas':1500,'culture':7000},
 'premium':{'domestic':2500,'overseas':1800,'culture':10000}}
INITIAL={'hope':(50000,25000),'happy':(100000,50000),'premium':(200000,100000)}
REQUIREMENTS = {
 'T01':('가입·첫 납부','P0',['FB-01','FB-02','FB-03','FB-04','FB-05']),
 'T02':('정기청구·중복·불확실 응답','P0',['FB-07','FB-09','FB-10']),
 'T03':('상품 등록·다른 기기 조회','P0',['FB-21','FB-22','FB-23','FB-79']),
 'T04':('상품 신청·가격·재고·확정','P0',['FB-24','FB-25','FB-26','FB-27','FB-28']),
 'T05':('본인 납입원금 우선 사용','P0',['FB-13','FB-14']),
 'T06':('BP 한도·미확정 우선순위','P0',['FB-15','FB-49','FB-61']),
 'T07':('취소·원수단 복원·원장 일치','P0',['FB-19','FB-29','FB-80']),
 'T08':('탈퇴·정지·원금 반환 한도','P0',['FB-20','FB-33','FB-39','FB-40']),
 'T09':('권한·감사·MFA·개인정보','P0',['FB-44','FB-53','FB-55','FB-56','FB-60']),
 'T10':('실제 PG·실기기·복구','P0',['FB-65','FB-69','FB-74','FB-75','FB-76','FB-77']),
 'TERM':('전체 용어·도움말','P1',['FB-33','FB-61'])}
GLOSSARY={'DP':'본인 납입금 잔액','BP':'추가 보너스포인트','SP':'특별포인트','CP':'법인 지원포인트',
 'PG':'전자결제대행사','API':'시스템 연동 기능','CRM':'고객관계관리','QA':'품질검증','KPI':'핵심성과지표'}


def integer(value):
    if type(value) is not int or not 0<=value<=10**15:
        raise ValueError('nonnegative bounded integer money/points required')
    return value


def allocation_oracle(values):
    """Document section 7 example policy; BP/SP ordering needs separate approval."""
    price,dp,bp,sp,limit=(integer(values[k]) for k in ('price','DP','BP','SP','bp_limit_bps'))
    if limit>10000:raise ValueError('basis points outside 0..10000')
    if values.get('bonus_order') not in ('BP_SP','SP_BP'):
        raise EvidenceUnavailable('bonus_order_not_approved')
    used={'DP':min(price,dp),'BP':0,'SP':0,'card':0}
    remainder=price-used['DP']
    for kind in values['bonus_order'].split('_'):
        available={'BP':min(bp,(price*limit)//10000),'SP':sp}[kind]
        used[kind]=min(remainder,available);remainder-=used[kind]
    used['card']=remainder
    return used


def refund_oracle(values):
    """Section 8: bonuses and signup fee must not be added to refundable principal."""
    principal,used,returned,deductions=(integer(values[k]) for k in
                                     ('principal','used','returned','deductions'))
    # Validate distractor buckets even though they cannot enlarge the ceiling.
    for key in ('BP','SP','signup_fee'):integer(values.get(key,0))
    return max(0,principal-used-returned-deductions)


def risk(priority):
    return Risk(priority,'납입 원금·환불·접근권한 또는 핵심 고객 흐름 영향',
                '한도·중복·실패·역할 경계에서 발생 가능','원거래 대조와 관리자 승인 필요')


def formula_suite(seed=20261116,count=1024):
    rng=random.Random(seed);scenarios=[];seen=set()
    allocation=[dict(price=1_000_000,DP=600_000,BP=150_000,SP=100_000,bp_limit_bps=1000,bonus_order='BP_SP')]
    while len(allocation)<count:
        price=rng.choice([0,1,9,10,99,100,999,10000,1000000,rng.randrange(10**8)])
        value=dict(price=price,DP=rng.choice([0,1,price//2,price,price+1]),
            BP=rng.choice([0,1,price//10,price,price+1]),SP=rng.choice([0,1,price//2,price]),
            bp_limit_bps=rng.choice([0,1,999,1000,1001,10000]),bonus_order=rng.choice(['BP_SP','SP_BP']))
        key=tuple(value.items())
        if key not in seen:seen.add(key);allocation.append(value)
    for index,value in enumerate(allocation):
        expected=allocation_oracle(value)
        scenarios.append(Scenario('allocation-'+str(index),'hope-allocation','T05','principal-first',risk('P0'),
            value,expected,'문서 §7: 정수 상품가 기준 BP 한도, 원금 먼저; BP/SP 순서는 예제 매개변수',
            '상품가·원금·보너스·한도·순서의 의미 상태 조합',dict(id='principal-first-v1',
                expression='DP=min(price,DP); cap=floor(price*bp_limit_bps/10000); allocate remaining by explicit example order',
                variables=value,units={'price':'KRW','DP':'KRW','BP':'P','SP':'P','bp_limit_bps':'basis_points'},
                rounding='BP cap: floor to integer point; monetary values: integer KRW',expected=expected)))
    seen=set();refunds=[dict(principal=1200000,used=450000,returned=0,deductions=0,BP=85000,SP=0,signup_fee=25000)]
    while len(refunds)<count:
        p=rng.choice([0,1,50000,1200000,rng.randrange(10**8)])
        value=dict(principal=p,used=rng.choice([0,p//2,p,p+1]),returned=rng.choice([0,1,p//3,p]),
            deductions=rng.choice([0,1,p//4]),BP=rng.choice([0,85000,p*2]),SP=rng.choice([0,100000]),signup_fee=25000)
        key=tuple(value.items())
        if key not in seen:seen.add(key);refunds.append(value)
    for index,value in enumerate(refunds):
        expected=refund_oracle(value)
        scenarios.append(Scenario('refund-'+str(index),'hope-refund','T08','principal-refund-ceiling',risk('P0'),
            value,expected,'문서 §8 원금 상한식; BP/SP/가입비는 상한에 추가하지 않음',
            '원금·사용·기반환·확정 차감의 경계와 무상 포인트 구분',dict(id='principal-refund-v1',
                expression='max(0,principal-used-returned-deductions)',variables=value,
                units={k:('P' if k in ('BP','SP') else 'KRW') for k in value},
                rounding='integer KRW; no rounding',expected=expected)))
    for grade,types in BP_LIMITS.items():
        for category,bps in types.items():
            value={'grade':grade,'category':category,'price':1000000}
            expected=1000000*bps//10000
            scenarios.append(Scenario('cap-'+grade+'-'+category,'hope-bp-limit','T06','grade-category-cap',risk('P0'),
                value,expected,'최종 양식 §4의 3등급 × 3상품유형 BP 상한표',
                '등급과 상품 유형에 따른 아홉 개 독립 정책 행',dict(id='grade-category-cap-v1',
                    expression='floor(price*approved_grade_category_bps/10000)',variables={**value,'bps':bps},
                    units={'price':'KRW','bps':'basis_points'},rounding='floor integer point',expected=expected)))
    for grade,(monthly,fee) in INITIAL.items():
        value={'grade':grade}
        expected=dict(total=monthly+fee,principal=monthly,signup_fee=fee,BP=fee)
        scenarios.append(Scenario('initial-'+grade,'hope-initial','T01','grade-initial-charge',risk('P0'),
            value,expected,'최종 양식 §4: 등급별 월회비+가입비, 반환 원금과 BP 분리',
            '세 등급의 서로 다른 금액 계약',dict(id='initial-charge-v1',expression='total=monthly+fee;principal=monthly;BP=fee',
                variables={'grade':grade,'monthly':monthly,'fee':fee},units={'monthly':'KRW','fee':'KRW','BP':'P'},
                rounding='integer KRW',expected=expected)))
    return scenarios


def fixture_adapters(mutation=None):
    """Independent Decimal fixture to test the test system; never a service adapter."""
    def allocate(v):
        remaining=Decimal(v['price']);out={k:0 for k in ('DP','BP','SP','card')}
        order=['DP']+v['bonus_order'].split('_')
        if mutation=='bonus_first':order=v['bonus_order'].split('_')+['DP']
        for kind in order:
            amount=Decimal(v[kind])
            if kind=='BP':
                cap=(Decimal(v['price'])*Decimal(v['bp_limit_bps'])/Decimal(10000)).to_integral_value(rounding=ROUND_FLOOR)
                if mutation!='ignore_bp_cap':amount=min(amount,cap)
            out[kind]=int(min(remaining,amount));remaining-=out[kind]
        out['card']=int(remaining)
        return dict(actual=out,evidence={'scope':'independent_decimal_fixture_not_customer_service'})
    def refund(v):
        result=Decimal(v['principal'])-Decimal(v['used'])-Decimal(v['returned'])-Decimal(v['deductions'])
        if mutation=='refund_bonus':result+=v['BP']+v['SP']
        if mutation=='refund_signup_fee':result+=v['signup_fee']
        return dict(actual=int(max(Decimal(0),result)),evidence={'scope':'independent_decimal_fixture_not_customer_service'})
    def bp_limit(v):
        rows={'hope':(15,10,50),'happy':(20,15,70),'premium':(25,18,100)}
        percentage=rows[v['grade']][('domestic','overseas','culture').index(v['category'])]
        return dict(actual=int(Decimal(v['price'])*percentage//100),evidence={'scope':'reference_fixture'})
    def initial(v):
        monthly={'hope':50000,'happy':100000,'premium':200000}[v['grade']]
        fee=monthly//2
        return dict(actual=dict(total=monthly+fee,principal=monthly,signup_fee=fee,BP=fee),evidence={'scope':'reference_fixture'})
    return [Adapter('hope-bp-limit','reference-fixture-v1',bp_limit),Adapter('hope-initial','reference-fixture-v1',initial),Adapter('hope-allocation','reference-fixture-v1',allocate),Adapter('hope-refund','reference-fixture-v1',refund)]
