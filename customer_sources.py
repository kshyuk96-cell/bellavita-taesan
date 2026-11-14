"""Persisted, revocable source grants for staff-forwarded customer orders."""
import contextlib,json,sqlite3
from pathlib import Path

class SourceRevoked(ValueError):pass

def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS order_sources (order_id TEXT PRIMARY KEY, context TEXT NOT NULL)')

def context(memory,room,sender):
    grant=memory.db.execute('SELECT token FROM customer_room_grants WHERE room=?',(room,)).fetchone()
    allowed=memory.db.execute('SELECT 1 FROM customer_order_rooms WHERE room=?',(room,)).fetchone()
    if not grant or not allowed:raise SourceRevoked('주문 수집 방의 허용 상태를 확인해 주세요.')
    binding=memory.db.execute('SELECT token FROM customer_binding_grants WHERE scope=? AND external_id=?',(room,sender)).fetchone()
    return {'room':room,'sender':sender,'room_grant':grant[0],'binding_grant':binding[0] if binding else None}

def valid(memory,source):
    if not isinstance(source,dict) or set(source)!={'room','sender','room_grant','binding_grant'}:return False
    try:current=context(memory,source['room'],source['sender'])
    except (ValueError,TypeError):return False
    return current['room_grant']==source['room_grant'] and (source['binding_grant'] is None or current['binding_grant']==source['binding_grant'])

@contextlib.contextmanager
def guard(path,source):
    from customer_memory import CustomerMemory
    if not Path(path).is_file():raise SourceRevoked('고객 연결 설정을 찾지 못해 자동 처리를 중지했어요.')
    try:memory=CustomerMemory(path)
    except sqlite3.Error as error:raise SourceRevoked('고객 연결 설정을 읽지 못해 자동 처리를 중지했어요.') from error
    try:
        with memory.atomic():
            if not valid(memory,source):raise SourceRevoked('고객방·발신자 연결이 해제되거나 변경되어 자동 처리를 중지했어요.')
            yield
    finally:memory.db.close()

def hold(c,key):
    import order_sharing
    row=c.execute('SELECT state FROM order_operations WHERE id=?',(key,)).fetchone()
    if row and row[0] not in ('cancel_requested','cancel_confirmed','source_hold'):
        c.execute("UPDATE order_operations SET state='source_hold' WHERE id=?",(key,))
        order_sharing.invalidate(c,key)

def suspend_revoked(db,path):
    from customer_memory import CustomerMemory
    from order_lock import writer_lock
    with writer_lock(db):
        with db() as c:
            rows=c.execute("SELECT s.order_id,s.context FROM order_sources s JOIN order_operations o ON o.id=s.order_id WHERE o.state IN ('pending','update','review_hold') OR (o.state='saved' AND EXISTS (SELECT 1 FROM order_share_jobs j WHERE j.order_id=o.id AND j.state IN ('waiting','form_review')))").fetchall()
        if not rows:return 0
        try:memory=CustomerMemory(path) if Path(path).is_file() else None
        except sqlite3.Error:memory=None
        changed=0
        try:
            scope=memory.atomic() if memory else contextlib.nullcontext()
            with scope,db() as c:
                for row in rows:
                    try:allowed=memory is not None and valid(memory,json.loads(row['context']))
                    except (ValueError,TypeError):allowed=False
                    if not allowed:hold(c,row['order_id']);changed+=1
        finally:
            if memory:memory.db.close()
        return changed
