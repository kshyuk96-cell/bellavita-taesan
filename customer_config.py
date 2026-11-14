"""Audited local-only customer configuration changes; never guesses source identity."""
import contextlib,datetime as dt,functools,json

def atomic_change(method):
    @functools.wraps(method)
    def wrapped(self,*args,**kwargs):
        with self.atomic():return method(self,*args,**kwargs)
    return wrapped

class ConfigChanges:
    @contextlib.contextmanager
    def atomic(self):
        nested=self.db.in_transaction
        if nested:self.db.execute('SAVEPOINT customer_config')
        else:self.db.execute('BEGIN IMMEDIATE')
        try:yield
        except Exception:
            if nested:self.db.execute('ROLLBACK TO customer_config');self.db.execute('RELEASE customer_config')
            else:self.db.rollback()
            raise
        else:
            if nested:self.db.execute('RELEASE customer_config')
            else:self.db.commit()
    def audit(self,action,entity,actor,before,after):
        if not isinstance(actor,str) or not actor.strip():raise ValueError('Confirmed actor required')
        if before==after:return
        self.db.execute('INSERT INTO customer_config_audit(action,entity,actor,before,after,changed) VALUES (?,?,?,?,?,?)',
            (action,entity,actor,json.dumps(before,ensure_ascii=False),json.dumps(after,ensure_ascii=False),dt.datetime.now(dt.timezone.utc).isoformat()))
    @atomic_change
    def allow_room(self,room,confirmed_by):
        import re
        if not isinstance(room,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',room):raise ValueError('Confirmed room ID required')
        before=self.db.execute('SELECT * FROM customer_order_rooms WHERE room=?',(room,)).fetchone()
        self.audit('allow_room',room,confirmed_by,before,(room,confirmed_by))
        self.db.execute('INSERT OR REPLACE INTO customer_order_rooms VALUES (?,?)',(room,confirmed_by))
    @atomic_change
    def disable_room(self,room,confirmed_by):
        before=self.db.execute('SELECT * FROM customer_order_rooms WHERE room=?',(room,)).fetchone()
        bindings=self.db.execute('SELECT * FROM customer_bindings WHERE scope=?',(room,)).fetchall()
        self.audit('disable_room',room,confirmed_by,{'room':before,'bindings':bindings},None)
        self.db.execute('DELETE FROM customer_bindings WHERE scope=?',(room,))
        self.db.execute('DELETE FROM customer_order_rooms WHERE room=?',(room,))
    @atomic_change
    def unbind(self,scope,external_id,customer_id,confirmed_by):
        before=self.db.execute('SELECT * FROM customer_bindings WHERE scope=? AND external_id=?',(scope,external_id)).fetchone()
        if before and before[2]!=customer_id:raise ValueError('Binding customer mismatch')
        self.audit('unbind',scope+':'+external_id,confirmed_by,before,None)
        self.db.execute('DELETE FROM customer_bindings WHERE scope=? AND external_id=?',(scope,external_id))
    @atomic_change
    def remove_variant(self,variant_id,customer_id,confirmed_by):
        before=self.db.execute('SELECT * FROM sender_variants WHERE id=?',(variant_id,)).fetchone()
        if before and before[1]!=customer_id:raise ValueError('Variant customer mismatch')
        self.audit('remove_variant',variant_id,confirmed_by,before,None)
        self.db.execute('DELETE FROM sender_variants WHERE id=?',(variant_id,))
    @atomic_change
    def remove_profile(self,customer_id,confirmed_by):
        before=self.db.execute('SELECT * FROM customer_profiles WHERE id=?',(customer_id,)).fetchone()
        self.audit('remove_profile',customer_id,confirmed_by,before,None)
        for table,column in (('customer_bindings','customer_id'),('sender_variants','customer_id'),('sender_evidence','customer_id'),('customer_profiles','id')):
            self.db.execute('DELETE FROM '+table+' WHERE '+column+'=?',(customer_id,))
