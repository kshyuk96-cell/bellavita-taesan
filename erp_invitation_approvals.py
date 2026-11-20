"""Read-only paid invitation orders awaiting an explicit ERP staff approval.

The lightweight index has no receipt-date cutoff. ERP filters its own import
ledger before requesting at most 100 complete order records for the dashboard.
"""
import datetime as dt
import json
import sqlite3
from pathlib import Path

import erp_invitation_order_edits as edits


def query(root, params=None):
    params = params or {}
    index = params.get('view', [''])[0] == 'index'
    raw = params.get('ids', [''])[0]
    requested = raw.split(',') if raw else []
    if len(requested) > 100 or len(set(requested)) != len(requested):
        raise ValueError('승인할 주문은 한 번에 100건까지 조회하세요.')
    selected = [edits.source_key(value) for value in requested]
    meta = edits.metadata(root)
    hidden = [edits.source_key(key) for key, row in meta.items() if row['hidden']]
    result = {'orders': [], 'count': 0,
              'checkedAt': dt.datetime.now(dt.timezone.utc).isoformat()}
    file = Path(root) / 'state.sqlite3'
    if not file.is_file():
        return result
    with sqlite3.connect(file.resolve().as_uri() + '?mode=ro', uri=True, timeout=10) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='invitation_orders'").fetchone():
            return result
        operations = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_operations'").fetchone()
        legacy = ("(SELECT MIN(o.order_number) FROM order_operations o WHERE o.id=i.id OR replace(o.id,'-','')=i.id)"
                  if operations else 'NULL')
        where = "i.state='paid' AND i.id NOT IN (SELECT value FROM json_each(?))"
        bindings = [json.dumps(hidden)]
        result['count'] = db.execute('SELECT COUNT(*) FROM invitation_orders i WHERE ' + where,
                                     bindings).fetchone()[0]
        if index:
            rows = db.execute('SELECT i.id,i.created,' + legacy + ' AS legacy_order_number '
                              'FROM invitation_orders i WHERE ' + where +
                              ' ORDER BY i.created DESC,i.id DESC', bindings)
            result['orders'] = [{'id': 'payapp:' + row['id'], 'createdAt': row['created'],
                                 'legacyOrderNumber': row['legacy_order_number']} for row in rows]
            return result
        if selected:
            where += ' AND i.id IN (SELECT value FROM json_each(?))'
            bindings.append(json.dumps(selected))
        rows = db.execute('SELECT i.*,' + legacy + ' AS legacy_order_number '
                          'FROM invitation_orders i WHERE ' + where +
                          ' ORDER BY i.created DESC,i.id DESC LIMIT 100', bindings)
        # Import lazily: management dispatch also imports this module.
        from erp_invitation_management import paid_order
        result['orders'] = [edits.apply(paid_order(row), meta) for row in rows]
        return result
