"""Real local SQLite transactions, synthetic orders only; no operational connection."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from taesan_core import Core, Evidence, Field, Plan, Principal, Registry, Result, Snapshot, Store, Tool


class OrderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'orders.sqlite3'
        with sqlite3.connect(self.path) as c:
            c.executescript('''CREATE TABLE orders (tenant TEXT, id TEXT, version INTEGER, completed INTEGER, center TEXT);
            CREATE TABLE operations (key TEXT PRIMARY KEY);
            INSERT INTO orders VALUES ('company-a','13370',1,0,'guro');
            INSERT INTO orders VALUES ('company-a','13371',1,1,'guro');
            INSERT INTO orders VALUES ('company-a','13372',1,NULL,'guro');
            INSERT INTO orders VALUES ('company-b','13370',1,0,'dongdaemun');''')
        self.core = Core(Store(Path(self.tmp.name) / 'private' / 'core.sqlite3'), Registry())
        self.staff = Principal('company-a', 'employee', frozenset({'writer'}))
        fields = {'id': Field('주문번호'), 'completed': Field('완료 상태', 'boolean')}
        self.core.registry.register(Tool('order.complete', fields, self.write, kind='write',
                                        roles=frozenset({'writer','admin'}), prepare=self.prepare, enabled=True))
        self.core.registry.register(Tool('order.list', {'status': Field('상태', choices=('all','complete','incomplete','review'))}, self.read, enabled=True))

    def prepare(self, principal, args):
        with sqlite3.connect(self.path) as c:
            row = c.execute('SELECT version, completed, center FROM orders WHERE tenant=? AND id=?', (principal.tenant, args['id'])).fetchone()
        if not row:
            raise LookupError('not found')
        return Snapshot(args['id'], str(row[0]), {'center': row[2], 'before': row[1], 'after': args['completed']})

    def write(self, principal, args, snapshot, *, idempotency_key):
        with sqlite3.connect(self.path) as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM operations WHERE key=?', (idempotency_key,)).fetchone():
                changed = c.execute('UPDATE orders SET completed=?, version=version+1 WHERE tenant=? AND id=? AND version=?',
                                    (args['completed'], principal.tenant, snapshot.target, int(snapshot.version))).rowcount
                if changed != 1:
                    return Result('uncertain')
                c.execute('INSERT INTO operations VALUES (?)', (idempotency_key,))
            row = c.execute('SELECT completed FROM orders WHERE tenant=? AND id=?', (principal.tenant, snapshot.target)).fetchone()
        return Result('ok', {'id': snapshot.target}, Evidence('fixture-sqlite', idempotency_key, 100),
                      applied=True, readback_matches=row[0] == args['completed'])

    def read(self, principal, args):
        with sqlite3.connect(self.path) as c:
            rows = c.execute('SELECT id, completed FROM orders WHERE tenant=?', (principal.tenant,)).fetchall()
        status = args['status']
        accepted = {'all': lambda b: True, 'complete': lambda b: b == 1,
                    'incomplete': lambda b: b == 0, 'review': lambda b: b is None}
        rows = [r[0] for r in rows if accepted[status](r[1])]
        return Result('ok' if rows else 'empty', {'ids': rows}, Evidence('fixture-sqlite', 'read-1', 100), count=len(rows))

    def request(self, request, plan=None, confirm=None):
        return self.core.handle(self.staff, 'erp', 'thread', request, plan=plan, confirm=confirm)

    def test_database_boolean_not_photo_or_label(self):
        for status, expected in [('all',['13370','13371','13372']),('complete',['13371']),('incomplete',['13370']),('review',['13372'])]:
            with self.subTest(status=status):
                reply = self.request(status, Plan('order.list', {'status': status}))
                self.assertEqual(reply.data['result']['ids'], expected)

    def test_preview_does_not_mutate_then_confirm_readback(self):
        preview = self.request('preview', Plan('order.complete', {'id':'13370','completed': True}))
        self.assertEqual(self.prepare(self.staff, {'id':'13370','completed':True}).version, '1')
        reply = self.request('confirm', confirm=preview.data['confirmation'])
        self.assertEqual(reply.status,'ok')
        self.assertEqual(self.prepare(self.staff, {'id':'13370','completed':True}).version, '2')
        again = self.request('confirm', confirm=preview.data['confirmation'])
        self.assertEqual(again, reply)
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT completed FROM orders WHERE tenant='company-b' AND id='13370'").fetchone()[0], 0)

    def test_concurrent_provider_change_between_prepare_and_write(self):
        old_execute = self.write
        def racing(principal, args, snapshot, *, idempotency_key):
            with sqlite3.connect(self.path) as c:
                c.execute("UPDATE orders SET version=version+1, completed=0 WHERE tenant='company-a' AND id='13370'")
            return old_execute(principal,args,snapshot,idempotency_key=idempotency_key)
        registry = Registry()
        registry.register(Tool('order.race', {'id':Field('번호'),'completed':Field('완료','boolean')}, racing,
                               kind='write',roles=frozenset({'writer'}),prepare=self.prepare,enabled=True))
        self.core.registry = registry
        p = self.request('p', Plan('order.race', {'id':'13370','completed':True}))
        self.assertEqual(self.request('c', confirm=p.data['confirmation']).status, 'uncertain')
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 0)

    def test_reused_row_target_different_version_blocked(self):
        p = self.request('p', Plan('order.complete', {'id':'13370','completed':True}))
        with sqlite3.connect(self.path) as c:
            c.execute("DELETE FROM orders WHERE tenant='company-a' AND id='13370'")
            c.execute("INSERT INTO orders VALUES ('company-a','13370',2,0,'suwon')")
        self.assertEqual(self.request('c',confirm=p.data['confirmation']).status, 'stale')
