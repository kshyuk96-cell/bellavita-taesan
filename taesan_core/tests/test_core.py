"""Synthetic provider tests: no live credentials, customer records, or external writes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from taesan_core import (AccessDenied, Attachment, Core, CoreError, Evidence,
                         Field, Plan, Principal, Registry, Result, Snapshot,
                         Store, Tool)


class CoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1700000000.0
        self.path = Path(self.tmp.name) / 'core.sqlite3'
        self.store = Store(self.path, clock=lambda: self.now)
        self.registry = Registry()
        self.core = Core(self.store, self.registry)
        self.reader = Principal('company-a', 'employee-a', frozenset({'reader'}))
        self.writer = replace(self.reader, roles=frozenset({'writer'}))
        self.calls = []
        self.row = {'version': 'v1', 'quantity': 2, 'completed': False}
        self.applied_keys = set()
        self.sequence = 0
        self.fields = {'date': Field('날짜'), 'center': Field('센터'),
                       'status': Field('상태', choices=('all', 'completed', 'pending'))}
        self.registry.register(Tool('deliveries', self.fields, self.read,
                                    enabled=True, context_fields=frozenset(self.fields)))
        self.registry.register(Tool('stock', {'quantity': Field('수량', 'integer')},
                                    self.write, kind='write', roles=frozenset({'writer', 'admin'}),
                                    prepare=self.prepare, enabled=True))

    def evidence(self):
        return Evidence('fixture-db', 'receipt-1', self.now)

    def read(self, principal, args):
        self.calls.append(('read', principal, dict(args)))
        # Completion is a true boolean in the provider, never a model interpretation.
        actual = self.row['completed']
        matches = args['status'] == 'all' or (args['status'] == 'completed' and actual is True) or (args['status'] == 'pending' and actual is False)
        return Result('ok' if matches else 'empty', {'completed': actual} if matches else {},
                      self.evidence(), 1 if matches else 0)

    def prepare(self, principal, args):
        return Snapshot('order-13370', self.row['version'],
                        {'before': self.row['quantity'], 'after': args['quantity']})

    def write(self, principal, args, snapshot, *, idempotency_key):
        self.calls.append(('write', principal, dict(args)))
        if snapshot.version != self.row['version']:
            return Result('uncertain')
        if idempotency_key not in self.applied_keys:
            self.applied_keys.add(idempotency_key)
            self.row['quantity'] = args['quantity']
            self.row['version'] = 'v2'
        return Result('ok', {'quantity': self.row['quantity']}, self.evidence(),
                      applied=True, readback_matches=self.row['quantity'] == args['quantity'])

    def request(self, plan=None, *, principal=None, channel='erp', conversation='chat-1', request=None, confirm=None):
        self.sequence += 1
        return self.core.handle(principal or self.reader, channel, conversation,
                                request or f'request-{self.sequence}', plan=plan, confirm=confirm)

    def full_read(self, **changes):
        return Plan('deliveries', {'date': '2026-11-16', 'center': 'all',
                                   'status': 'pending', **changes})

    def preview(self, quantity=4, **kwargs):
        return self.request(Plan('stock', {'quantity': quantity}), principal=self.writer, **kwargs)

    def test_zero_is_distinct_from_connection_failure(self):
        self.assertEqual(self.request(self.full_read(status='completed')).status, 'empty')
        self.registry.register(Tool('offline', {}, lambda *_: Result('unavailable'), enabled=True))
        result = self.request(Plan('offline'))
        self.assertEqual(result.status, 'unavailable')
        self.assertNotEqual(result.data['count'], 0)

    def test_positive_read_has_boolean_and_evidence(self):
        result = self.request(self.full_read())
        self.assertEqual(result.status, 'ok')
        self.assertIs(result.data['result']['completed'], False)
        self.assertEqual(result.data['evidence']['source'], 'fixture-db')

    def test_unknown_tool_unavailable(self):
        self.assertEqual(self.request(Plan('not-connected')).status, 'unavailable')
        self.assertEqual(self.calls, [])

    def test_disabled_tool_never_called(self):
        self.registry.register(Tool('disabled', {}, lambda *_: self.fail('executor called')))
        self.assertEqual(self.request(Plan('disabled')).status, 'unavailable')

    def test_provider_denial_is_not_zero(self):
        self.registry.register(Tool('restricted', {}, lambda *_: Result('denied'), enabled=True))
        result = self.request(Plan('restricted'))
        self.assertEqual(result.status, 'denied')
        self.assertIsNone(result.data['count'])

    def test_followup_preserves_date_and_status(self):
        self.request(self.full_read())
        self.request(Plan('deliveries', {'center': 'guro'}, True))
        self.assertEqual(self.calls[-1][2], {'date': '2026-11-16', 'center': 'guro', 'status': 'pending'})

    def test_followup_changes_only_status(self):
        self.request(self.full_read(center='guro'))
        self.request(Plan('deliveries', {'status': 'completed'}, True))
        self.assertEqual(self.calls[-1][2]['center'], 'guro')
        self.assertEqual(self.calls[-1][2]['date'], '2026-11-16')
        self.assertEqual(self.calls[-1][2]['status'], 'completed')

    def test_new_request_does_not_implicitly_reuse_context(self):
        self.request(self.full_read())
        result = self.request(Plan('deliveries', {'center': 'guro'}))
        self.assertEqual(result.status, 'clarify')
        self.assertEqual(result.data['missing'], ['date', 'status'])

    def test_other_employee_has_no_context(self):
        self.request(self.full_read())
        other = replace(self.reader, subject='employee-b')
        result = self.request(Plan('deliveries', {'center': 'guro'}, True), principal=other)
        self.assertEqual(result.data['missing'], ['date', 'status'])

    def test_other_tenant_has_no_context(self):
        self.request(self.full_read())
        other = replace(self.reader, tenant='company-b')
        result = self.request(Plan('deliveries', {'center': 'guro'}, True), principal=other)
        self.assertEqual(result.data['missing'], ['date', 'status'])

    def test_other_channel_has_no_context(self):
        self.request(self.full_read())
        result = self.request(Plan('deliveries', {'center': 'guro'}, True), channel='kakao')
        self.assertEqual(result.data['missing'], ['date', 'status'])

    def test_other_conversation_has_no_context(self):
        self.request(self.full_read())
        result = self.request(Plan('deliveries', {'center': 'guro'}, True), conversation='chat-2')
        self.assertEqual(result.data['missing'], ['date', 'status'])

    def test_expired_context_not_reused(self):
        self.request(self.full_read())
        self.now += 1801
        result = self.request(Plan('deliveries', {'center': 'guro'}, True))
        self.assertEqual(result.data['missing'], ['date', 'status'])

    def test_draft_asks_only_missing_fields(self):
        first = self.request(Plan('deliveries', {'date': '2026-11-16'}))
        self.assertEqual(first.data['missing'], ['center', 'status'])
        second = self.request(Plan('deliveries', {'center': 'guro'}, True))
        self.assertEqual(second.data['missing'], ['status'])
        final = self.request(Plan('deliveries', {'status': 'pending'}, True))
        self.assertEqual(final.status, 'ok')
        self.assertEqual(len(self.calls), 1)

    def test_different_tool_clears_context(self):
        self.request(self.full_read())
        self.preview()
        result = self.request(Plan('deliveries', {'center': 'guro'}, True))
        self.assertEqual(result.data['missing'], ['date', 'status'])

    def test_completed_query_carries_only_registered_context(self):
        self.registry.register(Tool('limited-context', self.fields, self.read, enabled=True,
                                    context_fields=frozenset({'date'})))
        self.request(Plan('limited-context', self.full_read().arguments))
        result = self.request(Plan('limited-context', {}, True))
        self.assertEqual(result.data['missing'], ['center', 'status'])

    def test_reader_cannot_preview_write(self):
        result = self.request(Plan('stock', {'quantity': 4}))
        self.assertEqual(result.status, 'denied')
        self.assertEqual(self.calls, [])

    def test_write_requires_explicit_confirmation(self):
        result = self.preview()
        self.assertEqual(result.status, 'preview')
        self.assertEqual(self.row['quantity'], 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(result.data['snapshot']['preview'], {'before': 2, 'after': 4})

    def test_confirmed_write_rechecks_and_proves_readback(self):
        preview = self.preview()
        result = self.request(principal=self.writer, confirm=preview.data['confirmation'])
        self.assertEqual(result.status, 'ok')
        self.assertEqual(self.row['quantity'], 4)
        self.assertEqual(len(self.applied_keys), 1)

    def test_role_revoked_before_confirmation_blocks_write(self):
        preview = self.preview()
        result = self.request(principal=self.reader, confirm=preview.data['confirmation'])
        self.assertEqual(result.status, 'denied')
        self.assertEqual(self.row['quantity'], 2)

    def test_other_employee_cannot_confirm(self):
        preview = self.preview()
        other = replace(self.writer, subject='employee-b')
        result = self.request(principal=other, confirm=preview.data['confirmation'])
        self.assertEqual(result.status, 'denied')
        self.assertEqual(self.calls, [])

    def test_other_tenant_cannot_confirm(self):
        preview = self.preview()
        result = self.request(principal=replace(self.writer, tenant='company-b'),
                              confirm=preview.data['confirmation'])
        self.assertEqual(result.status, 'denied')

    def test_other_channel_cannot_confirm(self):
        preview = self.preview()
        self.assertEqual(self.request(principal=self.writer, channel='kakao',
                                      confirm=preview.data['confirmation']).status, 'denied')

    def test_other_conversation_cannot_confirm(self):
        preview = self.preview()
        self.assertEqual(self.request(principal=self.writer, conversation='chat-2',
                                      confirm=preview.data['confirmation']).status, 'denied')

    def test_expired_confirmation_cannot_execute(self):
        preview = self.preview()
        self.now += 301
        result = self.request(principal=self.writer, confirm=preview.data['confirmation'])
        self.assertEqual(result.status, 'expired')
        self.assertEqual(self.calls, [])

    def test_changed_order_blocks_confirmation(self):
        preview = self.preview()
        self.row['version'] = 'new-order-same-row'
        self.row['quantity'] = 9
        result = self.request(principal=self.writer, confirm=preview.data['confirmation'])
        self.assertEqual(result.status, 'stale')
        self.assertEqual(self.row['quantity'], 9)
        self.assertEqual(self.calls, [])

    def test_changed_preview_even_same_version_blocks_confirmation(self):
        preview = self.preview()
        self.row['quantity'] = 8
        result = self.request(principal=self.writer, confirm=preview.data['confirmation'])
        self.assertEqual(result.status, 'stale')
        self.assertEqual(self.calls, [])

    def test_new_task_cancels_previous_preview(self):
        preview = self.preview()
        self.request(self.full_read())
        result = self.request(principal=self.writer, confirm=preview.data['confirmation'])
        self.assertEqual(result.status, 'expired')

    def test_duplicate_confirmation_new_request_does_not_write_twice(self):
        preview = self.preview()
        first = self.request(principal=self.writer, confirm=preview.data['confirmation'])
        second = self.request(principal=self.writer, confirm=preview.data['confirmation'])
        self.assertEqual(first.status, 'ok')
        self.assertEqual(second.status, 'expired')
        self.assertEqual(len(self.calls), 1)

    def test_same_request_replay_after_restart_no_provider_call(self):
        plan = self.full_read()
        first = self.request(plan, request='same-request')
        self.core = Core(Store(self.path, clock=lambda: self.now), self.registry)
        second = self.request(plan, request='same-request')
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)

    def test_write_replay_remains_blocked_after_purge(self):
        preview = self.preview()
        first = self.request(principal=self.writer, request='mutation-request', confirm=preview.data['confirmation'])
        self.now += 90000
        self.store.purge()
        self.core = Core(Store(self.path, clock=lambda: self.now), self.registry)
        second = self.request(principal=self.writer, request='mutation-request', confirm=preview.data['confirmation'])
        self.assertEqual(first.status, 'ok')
        self.assertEqual(second.status, 'expired')
        self.assertEqual(len(self.calls), 1)

    def test_expired_result_payload_erased_but_receipt_remains(self):
        self.request(self.full_read(), request='retention-test')
        self.now += 90000
        self.store.purge()
        with self.store.db() as c:
            row = c.execute("SELECT fingerprint,response,tool FROM requests WHERE request='retention-test'").fetchone()
            self.assertIsNone(row['response'])
            self.assertEqual(len(row['fingerprint']), 64)
            self.assertEqual(row['tool'], 'deliveries')

    def test_unknown_intervening_business_resets_context(self):
        self.request(self.full_read())
        self.request(Plan('another-business'))
        result = self.request(Plan('deliveries', {'center':'guro'}, True))
        self.assertEqual(result.status,'clarify')
        self.assertEqual(set(result.data['missing']), {'date','status'})

    def test_unknown_business_cancels_pending_confirmation(self):
        preview = self.preview()
        self.request(Plan('another-business'))
        result = self.request(principal=self.writer, confirm=preview.data['confirmation'])
        self.assertEqual(result.status,'expired')
        self.assertEqual(self.calls, [])

    def test_bad_arguments_clear_previous_context(self):
        self.request(self.full_read())
        self.request(Plan('deliveries', {'unregistered':'field'}))
        result = self.request(Plan('deliveries', {'center':'guro'}, True))
        self.assertEqual(result.status,'clarify')
        self.assertEqual(set(result.data['missing']), {'date','status'})

    def test_consumed_preview_does_not_keep_raw_arguments(self):
        preview = self.preview()
        self.request(principal=self.writer, confirm=preview.data['confirmation'])
        with self.store.db() as c:
            row = c.execute('SELECT arguments,snapshot FROM previews WHERE id=?',(preview.data['confirmation'],)).fetchone()
            self.assertEqual(row['arguments'],'{}')
            self.assertEqual(row['snapshot'],'{}')

    def test_cached_result_denied_when_tool_disabled(self):
        self.request(self.full_read(), request='old-read')
        self.core.registry._tools['deliveries'] = replace(self.registry.get('deliveries'), enabled=False)
        result = self.request(self.full_read(), request='old-read')
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(result.data, {})

    def test_request_id_content_change_rejected(self):
        self.request(self.full_read(), request='request-fixed')
        with self.assertRaises(CoreError):
            self.request(self.full_read(center='guro'), request='request-fixed')
        self.assertEqual(len(self.calls), 1)

    def test_request_id_other_scope_rejected(self):
        self.request(self.full_read(), request='request-fixed')
        with self.assertRaises(CoreError):
            self.request(self.full_read(), request='request-fixed', channel='kakao')

    def test_concurrent_duplicate_read_executes_once(self):
        entered, release = threading.Event(), threading.Event()
        def slow(principal, args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test timeout')
            return self.read(principal, args)
        self.registry.register(Tool('slow-read', self.fields, slow, enabled=True))
        plan = Plan('slow-read', self.full_read().arguments)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.core.handle, self.reader, 'erp', 'chat-1', 'concurrent', plan=plan)
            self.assertTrue(entered.wait(5))
            duplicate = self.core.handle(self.reader, 'erp', 'chat-1', 'concurrent', plan=plan)
            self.assertEqual(duplicate.status, 'uncertain')
            busy = self.core.handle(self.reader, 'erp', 'chat-1', 'different-request', plan=plan)
            self.assertEqual(busy.status, 'busy')
            release.set()
            self.assertEqual(first.result().status, 'ok')
        self.assertEqual(len(self.calls), 1)

    def test_raised_provider_is_uncertain_and_not_blindly_retried(self):
        calls = []
        def broken(*args):
            calls.append(True)
            raise TimeoutError('may have executed')
        self.registry.register(Tool('broken', {}, broken, enabled=True))
        first = self.request(Plan('broken'), request='uncertain-request')
        second = self.request(Plan('broken'), request='uncertain-request')
        self.assertEqual(first.status, 'uncertain')
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_provider_plain_success_string_is_not_success(self):
        self.registry.register(Tool('claims', {}, lambda *_: '등록 완료', enabled=True))
        self.assertEqual(self.request(Plan('claims')).status, 'uncertain')

    def test_write_without_applied_proof_is_uncertain(self):
        self.registry.register(Tool('no-proof', {}, lambda *_a, **_k: Result('ok', {}, self.evidence()),
                                    kind='write', roles=frozenset({'writer'}), enabled=True,
                                    prepare=lambda *_: Snapshot('order-1', 'v1', {})))
        preview = self.request(Plan('no-proof'), principal=self.writer)
        self.assertEqual(self.request(principal=self.writer, confirm=preview.data['confirmation']).status, 'uncertain')

    def test_write_without_readback_is_uncertain(self):
        self.registry.register(Tool('no-readback', {}, lambda *_a, **_k: Result('ok', {}, self.evidence(), applied=True),
                                    kind='write', roles=frozenset({'writer'}), enabled=True,
                                    prepare=lambda *_: Snapshot('order-1', 'v1', {})))
        preview = self.request(Plan('no-readback'), principal=self.writer)
        self.assertEqual(self.request(principal=self.writer, confirm=preview.data['confirmation']).status, 'uncertain')

    def test_unregistered_arguments_do_not_reach_provider(self):
        result = self.request(Plan('deliveries', {**self.full_read().arguments, 'admin': True}))
        self.assertEqual(result.status, 'uncertain')
        self.assertEqual(self.calls, [])

    def test_invalid_field_enum_never_reaches_provider(self):
        self.assertEqual(self.request(self.full_read(status='cancel-everything')).status, 'uncertain')
        self.assertEqual(self.calls, [])

    def test_boolean_cannot_be_quantity(self):
        self.assertEqual(self.request(Plan('stock', {'quantity': True}), principal=self.writer).status, 'uncertain')
        self.assertEqual(self.calls, [])

    def test_model_plan_cannot_set_confirmation_or_roles(self):
        with self.assertRaises(TypeError):
            Plan('stock', {'quantity': 4}, confirm='approval')
        with self.assertRaises(TypeError):
            Plan('stock', {'quantity': 4}, roles=['admin'])

    def test_non_principal_rejected(self):
        with self.assertRaises(AccessDenied):
            self.core.handle({'roles': ['admin']}, 'erp', 'chat', 'request-1', plan=self.full_read())

    def test_plan_and_confirmation_cannot_be_combined(self):
        with self.assertRaises(CoreError):
            self.request(self.full_read(), confirm='confirmation')

    def test_attachment_requires_explicit_consent(self):
        for consent in (False, None, 1, 'yes'):
            with self.subTest(consent=consent), self.assertRaises(AccessDenied):
                Attachment('image', b'fixture', consent)

    def test_attachment_image_audio_only(self):
        for kind in ('video', 'url', 'script'):
            with self.subTest(kind=kind), self.assertRaises(CoreError):
                Attachment(kind, b'fixture', True)
        for kind in ('image', 'audio'):
            self.assertEqual(Attachment(kind, b'fixture', True).content, b'fixture')

    def test_attachment_rejects_empty_text_and_oversized(self):
        for value in (b'', 'https://example.com/image', b'x' * (10 * 1024 * 1024 + 1)):
            with self.subTest(size=len(value)), self.assertRaises(CoreError):
                Attachment('image', value, True)

    def test_result_cannot_claim_success_without_evidence(self):
        with self.assertRaises(CoreError):
            Result('ok')
        with self.assertRaises(CoreError):
            Result('empty', count=0)

    def test_result_zero_requires_empty_and_boolean_not_count(self):
        with self.assertRaises(CoreError):
            Result('ok', evidence=self.evidence(), count=0)
        with self.assertRaises(CoreError):
            Result('ok', evidence=self.evidence(), count=True)

    def test_writes_cannot_grant_reader_role(self):
        with self.assertRaises(CoreError):
            Tool('unsafe-write', {}, self.write, kind='write', prepare=self.prepare)

    def test_cached_read_rechecks_current_roles(self):
        self.registry.register(Tool('private-read', {}, lambda *_: Result('ok', {'sensitive': 'fixture'}, self.evidence(), 1),
                                    roles=frozenset({'writer'}), enabled=True))
        first = self.request(Plan('private-read'), principal=self.writer, request='private-request')
        self.assertEqual(first.status, 'ok')
        replay = self.request(Plan('private-read'), principal=self.reader, request='private-request')
        self.assertEqual(replay.status, 'denied')
        self.assertNotIn('sensitive', str(replay.data))

    def test_cached_confirmation_rechecks_current_roles(self):
        preview = self.preview()
        first = self.request(principal=self.writer, request='written-request', confirm=preview.data['confirmation'])
        self.assertEqual(first.status, 'ok')
        replay = self.request(principal=self.reader, request='written-request', confirm=preview.data['confirmation'])
        self.assertEqual(replay.status, 'denied')
        self.assertEqual(len(self.calls), 1)

    def test_changed_tool_version_invalidates_preview(self):
        preview = self.preview()
        new_registry = Registry()
        new_registry.register(replace(self.registry.get('stock'), version='v2'))
        self.core = Core(self.store, new_registry)
        result = self.request(principal=self.writer, confirm=preview.data['confirmation'])
        self.assertEqual(result.status, 'stale')
        self.assertEqual(self.calls, [])

    def test_provider_failure_after_write_is_not_retried(self):
        calls = []
        def uncertain_write(*_args, **_kwargs):
            calls.append(True)
            self.row['quantity'] = 4
            raise TimeoutError('write may have happened')
        self.registry.register(Tool('timeout-write', {}, uncertain_write, kind='write',
                                    roles=frozenset({'writer'}), enabled=True,
                                    prepare=lambda *_: Snapshot('order-1', 'v1', {})))
        preview = self.request(Plan('timeout-write'), principal=self.writer)
        first = self.request(principal=self.writer, request='timeout-execution', confirm=preview.data['confirmation'])
        replay = self.request(principal=self.writer, request='timeout-execution', confirm=preview.data['confirmation'])
        new_attempt = self.request(principal=self.writer, confirm=preview.data['confirmation'])
        self.assertEqual(first.status, 'uncertain')
        self.assertEqual(first, replay)
        self.assertEqual(new_attempt.status, 'expired')
        self.assertEqual(calls, [True])
        self.assertEqual(self.row['quantity'], 4)

    def test_database_private_permissions(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_symlink_database_rejected(self):
        link = Path(self.tmp.name) / 'link.sqlite3'
        link.symlink_to(self.path)
        with self.assertRaises(CoreError):
            Store(link)

    def test_invalid_ttl_and_clock(self):
        with self.assertRaises(CoreError):
            Store(self.path, ttl=0)
        self.store.clock = lambda: float('nan')
        with self.assertRaises(CoreError):
            self.store.now()

    def test_purge_erases_expired_draft_content(self):
        self.request(Plan('deliveries', {'date': '2026-11-16'}))
        self.now += 1801
        self.store.purge()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM turns').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM requests').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
