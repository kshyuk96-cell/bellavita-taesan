"""Safe initial adapters: existing commerce contracts and isolated agent/formula fixtures.

The latter two are demonstration policies, not approved operational business rules.
Oracle functions never call adapter/SUT functions. Mutants are explicit trusted code.
"""
from decimal import Decimal, ROUND_HALF_UP
import itertools
import random
from .commerce import Order, check_fields
from .core import CoreError, Principal
from .universal_qa import Adapter, Risk, Scenario

RISK = {
    'P0': Risk('P0', '다른 회사 정보 노출 가능', '권한 경계마다 반복 입력 가능', '노출 후 회수 불가; 즉시 차단'),
    'P1': Risk('P1', '잘못된 주문/중복 실행/금액 발생', '경계값과 재시도에서 발생 가능', '수동 확인과 상태 복원 필요'),
    'P2': Risk('P2', '일부 정상 요청 거절로 업무 지연', '드문 조합 입력에서 발생', '다시 입력하여 복구 가능'),
    'P3': Risk('P3', '표시/계산 진단의 낮은 영향', '합성 진단 조건에 한정', '진단 재실행으로 복구 가능'),
}


def order_adapter(data, mutant=None):
    fields = data['fields']
    if mutant == 'allow_zero_quantity' and fields.get('quantity') == 0:
        fields = dict(fields, quantity=1)
    try:
        check_fields(fields)
        actual = 'accepted'
    except CoreError:
        actual = 'rejected'
    return {'actual': actual, 'evidence': {'contract': 'commerce.check_fields', 'fixture': True}}


def scope_adapter(data, mutant=None):
    principal = Principal(data['principal_tenant'], 'synthetic-user', frozenset({data['role']}))
    order = Order(data['order_tenant'], 'fixture', 'synthetic-order', 'v1', {'amount': data['amount']})
    try:
        if mutant != 'skip_tenant':
            order.authorize(principal)
        actual = 'authorized'
    except CoreError:
        actual = 'denied'
    return {'actual': actual, 'evidence': {'contract': 'Order.authorize', 'fixture': True}}


def control_adapter(data, mutant=None):
    """Pure simulated task lifecycle, with no process, filesystem, queue or network."""
    state, effects = data['initial'], 0
    for action in data['actions']:
        role = data['role']
        if action == 'approve' and role == 'admin' and state == 'proposed':
            state = 'approved'
        elif action == 'execute' and role in {'writer', 'admin'} and (state == 'approved' or
                mutant == 'replay_unknown' and state == 'unknown'):
            state, effects = 'running', effects + 1
        elif action == 'timeout' and state == 'running':
            state = 'unknown'
        elif action == 'verify' and role == 'admin' and state in {'running', 'unknown'}:
            state = 'done'
        elif action == 'cancel' and role == 'admin' and state in {'proposed', 'approved'}:
            state = 'cancelled'
        # retry never clears uncertain execution: reconciliation is required.
    return {'actual': {'state': state, 'effects': effects},
            'evidence': {'contract': 'synthetic-control-policy/v1', 'fixture': True,
                         'steps_observed': len(data['actions'])}}


def formula_adapter(data, mutant=None):
    """Demonstration quote in KRW integer won, no billing connector."""
    value = Decimal(data['unit_won']) * data['quantity'] * (10000-data['discount_bps']) / 10000
    if mutant == 'truncate_won':
        actual = int(value)
    else:
        actual = int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return {'actual': actual, 'evidence': {'contract': 'synthetic-quote/v1', 'fixture': True}}


# Independent finite transition specification, intentionally declarative rather than SUT branches.
_TRANSITIONS = {
    ('admin', 'proposed', 'approve'): ('approved', 0),
    ('writer', 'approved', 'execute'): ('running', 1),
    ('admin', 'approved', 'execute'): ('running', 1),
    **{(role, 'running', 'timeout'): ('unknown', 0) for role in ('reader', 'writer', 'admin')},
    ('admin', 'running', 'verify'): ('done', 0),
    ('admin', 'unknown', 'verify'): ('done', 0),
    ('admin', 'proposed', 'cancel'): ('cancelled', 0),
    ('admin', 'approved', 'cancel'): ('cancelled', 0),
}


def _control_oracle(data):
    state, effects = data['initial'], 0
    for action in data['actions']:
        state, increment = _TRANSITIONS.get((data['role'], state, action), (state, 0))
        effects += increment
    return {'state': state, 'effects': effects}


def build_suite(seed=20261116, per_group=1000):
    """Seeded distinct states, boundary Cartesian products and failure sequences.

    per_group=1000 produces 3006 scenarios. Repeats are stateful sequence actions,
    not duplicated test IDs. Dedup uses full semantic input, not synthetic user IDs.
    """
    if type(per_group) is not int or not 4 <= per_group <= 1152:
        raise CoreError('per_group must be between 4 and 1152')
    rng = random.Random(seed)
    scenarios = []
    def add(adapter, index, data, expected, priority, key, oracle, rationale, formula=None):
        scenarios.append(Scenario(f'{adapter}-{index:05d}', adapter, adapter, key,
                                  RISK[priority], data, expected, oracle, rationale, formula))
    # Numeric type/range and text length oracle is specified independently of check_fields.
    candidates = list(itertools.product([-1, 0, 1, 2, 10**12, 10**12+1, True, 1.5],
                                       [-1, 0, 1, 500, 10**12, 10**12+1, False, 0.5],
                                       [0, 1, 2, 1999, 2000, 2001], ['address', 'message', 'buyer_name']))
    rng.shuffle(candidates)
    for i, (q, a, length, field) in enumerate(candidates[:per_group]):
        fields = {'quantity': q, 'amount': a, field: '가'*length}
        valid = type(q) is int and 1 <= q <= 10**12 and type(a) is int and 0 <= a <= 10**12 and 1 <= length <= 2000
        key = 'order.valid' if valid else 'order.boundary-rejection'
        add('order', i, {'fields': fields}, 'accepted' if valid else 'rejected',
            'P2' if valid else 'P1', key, 'integer type/range and text length specification',
            'quantity/amount type and boundary × independent field/text length; identical states omitted')
    for i in range(6):
        role = ('reader','writer','admin')[i % 3]
        same = i % 2 == 0
        data = {'principal_tenant': 'company-a', 'order_tenant': 'company-a' if same else 'company-b',
                'role': role, 'amount': 0}
        add('scope', i, data, 'authorized' if same else 'denied', 'P0' if not same else 'P2',
            'scope.same-tenant' if same else 'scope.cross-tenant', 'tenant equality specification',
            'six role × tenant boundary equivalence classes; inert amount variation omitted')
    # Mandatory sequences guarantee mutation-sensitive timeout/replay and successful completion paths.
    mandatory = [('admin','proposed',('approve','execute','timeout','retry','execute')),
                 ('admin','proposed',('approve','execute','verify','execute')),
                 ('reader','approved',('execute','execute')), ('admin','proposed',('cancel','approve','execute'))]
    states = set(mandatory)
    actions = ('approve','execute','timeout','retry','cancel','verify')
    while len(states) < per_group:
        states.add((rng.choice(('reader','writer','admin')), rng.choice(('proposed','approved','running','unknown')),
                    tuple(rng.choice(actions) for _ in range(rng.randint(3,9)))))
    for i, (role, initial, sequence) in enumerate(sorted(states)[:per_group]):
        data = {'role': role, 'initial': initial, 'actions': list(sequence)}
        key = 'control.uncertain-retry' if 'timeout' in sequence or initial == 'unknown' else 'control.roles-sequences'
        add('control', i, data, _control_oracle(data), 'P1', key,
            'independent declarative finite transition table/v1',
            'role × initial state × ordered actions; retry after uncertainty cannot repeat effects')
    states = {(1,1,5000), (3,1,5000), (0,1,0), (10**9,100,9999)}
    while len(states) < per_group:
        states.add((rng.randint(0,10**9), rng.choice((1,2,3,10,100)), rng.choice((0,1,3333,5000,9999,10000))))
    for i, (unit, quantity, discount) in enumerate(sorted(states)):
        data = {'unit_won': unit, 'quantity': quantity, 'discount_bps': discount}
        numerator = unit * quantity * (10000-discount)
        expected = (numerator + 5000) // 10000  # independent exact integer oracle
        formula = {'id': 'synthetic-quote-v1', 'expression': 'round_half_up(unit_won * quantity * (10000-discount_bps)/10000)',
                   'variables': data, 'units': {'unit_won':'KRW/item','quantity':'item','discount_bps':'basis-point','result':'KRW'},
                   'rounding': 'nonnegative; nearest integer won; ties upward; once at final total', 'expected': expected}
        add('formula', i, data, expected, 'P3' if discount == 0 else 'P1', 'formula.rounding',
            'exact integer numerator with +5000 then floor divide 10000',
            'zero/max discount, tie, large amount and quantity; integer won output', formula)
    return scenarios


def adapters(mutant=None):
    return [Adapter(name, 'v1' if mutant is None else 'mutant-'+mutant,
                    lambda data, fn=fn: fn(data, mutant)) for name, fn in
            [('order', order_adapter), ('scope', scope_adapter), ('control', control_adapter), ('formula', formula_adapter)]]

REQUIRED_COVERAGE = ['order.valid','order.boundary-rejection','scope.same-tenant','scope.cross-tenant',
                     'control.uncertain-retry','control.roles-sequences','formula.rounding']
