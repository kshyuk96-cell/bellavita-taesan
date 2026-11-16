"""Versioned, synthetic-only Hope Tour batch-01 data and approval contract."""
from copy import deepcopy
from dataclasses import dataclass, asdict
from .universal_qa import fingerprint

DATA_VERSION='hope-fixtures-20261116-v1'
ROLES=('member','product_admin','finance','owner','unauthorized')
ITEMS=[
 '검증 환경 식별·운영 실행 차단','생성·초기화 인터페이스','실행별 데이터 공간 분리','초기화 정합성',
 '3등급 신규 회원','회원·상품관리자·재무·최고관리자 권한','U04 혼합 잔액','0·정확·1원 부족 경계',
 'U09 반환 상한','국내·해외·문화·크루즈 상품','마지막 1석·매진·객실 재고','예약금·잔금·납기',
 '승인·거절·불명 시뮬레이터','미확정 정책 차단','사전 계약·데이터 대조','3등급 가입 통합',
 '원금 우선 결제 통합','취소·원수단·재고 복원 통합','탈퇴·청구중단·반환 통합','증거·결함·고객 보고서 연결']


@dataclass(frozen=True)
class FixtureContract:
    source_revision: str
    seed: int = 20261116
    example_bonus_order: str | None = None
    cancellation_fee_won: int | None = None
    scope: str = 'local_reference_fixture'
    max_commands: int = 200

    def __post_init__(self):
        import re
        if type(self.source_revision) is not str or not re.fullmatch('[a-f0-9]{40}',self.source_revision):
            raise ValueError('exact source required')
        if (self.scope!='local_reference_fixture' or type(self.seed) is not int or not 0<=self.seed<2**32 or
                self.example_bonus_order not in (None,'BP_SP','SP_BP') or
                self.cancellation_fee_won not in (None,0) or (self.cancellation_fee_won is not None and type(self.cancellation_fee_won) is not int) or
                type(self.max_commands) is not int or not 1<=self.max_commands<=1000):
            raise ValueError('bounded synthetic example contract only')

    @property
    def manifest(self):
        return dict(schema='hope-fixture-contract/v1',data_version=DATA_VERSION,
                    **asdict(self),operational_policy_approved=False,units={'DP':'KRW','BP':'P','SP':'P','card':'KRW'},
                    point_exchange='synthetic_example_1P_equals_1KRW')

    @property
    def digest(self):return fingerprint(self.manifest)


def fixture_data(contract):
    """Desired initial state, separate from the application/ledger implementation."""
    if not isinstance(contract,FixtureContract):raise ValueError('reviewed fixture contract required')
    members={}
    for identifier,grade in [('U01','hope'),('U02','happy'),('U03','premium')]:
        members[identifier]=dict(grade=grade,status='new',principal=0,used=0,returned=0,deductions=0,
                                DP=0,BP=0,SP=0,card='unregistered',recurring=False)
    for identifier,dp,bp,sp in [('U04',600000,150000,100000),('U05',0,0,0),
                               ('U06',1000000,150000,100000),('U07',999999,100001,0),('U10',0,0,0)]:
        members[identifier]=dict(grade='hope',status='active',principal=dp,used=0,returned=0,deductions=0,
                                DP=dp,BP=bp,SP=sp,card='success',recurring=True)
    members['U08']=dict(grade='hope',status='dormant',principal=50000,used=0,returned=0,deductions=0,
                       DP=50000,BP=0,SP=0,card='decline',recurring=False)
    members['U09']=dict(grade='hope',status='active',principal=1200000,used=450000,returned=0,deductions=0,
                       DP=750000,BP=85000,SP=0,card='success',recurring=True)
    products={
      'P01':dict(category='domestic',price=300000,stock=10,rooms={'standard':10},deposit_bps=3000,due='2027-02-14'),
      'P02':dict(category='overseas',price=1000000,stock=1,rooms={'standard':1},deposit_bps=3000,due='2027-02-14'),
      'P03':dict(category='culture',price=100000,stock=0,rooms={'standard':0},deposit_bps=10000,due='2027-02-14'),
      'P04':dict(category='overseas',kind='cruise',price=1000000,room_prices={'inside':1000000,'balcony':1200000},stock=3,rooms={'inside':2,'balcony':1},deposit_bps=3000,due='2027-02-14')}
    opening_journal=[];opening_cards=[]
    for uid,member in members.items():
        member['principal_origin']='seed-principal-'+uid
        member['bonus_origin']='seed-bonus-'+uid
        member['special_origin']='seed-special-'+uid
        for bucket,amount in [('DP',member['principal']),('BP',member['BP']),('SP',member['SP'])]:
            if amount:
                ref=member[{'DP':'principal_origin','BP':'bonus_origin','SP':'special_origin'}[bucket]]
                opening_journal.append(dict(id='seed-'+uid+'-'+bucket,member=uid,bucket=bucket,
                    amount=amount,reference=ref,origin=ref,kind='fixture_opening',reverses=None))
        if member['used']:
            opening_journal.append(dict(id='seed-used-'+uid,member=uid,bucket='DP',amount=-member['used'],
                reference='fixture-historic-use-'+uid,origin=member['principal_origin'],kind='fixture_historic_use',reverses=None))
        if member['principal']:
            opening_cards.append(dict(id='seed-card-'+uid,member=uid,kind='fixture_opening',amount=member['principal'],
                reference=member['principal_origin'],status='synthetic_opening'))
    return deepcopy(dict(schema='hope-fixture-data/v1',version=DATA_VERSION,seed=contract.seed,
        clock='2026-11-16T09:00:00+09:00',members=members,products=products,roles=list(ROLES),card_scenarios=['success','decline','unknown'],
        bookings={},refunds={},withdrawals={},journal=opening_journal,cards=opening_cards,pending={}))


def assert_initial_state(state,contract):
    """Strict full comparison: no ignored balance/inventory fields or missing rows."""
    expected=fixture_data(contract)
    if fingerprint(state)!=fingerprint(expected):raise ValueError('fixture state/contract mismatch')
    return dict(matched=True,state_digest=fingerprint(state),contract_digest=contract.digest)
