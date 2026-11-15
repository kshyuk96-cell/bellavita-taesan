"""Read-only duplicate suspicion based on host-verified place identity.

Raw address spelling, coordinates, and a model's guesses never establish location
identity. The host owns authenticated resolution and canonical sender DB keys.
This module does not fetch addresses, write orders, notify customers, or delete.
"""
from dataclasses import dataclass
from .commerce import Order, check_fields, iso_time
from .core import AccessDenied, CoreError, Evidence, Principal, ident


def _detail(value):
    if value is not None and (type(value) is not str or not value.strip() or len(value) > 150):
        raise CoreError('invalid canonical location detail')


@dataclass(frozen=True)
class Location:
    """A trusted resolver observation, not client/model input.

    A resolved place includes a verified canonical building ID and receipt.
    floor/hall/wing are canonical dimension IDs. None means unknown, never equal.
    The resolver may use an explicit stable value such as 'not-applicable' when it
    verified that a dimension does not apply. Multiple address matches must be
    represented as ambiguous, not resolved with a guessed first match.
    """
    status: str
    tenant: str
    building_id: str | None = None
    floor: str | None = None
    hall: str | None = None
    wing: str | None = None
    evidence: Evidence | None = None

    def __post_init__(self):
        ident(self.tenant)
        if self.status not in {'resolved', 'ambiguous', 'unavailable'}:
            raise CoreError('invalid location status')
        if self.building_id is not None:
            ident(self.building_id)
        for value in (self.floor, self.hall, self.wing):
            _detail(value)
        if self.status == 'resolved':
            if self.building_id is None or not isinstance(self.evidence, Evidence):
                raise CoreError('verified location identity and receipt required')
        elif any(value is not None for value in (self.building_id, self.floor, self.hall, self.wing, self.evidence)):
            raise CoreError('unresolved location must not imply identity')


@dataclass(frozen=True)
class DuplicateFinding:
    source: str
    external_id: str
    version: str
    decision: str
    reason: str

    def __post_init__(self):
        for value in (self.source, self.external_id, self.version, self.reason):
            ident(value)
        if self.decision not in {'suspected', 'review', 'different'}:
            raise CoreError('invalid duplicate decision')


@dataclass(frozen=True)
class DuplicateReport:
    status: str
    findings: tuple

    def __post_init__(self):
        if self.status not in {'suspected', 'review', 'clear'} or type(self.findings) is not tuple or not all(
                isinstance(value, DuplicateFinding) for value in self.findings):
            raise CoreError('invalid duplicate report')


class DuplicateReviewer:
    """Read scoped candidate orders using an optional host location resolver.

    resolver(principal, address, venue_detail) -> Location. The host must validate
    provider ownership and source scope before creating Location. Missing resolver,
    exceptions, malformed/cross-company results, or ambiguous results all require
    review. Location observations are ephemeral; this module persists no PII.
    """
    def __init__(self, resolver=None):
        if resolver is not None and not callable(resolver):
            raise CoreError('invalid location resolver')
        self._resolver = resolver

    @staticmethod
    def _validate_order(principal, order):
        if not isinstance(order, Order):
            raise CoreError('normalized order required')
        order.authorize(principal)
        # Recheck mutable fields at this boundary; do not trust an altered dataclass.
        check_fields(order.fields)

    def _resolve(self, principal, order):
        address = order.fields.get('address')
        if not address or self._resolver is None:
            return None
        try:
            value = self._resolver(principal, address, order.fields.get('venue_detail', ''))
        except Exception:
            return None
        if not isinstance(value, Location) or value.tenant != principal.tenant:
            return None
        return value

    def _compare(self, principal, order, candidate, location):
        left, right = order.fields, candidate.fields
        if not left.get('event_at') or not right.get('event_at'):
            return 'review', 'missing-event-time'
        if iso_time(left['event_at']) != iso_time(right['event_at']):
            return 'different', 'different-event-time'
        if not left.get('sender_key') or not right.get('sender_key'):
            return 'review', 'missing-authoritative-sender-key'
        if left['sender_key'] != right['sender_key']:
            return 'different', 'different-sender-key'
        target = location()  # Resolve lazily only after the event/sender comparison.
        other = self._resolve(principal, candidate)
        if target is None or other is None:
            return 'review', 'location-unavailable'
        if target.status != 'resolved' or other.status != 'resolved':
            reason = 'location-ambiguous' if 'ambiguous' in (target.status, other.status) else 'location-unavailable'
            return 'review', reason
        if target.building_id != other.building_id:
            return 'different', 'different-building'
        details = [(getattr(target, name), getattr(other, name)) for name in ('floor', 'hall', 'wing')]
        if any(a is not None and b is not None and a != b for a, b in details):
            return 'different', 'different-location-detail'
        if any(a is None or b is None for a, b in details):
            return 'review', 'missing-location-detail'
        return 'suspected', 'same-event-sender-and-verified-location'

    def review(self, principal, order, candidates):
        if not isinstance(principal, Principal):
            raise AccessDenied('authenticated principal required')
        if type(candidates) not in (list, tuple) or len(candidates) > 1000:
            raise CoreError('bounded candidate list required')
        self._validate_order(principal, order)
        # Validate the complete batch before calling the resolver or returning any findings.
        for candidate in candidates:
            self._validate_order(principal, candidate)
        cache = []

        def location():
            if not cache:
                cache.append(self._resolve(principal, order))
            return cache[0]

        grouped = {}
        for candidate in candidates:
            key = (candidate.source, candidate.external_id)
            if key == (order.source, order.external_id):
                continue
            grouped.setdefault(key, []).append(candidate)
        findings = []
        for key in sorted(grouped):
            group = grouped[key]
            # Conflicting snapshots of one reference must be reconciled before deciding.
            from .core import digest
            if len({digest({'version': item.version, 'generation': item.generation, 'fields': item.fields}) for item in group}) > 1:
                candidate = group[0]
                decision, reason = 'review', 'conflicting-candidate-versions'
            else:
                candidate = group[0]
                decision, reason = self._compare(principal, order, candidate, location)
            findings.append(DuplicateFinding(candidate.source, candidate.external_id,
                                             candidate.version, decision, reason))
        # Any unresolved candidate remains visible even if another candidate is a match.
        status = 'suspected' if any(f.decision == 'suspected' for f in findings) else (
            'review' if any(f.decision == 'review' for f in findings) else 'clear')
        return DuplicateReport(status, tuple(findings))
