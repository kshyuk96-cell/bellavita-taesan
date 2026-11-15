"""Read-only request decisions and durable, host-verified status observations.

No callback in this module performs a payment, refund, message, or order mutation.
Webhook state is an integration projection, not an operational order ledger. The
host verifier must check signature, replay window and provider/account identity,
then return a current Principal restricted to the configured tenant. The connector
must supply a trustworthy monotonically increasing sequence for each order
creation generation; timestamps or delivery order are not substitute sequences.
An unknown/conflicting observation places a durable review fence. Later partial
events do not clear it; the host must reconcile against an authoritative full
provider snapshot outside this module before creating a fresh projection.
"""
from dataclasses import dataclass, field
import json
from .commerce import ORDER_FIELDS, Order, check_fields
from .core import AccessDenied, CoreError, Principal, Store, digest, encoded, ident

DIMENSIONS = {
    'payment': ('unknown', 'unpaid', 'paid', 'refunded'),
    'production': ('unknown', 'draft', 'ready', 'in_progress', 'done'),
    'fulfillment': ('unknown', 'pending', 'shipped', 'delivered'),
    'cancellation': ('unknown', 'active', 'cancelled'),
}


def _scope(principal, tenant):
    if not isinstance(principal, Principal) or principal.tenant != tenant:
        raise AccessDenied('company scope mismatch')


def _state(value, *, partial=False):
    if type(value) is not dict or not value.keys() <= DIMENSIONS.keys():
        raise CoreError('unsupported status dimensions')
    if not partial and value.keys() != DIMENSIONS.keys():
        raise CoreError('all status dimensions required')
    if any(type(v) is not str or v not in DIMENSIONS[k] for k, v in value.items()):
        raise CoreError('unsupported canonical status')
    return dict(value)


@dataclass(frozen=True)
class StatusObservation:
    state: dict
    reasons: tuple = ()
    binding: tuple | None = None

    def __post_init__(self):
        _state(self.state, partial=True)
        if type(self.reasons) is not tuple or not all(type(v) is str for v in self.reasons):
            raise CoreError('invalid status reasons')
        if self.binding is not None:
            if type(self.binding) is not tuple or len(self.binding) != 5:
                raise CoreError('invalid status binding')
            for value in self.binding:
                ident(value)
        object.__setattr__(self, 'state', dict(self.state))

    @property
    def status(self):
        return 'review' if self.reasons else 'ok'


@dataclass(frozen=True)
class StatusMap:
    tenant: str
    source: str
    mapping: dict
    # Explicit host policy; no unverified inference that shipment means payment.
    conflicts: tuple = ()
    enabled: bool = False

    def __post_init__(self):
        ident(self.tenant)
        ident(self.source)
        if type(self.mapping) is not dict or not self.mapping.keys() <= DIMENSIONS.keys():
            raise CoreError('invalid status mapping')
        copied = {}
        for dimension, rows in self.mapping.items():
            if type(rows) is not dict or any(type(k) is not str or not k or len(k) > 100 or
                                           type(v) is not str or v not in DIMENSIONS[dimension]
                                           for k, v in rows.items()):
                raise CoreError('invalid status mapping value')
            copied[dimension] = dict(rows)
        if type(self.conflicts) is not tuple:
            raise CoreError('invalid conflict policy')
        conflicts = []
        for conflict in self.conflicts:
            if type(conflict) is not tuple or len(conflict) < 2:
                raise CoreError('conflict requires explicit status pairs')
            item = dict(conflict)
            if len(item) != len(conflict):
                raise CoreError('duplicate conflict dimension')
            _state(item, partial=True)
            conflicts.append(tuple(item.items()))
        if type(self.enabled) is not bool:
            raise CoreError('invalid mapping enabled flag')
        object.__setattr__(self, 'mapping', copied)
        object.__setattr__(self, 'conflicts', tuple(conflicts))

    def review(self, canonical):
        _state(canonical, partial=True)
        reasons = []
        for i, conflict in enumerate(self.conflicts):
            if all(canonical.get(k) == v for k, v in conflict):
                reasons.append(f'conflict-{i + 1}')
        return tuple(reasons)

    def translate(self, principal, external_statuses, *, order=None):
        _scope(principal, self.tenant)
        binding = None
        if order is not None:
            if not isinstance(order, Order):
                raise CoreError('invalid source order')
            order.authorize(principal)
            if order.source != self.source:
                raise AccessDenied('order source does not match mapping')
            if order.generation is not None:
                binding = (order.tenant, order.source, order.external_id, order.version, order.generation)
        if not self.enabled:
            return StatusObservation({}, ('status-map-not-connected',), binding)
        if type(external_statuses) is not dict or not external_statuses or not external_statuses.keys() <= DIMENSIONS.keys():
            raise CoreError('invalid external status envelope')
        result, reasons = {}, []
        for dimension, raw in external_statuses.items():
            if type(raw) is not str or not raw or len(raw) > 100:
                raise CoreError('invalid external status')
            canonical = self.mapping.get(dimension, {}).get(raw)
            if canonical is None or canonical == 'unknown':
                reasons.append(f'unmapped-{dimension}')
            else:
                result[dimension] = canonical
        reasons.extend(self.review(result))
        return StatusObservation(result, tuple(reasons), binding)


@dataclass(frozen=True)
class RequestPolicy:
    tenant: str
    allowed_change_fields: frozenset = frozenset()
    change_stages: frozenset = frozenset()
    cancel_stages: frozenset = frozenset()
    enabled: bool = False

    def __post_init__(self):
        ident(self.tenant)
        if type(self.allowed_change_fields) is not frozenset or not self.allowed_change_fields <= ORDER_FIELDS:
            raise CoreError('invalid change fields')
        for stages in (self.change_stages, self.cancel_stages):
            if type(stages) is not frozenset or not stages <= set(DIMENSIONS['production']) - {'unknown'}:
                raise CoreError('invalid request stages')
        if type(self.enabled) is not bool:
            raise CoreError('invalid request policy flag')


@dataclass(frozen=True)
class RequestDecision:
    status: str
    reasons: tuple
    approval_required: bool = True
    can_execute: bool = False


def assess_request(principal, order, observation, policy, *, action, changes=None):
    """Eligibility only. Even eligible requests need the host's exact approval flow."""
    if not isinstance(order, Order) or not isinstance(policy, RequestPolicy) or not isinstance(observation, StatusObservation):
        raise CoreError('invalid request decision input')
    order.authorize(principal)
    check_fields(order.fields)
    _scope(principal, policy.tenant)
    if action not in {'change', 'cancel'}:
        raise CoreError('unsupported customer request')
    if changes is None:
        changes = {}
    check_fields(changes)
    if action == 'cancel' and changes:
        raise CoreError('cancel cannot contain field changes')
    if not policy.enabled:
        return RequestDecision('review', ('request-policy-not-connected',))
    if observation.status != 'ok':
        return RequestDecision('review', observation.reasons)
    binding = (order.tenant, order.source, order.external_id, order.version, order.generation)
    if order.generation is None or observation.binding != binding:
        return RequestDecision('review', ('status-order-binding-required',))
    state = _state(observation.state, partial=True)
    if set(state) != set(DIMENSIONS) or any(v == 'unknown' for v in state.values()):
        return RequestDecision('review', ('status-observation-incomplete',))
    if state.get('cancellation') == 'cancelled':
        return RequestDecision('blocked', ('already-cancelled',))
    if state.get('fulfillment') in {'shipped', 'delivered'}:
        return RequestDecision('review', ('delivery-already-started',))
    stage = state.get('production', 'unknown')
    stages = policy.change_stages if action == 'change' else policy.cancel_stages
    if stage == 'unknown' or stage not in stages:
        return RequestDecision('review', ('production-stage-requires-review',))
    if action == 'change' and (not changes or not changes.keys() <= policy.allowed_change_fields):
        return RequestDecision('review', ('change-fields-require-review',))
    if action == 'cancel' and state.get('payment') in {'paid', 'refunded'}:
        return RequestDecision('review', ('payment-reconciliation-required',))
    return RequestDecision('eligible', ('explicit-approval-required',))


DEFAULT_HANDOFF_FIELDS = {
    'production': ('product_id', 'quantity', 'sender_key', 'message'),
    'delivery': ('address', 'delivery_at', 'quantity'),
}


def handoff(principal, order, kind, *, required_fields=None):
    """Version-pinned draft package. Event time never fills missing delivery time."""
    if not isinstance(order, Order) or kind not in DEFAULT_HANDOFF_FIELDS:
        raise CoreError('invalid handoff')
    order.authorize(principal)
    check_fields(order.fields)
    required = DEFAULT_HANDOFF_FIELDS[kind] if required_fields is None else required_fields
    if type(required) is not tuple or len(set(required)) != len(required) or not set(required) <= ORDER_FIELDS:
        raise CoreError('invalid handoff requirements')
    missing = tuple(k for k in required if k not in order.fields)
    return {'status': 'missing' if missing else 'draft', 'kind': kind,
            'tenant': order.tenant, 'source': order.source, 'external_id': order.external_id,
            'version': order.version, 'generation': order.generation, 'missing': missing,
            'fields': json.loads(encoded({k: order.fields[k] for k in required if k in order.fields})),
            'executed': False}


class Webhooks:
    """Durable integration projection with no operational side effects.

    verify(credential, payload) must freshly authenticate the exact payload and
    source account, returning a scoped Principal. A provider sequence and immutable
    creation generation are mandatory; missing ordering proof is rejected. The
    payload is exactly {tenant, source, event_id, order_id, generation, sequence,
    statuses}. No callback chooses arbitrary company, order or destination.
    """
    def __init__(self, store, mapping, *, verify, enabled=False):
        if not isinstance(store, Store) or not isinstance(mapping, StatusMap) or not callable(verify) or type(enabled) is not bool:
            raise CoreError('invalid webhook setup')
        self.store, self.mapping, self.verify, self.enabled = store, mapping, verify, enabled
        with store.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS commerce_projection (
              tenant TEXT, source TEXT, order_id TEXT, generation TEXT,
              sequence INTEGER NOT NULL, state TEXT NOT NULL,
              observed_sequence INTEGER NOT NULL, review_pending INTEGER NOT NULL,
              PRIMARY KEY(tenant,source,order_id));
            CREATE TABLE IF NOT EXISTS commerce_events (
              tenant TEXT, source TEXT, event_id TEXT, fingerprint TEXT NOT NULL,
              result TEXT NOT NULL, PRIMARY KEY(tenant,source,event_id));
            ''')

    def register_order(self, principal, order, *, initial=None):
        if not isinstance(order, Order):
            raise CoreError('invalid registered order')
        order.authorize(principal)
        _scope(principal, self.mapping.tenant)
        if not principal.roles & {'writer', 'admin'}:
            raise AccessDenied('projection registration requires writer/admin')
        if order.source != self.mapping.source:
            raise AccessDenied('source not registered')
        if order.generation is None:
            raise CoreError('explicit immutable order generation required')
        ident(order.generation)
        state = {k: 'unknown' for k in DIMENSIONS} if initial is None else _state(initial)
        if self.mapping.review(state):
            raise CoreError('conflicting initial status')
        scope = (order.tenant, order.source, order.external_id)
        with self.store.db() as c:
            old = c.execute('SELECT * FROM commerce_projection WHERE tenant=? AND source=? AND order_id=?', scope).fetchone()
            if old:
                if old['generation'] != order.generation:
                    raise CoreError('order ID reused: reconcile creation generation explicitly')
                return {'status': 'existing', 'generation': old['generation']}
            c.execute('INSERT INTO commerce_projection VALUES (?,?,?,?,?,?,?,?)', (*scope, order.generation, -1, encoded(state), -1, 0))
        return {'status': 'registered', 'generation': order.generation}

    def state(self, principal, source, external_id):
        _scope(principal, self.mapping.tenant)
        ident(source)
        ident(external_id)
        if source != self.mapping.source:
            raise AccessDenied('source not registered')
        with self.store.db() as c:
            row = c.execute('SELECT * FROM commerce_projection WHERE tenant=? AND source=? AND order_id=?',
                            (principal.tenant, source, external_id)).fetchone()
        if row is None:
            return {'status': 'not_found'}
        return {'status': 'review' if row['review_pending'] else 'observed',
                'generation': row['generation'], 'sequence': row['sequence'],
                'observed_sequence': row['observed_sequence'],
                'generation_binding': (row['tenant'], row['source'], row['order_id'], row['generation']),
                'state': json.loads(row['state'])}

    def receive(self, credential, payload):
        if not self.enabled or not self.mapping.enabled:
            return {'status': 'unavailable'}
        if type(payload) is not dict:
            raise CoreError('invalid webhook envelope')
        payload = json.loads(encoded(payload))
        if set(payload) != {'tenant', 'source', 'event_id', 'order_id', 'generation', 'sequence', 'statuses'}:
            raise CoreError('invalid webhook envelope')
        for key in ('tenant', 'source', 'event_id', 'order_id', 'generation'):
            ident(payload[key])
        if type(payload['sequence']) is not int or not 0 <= payload['sequence'] <= 2**53 - 1:
            raise CoreError('trusted sequence required')
        if payload['tenant'] != self.mapping.tenant or payload['source'] != self.mapping.source:
            return {'status': 'denied'}
        # Copy before verification so neither caller nor verifier can alter the facts applied.
        payload = json.loads(encoded(payload))
        fingerprint = digest(payload)
        try:
            principal = self.verify(credential, json.loads(encoded(payload)))
            _scope(principal, self.mapping.tenant)
            if not principal.roles & {'writer', 'admin'}:
                raise AccessDenied('webhook projection requires writer/admin')
        except Exception:
            return {'status': 'denied'}
        observation = self.mapping.translate(principal, payload['statuses'])
        key = (payload['tenant'], payload['source'], payload['event_id'])
        scope = (payload['tenant'], payload['source'], payload['order_id'])
        with self.store.db() as c:
            old = c.execute('SELECT fingerprint,result FROM commerce_events WHERE tenant=? AND source=? AND event_id=?', key).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise CoreError('event ID content conflict')
                return {**json.loads(old['result']), 'duplicate': True}
            row = c.execute('SELECT * FROM commerce_projection WHERE tenant=? AND source=? AND order_id=?', scope).fetchone()
            if row is None:
                result = {'status': 'unmatched'}
            elif row['generation'] != payload['generation']:
                result = {'status': 'review', 'reasons': ['creation-generation-mismatch']}
            elif payload['sequence'] <= row['observed_sequence']:
                result = {'status': 'stale'}
            elif observation.status != 'ok':
                c.execute('UPDATE commerce_projection SET observed_sequence=?,review_pending=1 WHERE tenant=? AND source=? AND order_id=?', (payload['sequence'], *scope))
                result = {'status': 'review', 'reasons': list(observation.reasons)}
            elif row['review_pending']:
                c.execute('UPDATE commerce_projection SET observed_sequence=? WHERE tenant=? AND source=? AND order_id=?', (payload['sequence'], *scope))
                result = {'status': 'review', 'reasons': ['unresolved-earlier-observation']}
            else:
                previous = json.loads(row['state'])
                state = {**previous, **observation.state}
                reasons = list(self.mapping.review(state))
                for dimension, value in observation.state.items():
                    if DIMENSIONS[dimension].index(value) < DIMENSIONS[dimension].index(previous[dimension]):
                        reasons.append(f'regression-{dimension}')
                if reasons:
                    c.execute('UPDATE commerce_projection SET observed_sequence=?,review_pending=1 WHERE tenant=? AND source=? AND order_id=?', (payload['sequence'], *scope))
                    result = {'status': 'review', 'reasons': reasons}
                else:
                    c.execute('UPDATE commerce_projection SET sequence=?,state=?,observed_sequence=? WHERE tenant=? AND source=? AND order_id=?',
                              (payload['sequence'], encoded(state), payload['sequence'], *scope))
                    result = {'status': 'accepted', 'generation': row['generation'], 'sequence': payload['sequence']}
            c.execute('INSERT INTO commerce_events VALUES (?,?,?,?,?)', (*key, fingerprint, encoded(result)))
        return result
