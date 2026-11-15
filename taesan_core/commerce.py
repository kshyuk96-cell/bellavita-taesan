"""Company-scoped business contracts. No inferred company policies or live connectors."""
from dataclasses import dataclass, field
from datetime import datetime
from .core import AccessDenied, CoreError, Principal, encoded, ident

ORDER_FIELDS = frozenset({'event_at', 'delivery_at', 'address', 'venue_detail', 'sender_key',
                          'sender_name', 'product_id', 'quantity', 'buyer_name', 'message',
                          'options', 'amount', 'currency'})
DEFAULT_REQUIRED = ('event_at', 'address', 'sender_key', 'product_id', 'quantity')


def iso_time(value):
    if type(value) is not str:
        raise CoreError('timezone-aware ISO time required')
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CoreError('invalid event time') from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise CoreError('timezone-aware ISO time required')
    return result


def check_fields(value):
    if type(value) is not dict or not value.keys() <= ORDER_FIELDS:
        raise CoreError('unsupported order field')
    for key, item in value.items():
        if key in {'event_at', 'delivery_at'}:
            iso_time(item)
        elif key in {'amount', 'quantity'}:
            if type(item) is not int or item < (1 if key == 'quantity' else 0) or item > 10**12:
                raise CoreError('invalid integer order value')
        elif key == 'options':
            if type(item) is not dict or len(item) > 30 or any(
                    type(k) is not str or not k or len(k) > 100 or
                    type(v) is not str or not v or len(v) > 300 for k, v in item.items()):
                raise CoreError('invalid order options')
        elif type(item) is not str or not item.strip() or len(item) > 2000:
            raise CoreError('invalid order text')
    encoded(value)
    return value


@dataclass(frozen=True)
class Company:
    tenant: str
    name: str
    required_fields: tuple = DEFAULT_REQUIRED
    enabled: bool = False
    currency: str = 'KRW'

    def __post_init__(self):
        ident(self.tenant)
        if type(self.name) is not str or not self.name.strip() or len(self.name) > 100:
            raise CoreError('invalid company name')
        if type(self.required_fields) is not tuple or len(set(self.required_fields)) != len(self.required_fields) or not set(self.required_fields) <= ORDER_FIELDS:
            raise CoreError('invalid required fields')
        if type(self.enabled) is not bool or self.currency != 'KRW':
            raise CoreError('unsupported configuration')


class Companies:
    def __init__(self):
        self._items = {}

    def register(self, company):
        if not isinstance(company, Company) or company.tenant in self._items:
            raise CoreError('duplicate or invalid company')
        self._items[company.tenant] = company

    def for_principal(self, principal):
        if not isinstance(principal, Principal):
            raise AccessDenied('authenticated principal required')
        company = self._items.get(principal.tenant)
        if company is None or not company.enabled:
            raise AccessDenied('company is not connected')
        return company

    @classmethod
    def planned(cls):
        """Names supplied by the owner; operational rules remain unapproved/disabled."""
        value = cls()
        value.register(Company('hope-sunrise', '희망일출'))
        value.register(Company('bellavita', '벨라비타'))
        return value


@dataclass(frozen=True)
class Order:
    tenant: str
    source: str
    external_id: str
    version: str
    fields: dict = field(default_factory=dict)
    generation: str | None = None

    def __post_init__(self):
        for value in (self.tenant, self.source, self.external_id, self.version):
            ident(value)
        if self.generation is not None:
            ident(self.generation)
        check_fields(self.fields)
        # Defensive JSON copy: external mutable input must not change a normalized order.
        import json
        object.__setattr__(self, 'fields', json.loads(encoded(self.fields)))

    def authorize(self, principal):
        if not isinstance(principal, Principal) or principal.tenant != self.tenant:
            raise AccessDenied('order belongs to another company')
        return self


def normalize_order(principal, companies, source, payload, *, field_map):
    """Trusted connector config maps source fields; client cannot choose company or mapping.

    Payload fields: id, version, values; optional generation is a stable creation ID. Event/delivery times remain independent.
    Unknown source fields are rejected instead of silently assigned business meaning.
    """
    company = companies.for_principal(principal)
    ident(source)
    if type(field_map) is not dict or len(set(field_map.values())) != len(field_map) or not set(field_map.values()) <= ORDER_FIELDS:
        raise CoreError('invalid connector mapping')
    if type(payload) is not dict or not {'id','version','values'} <= payload.keys() or not payload.keys() <= {'id','version','values','generation'} or type(payload['values']) is not dict:
        raise CoreError('invalid connector order')
    if not payload['values'].keys() <= field_map.keys():
        raise CoreError('unmapped connector field')
    fields = {field_map[k]: value for k, value in payload['values'].items()}
    if 'currency' in fields and fields['currency'] != company.currency:
        raise CoreError('unsupported order currency')
    return Order(company.tenant, source, payload['id'], payload['version'], fields, payload.get('generation'))
