"""Owner-scoped drafts with optimistic revision checks; no ledger or shopping writes."""
from dataclasses import dataclass
import json
import secrets
from .commerce import Order, check_fields
from .core import AccessDenied, CoreError, encoded, ident


@dataclass(frozen=True)
class Draft:
    id: str
    revision: int
    fields: dict
    missing: tuple
    source: str
    external_id: str
    source_version: str
    source_generation: str | None = None


class Drafts:
    def __init__(self, store, companies):
        self.store, self.companies = store, companies
        with self.store.db() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS commerce_drafts (
                id TEXT PRIMARY KEY, tenant TEXT, subject TEXT, source TEXT,
                external_id TEXT, source_version TEXT, generation TEXT, revision INTEGER, fields TEXT,
                expires REAL)''')

    def _view(self, row, company):
        fields = json.loads(row['fields'])
        return Draft(row['id'], row['revision'], fields,
                     tuple(k for k in company.required_fields if k not in fields),
                     row['source'], row['external_id'], row['source_version'], row['generation'])

    def create(self, principal, order):
        company = self.companies.for_principal(principal)
        if not isinstance(order, Order):
            raise CoreError('normalized order required')
        order.authorize(principal)
        check_fields(order.fields)
        key = secrets.token_hex(16)
        with self.store.db() as c:
            c.execute('INSERT INTO commerce_drafts VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (key, company.tenant, principal.subject, order.source, order.external_id,
                       order.version, order.generation, 1, encoded(order.fields), self.store.now() + self.store.ttl))
            row = c.execute('SELECT * FROM commerce_drafts WHERE id=?', (key,)).fetchone()
        return self._view(row, company)

    def _row(self, c, principal, key):
        ident(key)
        row = c.execute('SELECT * FROM commerce_drafts WHERE id=? AND tenant=? AND subject=?',
                        (key, principal.tenant, principal.subject)).fetchone()
        if not row or row['expires'] <= self.store.now():
            raise AccessDenied('draft missing, expired, or belongs to another employee')
        return row

    def get(self, principal, key):
        company = self.companies.for_principal(principal)
        with self.store.db() as c:
            return self._view(self._row(c, principal, key), company)

    def patch(self, principal, key, expected_revision, changes, *, clear_fields=(), current_order=None):
        company = self.companies.for_principal(principal)
        if type(expected_revision) is not int or expected_revision < 1:
            raise CoreError('draft revision required')
        check_fields(changes)
        from .commerce import ORDER_FIELDS
        if type(clear_fields) is not tuple or len(set(clear_fields)) != len(clear_fields) or not set(clear_fields) <= ORDER_FIELDS or set(clear_fields) & changes.keys():
            raise CoreError('invalid clear fields')
        if current_order is not None:
            if not isinstance(current_order, Order):
                raise CoreError('normalized current order required')
            current_order.authorize(principal)
        with self.store.db() as c:
            row = self._row(c, principal, key)
            if row['revision'] != expected_revision:
                raise CoreError('draft changed; reread current revision')
            if current_order is not None and (current_order.source, current_order.external_id, current_order.version) != (row['source'], row['external_id'], row['source_version'], row['generation']):
                raise CoreError('source order changed; review required')
            fields = json.loads(row['fields'])
            fields.update(changes)
            for field in clear_fields:
                fields.pop(field, None)
            c.execute('UPDATE commerce_drafts SET fields=?,revision=revision+1,expires=? WHERE id=?',
                      (encoded(fields), self.store.now() + self.store.ttl, key))
            result = c.execute('SELECT * FROM commerce_drafts WHERE id=?', (key,)).fetchone()
        return self._view(result, company)

    def purge(self):
        with self.store.db() as c:
            c.execute('DELETE FROM commerce_drafts WHERE expires<=?', (self.store.now(),))
