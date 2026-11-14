"""Quarantine unfinished legacy Kakao orders without guessing their customer source."""

class OriginUnverified(ValueError):pass

def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS order_origin_approvals (
        order_id TEXT PRIMARY KEY,actor TEXT NOT NULL,reason TEXT NOT NULL,confirmed_at TEXT NOT NULL)''')
    c.execute('CREATE TABLE IF NOT EXISTS order_intake_aliases (alias_key TEXT PRIMARY KEY,order_id TEXT NOT NULL)')

def approved(c,key):return c.execute('SELECT 1 FROM order_origin_approvals WHERE order_id=?',(key,)).fetchone() is not None

def record_manual(c,key,actor,reason):
    import order_operations as ops
    row=c.execute('SELECT owner,kind FROM order_operations WHERE id=?',(key,)).fetchone()
    if not row or row['owner']!=actor or row['kind']!='kakao' or not str(reason).strip():raise ValueError('Authenticated owning staff confirmation required')
    c.execute('INSERT OR REPLACE INTO order_origin_approvals VALUES (?,?,?,?)',(key,actor,reason,ops.now()))

def hold(c,key):
    import order_sharing
    c.execute("UPDATE order_operations SET state='origin_hold' WHERE id=? AND state NOT IN ('cancel_requested','cancel_confirmed')",(key,))
    order_sharing.invalidate(c,key)

def unfinished(c,row):
    if row['state'] in ('pending','update','review_hold','origin_hold'):return True
    return row['state']=='saved' and bool(c.execute("SELECT 1 FROM order_share_jobs WHERE order_id=? AND state IN ('waiting','form_review')",(row['id'],)).fetchone())

def classify_untracked(db):
    from order_lock import writer_lock
    with writer_lock(db),db() as c:
        rows=c.execute("SELECT o.* FROM order_operations o LEFT JOIN order_sources s ON s.order_id=o.id LEFT JOIN order_origin_approvals a ON a.order_id=o.id WHERE o.kind='kakao' AND s.order_id IS NULL AND a.order_id IS NULL AND o.state IN ('pending','update','review_hold','saved')").fetchall()
        count=0
        for row in rows:
            if unfinished(c,row):hold(c,row['id']);count+=1
        return count
