"""Reviewed batch-01 expectations: authored literals, never taken from worker reports."""
from taesan_core.hope_test_data import ITEMS,DATA_VERSION
from taesan_core.universal_qa import Scenario,Risk,Adapter,build_contract

EXPECTED={
    1: dict(environment='local_reference_fixture', operationalWrites=False, realCharges=False, realNotifications=False),
    2: dict(generation=1, matched=True),
    3: dict(distinct=True, own=200, cross=403),
    4: dict(generation=2, initial_state_matches=True, stale_status=409, history_retained=True),
    5: {uid: dict(grade=grade, status='new', card='unregistered') for uid, grade in [('U01', 'hope'), ('U02', 'happy'), ('U03', 'premium')]},
    6: dict(member_ids=['U04'], owner_count=10, product_keys=['products'], unauthorized=403, finance_seed=403),
    7: dict(DP=600000, BP=150000, SP=100000),
    8: {'U05': 0, 'U06': 1000000, 'U07': 999999, 'U07_BP': 100001},
    9: dict(principal=1200000, used=450000, BP=85000, DP=750000, independent_ceiling=750000),
    10: {'P01': ('domestic', 300000), 'P02': ('overseas', 1000000), 'P03': ('culture', 100000), 'P04': ('cruise', 1000000)},
    11: dict(last_seat=1, sold_out=0, rooms={'inside': 2, 'balcony': 1}),
    12: dict(deposit_gross=300000, balance_gross=700000, balance_due='2027-02-14', payment_mode='full_simulator_payment'),
    13: dict(unknown='reconciliation_required', replay='reconciliation_required_no_replay', before_cards=0, initial_cards=1, resolved_DP=50000, declined='declined', declined_DP=0),
    14: dict(bonus='bonus_order_policy_pending', corporate='corporate_policy_pending', suspension='suspension_withdrawal_policy_pending', unchanged=True),
    15: dict(status=409, unchanged=True, contract_matches=True),
    16: {'U01': dict(status='active', paid=75000, principal=50000, signup_fee=25000, BP=25000), 'U02': dict(status='active', paid=150000, principal=100000, signup_fee=50000, BP=50000), 'U03': dict(status='active', paid=300000, principal=200000, signup_fee=100000, BP=100000)},
    17: dict(plan=dict(DP=600000, BP=100000, SP=100000, card=200000), independent_match=True, stock=0, bookings=1, card_approvals=1, journal={'DP': -600000, 'BP': -100000, 'SP': -100000}),
    18: dict(balances=dict(DP=600000, BP=150000, SP=100000), stock=1, booking='cancelled', card_net=0, reversals=3, original_links=True, duplicate='approved_refund_required'),
    19: dict(ceiling=750000, free_bonus_cash=0, stopped=True, DP=0, BP=85000, returned=750000, status='withdrawn', returns=1, original_source='seed-principal-U09'),
    20: dict(customer_promoted=False, canonical_cases=38, word_created=True, excel_created=True),
}

ADAPTER='hope-local-batch01'
VERSION='reference-adapter-v1'

def scenarios():
    rows=[]
    for number,title in enumerate(ITEMS,1):
        priority='P0' if number in (1,3,6,7,8,9,13,14,15,16,17,18,19) else 'P1'
        rows.append(Scenario('batch01-'+str(number).zfill(2),ADAPTER,
            'local-fixture-workflow' if 16<=number<=19 else 'local-fixture-preparation',
            'batch01-'+str(number).zfill(2),
            Risk(priority,title+' 실패는 검증 신뢰·금액·권한에 영향',
                 '초기값·반복·실패 경계에서 발생 가능','원거래와 초기 상태를 대조하고 재시험 필요'),
            {'item':number,'data_version':DATA_VERSION},EXPECTED[number],
            '고객 문서 및 승인된 첫 20개 계획의 사전 고정 기대값',
            '각 승인 항목별 서로 다른 준비·업무·증거 상태'))
    return rows


def contract():
    return build_contract(scenarios(),[Adapter(ADAPTER,VERSION,lambda _:None)])
