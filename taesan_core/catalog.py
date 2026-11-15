"""Read-only catalog validation. Prices and inventory come from a trusted host snapshot."""
from dataclasses import dataclass, field
import math
import time
from .core import AccessDenied, CoreError, Evidence, Principal, ident


def money(value):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise CoreError('integer KRW value required')
    return value


@dataclass(frozen=True)
class Option:
    name: str
    choices: dict
    required: bool = True

    def __post_init__(self):
        ident(self.name)
        if type(self.required) is not bool or type(self.choices) is not dict or not 1 <= len(self.choices) <= 100:
            raise CoreError('invalid option')
        for name, extra in self.choices.items():
            if type(name) is not str or not name.strip() or len(name) > 200:
                raise CoreError('invalid option choice')
            money(extra)
        object.__setattr__(self, 'choices', dict(self.choices))


@dataclass(frozen=True)
class Product:
    tenant: str
    id: str
    version: str
    price: int
    evidence: Evidence
    options: tuple = ()
    visible: bool = False
    active: bool = False
    stock: int | None = None
    tracks_stock: bool = True
    currency: str = 'KRW'

    def __post_init__(self):
        for value in (self.tenant, self.id, self.version):
            ident(value)
        money(self.price)
        if type(self.options) is not tuple or any(not isinstance(o, Option) for o in self.options) or len({o.name for o in self.options}) != len(self.options):
            raise CoreError('invalid product options')
        if any(type(v) is not bool for v in (self.visible, self.active, self.tracks_stock)) or self.currency != 'KRW' or not isinstance(self.evidence, Evidence):
            raise CoreError('invalid product state')
        if self.stock is not None and (type(self.stock) is not int or self.stock < 0):
            raise CoreError('invalid stock quantity')


@dataclass(frozen=True)
class Quote:
    status: str
    reason: str
    amount: int | None = None
    currency: str | None = None
    product_version: str | None = None
    evidence: Evidence | None = None


class Catalog:
    def __init__(self, companies, *, provider=None, clock=time.time, max_age=120):
        self.companies, self.provider, self.clock = companies, provider, clock
        if provider is not None and not callable(provider):
            raise CoreError('invalid catalog provider')
        if type(max_age) not in (int,float) or not math.isfinite(max_age) or not 0 < max_age <= 3600:
            raise CoreError('invalid snapshot age')
        self.max_age = max_age

    def quote(self, principal, product_id, quantity, selections=None, *, expected_version=None, submitted_amount=None):
        company = self.companies.for_principal(principal)
        ident(product_id)
        if expected_version is not None:
            ident(expected_version)
        if type(quantity) is not int or not 1 <= quantity <= 1000000:
            raise CoreError('invalid quantity')
        selections = {} if selections is None else selections
        if type(selections) is not dict:
            raise CoreError('invalid option selections')
        if submitted_amount is not None:
            money(submitted_amount)
        if self.provider is None:
            return Quote('unavailable', 'catalog_not_connected')
        try:
            product = self.provider(principal, product_id)
        except PermissionError:
            return Quote('denied', 'catalog_access_denied')
        except Exception:
            return Quote('unavailable', 'catalog_read_failed')
        if product is None:
            return Quote('not_found', 'product_not_found')
        if not isinstance(product, Product):
            return Quote('needs_review', 'invalid_product_response')
        if product.tenant != company.tenant or product.id != product_id:
            return Quote('denied', 'product_scope_mismatch')
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise CoreError('invalid clock')
        if not 0 <= now - product.evidence.observed_at <= self.max_age:
            return Quote('needs_review', 'catalog_snapshot_stale')
        if not product.visible or not product.active:
            return Quote('unavailable', 'product_not_on_sale')
        if product.currency != company.currency:
            return Quote('needs_review', 'currency_mismatch')
        if expected_version is not None and expected_version != product.version:
            return Quote('needs_review', 'product_version_changed')
        options = {o.name:o for o in product.options}
        if not selections.keys() <= options.keys():
            return Quote('invalid', 'unknown_option')
        total = product.price
        for name, option in options.items():
            if name not in selections:
                if option.required:
                    return Quote('clarify', 'missing_option:' + name)
            elif type(selections[name]) is not str or selections[name] not in option.choices:
                return Quote('invalid', 'invalid_option:' + name)
            else:
                total += option.choices[selections[name]]
        total = money(total * quantity)
        if product.tracks_stock and product.stock is None:
            return Quote('needs_review', 'inventory_not_readable')
        if product.tracks_stock and quantity > product.stock:
            return Quote('unavailable', 'insufficient_stock')
        if submitted_amount is not None and submitted_amount != total:
            return Quote('needs_review', 'price_mismatch', total, company.currency, product.version, product.evidence)
        return Quote('ok', 'catalog_verified', total, company.currency, product.version, product.evidence)
