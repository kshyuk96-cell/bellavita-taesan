import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from google.oauth2.credentials import Credentials

import script_project_access as scripts


PROD = 'fixture_production_script_1234567890'
COPY = 'fixture_copy_script_1234567890123456'
ENV = {'TAESAN_SCRIPT_PROJECT_ID': PROD, 'TAESAN_SCRIPT_TEST_PROJECT_ID': COPY}
FILES = [{'name': 'Code', 'type': 'SERVER_JS', 'source': 'function run(){return 1;}'} ,
         {'name': 'Unrelated', 'type': 'SERVER_JS', 'source': 'function preserved(){return 3;}'} ,
         {'name': 'Page', 'type': 'HTML', 'source': '<p>original fixture</p>'},
         {'name': 'appsscript', 'type': 'JSON', 'source': '{"timeZone":"Asia/Seoul","oauthScopes":[]}'}]
PATCH = [{'name': 'Code', 'type': 'SERVER_JS', 'source': 'function run(){return 2;}'}]


class Response:
    def __init__(self, payload=None, status=200):
        self.payload, self.status_code, self.history = payload, status, []
        self.closed = False
    def json(self):
        return copy.deepcopy(self.payload)
    def close(self):
        self.closed = True


class Session:
    def __init__(self, target=COPY):
        self.credentials = Credentials(token='fixture-token-never-sent', scopes=[scripts.PROJECT_SCOPE])
        self.target = target
        self.files = copy.deepcopy(FILES)
        self.events = []
        self.put_failure = None
        self.get_failure = None
        self.mismatch_after_put = False
        self.wrote = False
    def get(self, url, **kwargs):
        self.events.append(('get', url, kwargs))
        if self.get_failure:
            raise self.get_failure
        files = copy.deepcopy(self.files)
        if self.wrote and self.mismatch_after_put:
            files[1]['source'] += '// concurrent edit'
        return Response({'scriptId': self.target, 'files': files})
    def put(self, url, **kwargs):
        self.events.append(('put', url, copy.deepcopy(kwargs)))
        if self.put_failure:
            raise self.put_failure
        self.files = copy.deepcopy(kwargs['json']['files'])
        self.wrote = True
        return Response({'scriptId': self.target, 'files': self.files})


def approval(plan):
    return {**copy.deepcopy(plan['binding']), 'approved': True, 'approved_by': 'owner-fixture', 'approval_id': 'approval-fixture'}


def backup(snapshot):
    return dict(project_id=snapshot['project_id'], source_digest=snapshot['source_digest'],
                backup_id='durable-fixture-backup', durable=True, outside_git=True)


class ScriptProjectAccessTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.role = Mock(return_value='admin')
        self.backup = Mock(side_effect=backup)

    def prepare(self, environment='copy', patch=PATCH):
        return scripts.prepare_change(environment, patch, scripts.source_digest(FILES),
            env=ENV, session=self.session, actor='owner-fixture', role_lookup=self.role)

    def apply(self, plan, **overrides):
        args = dict(env=ENV, session=self.session, actor='owner-fixture', role_lookup=self.role,
                    verify_approval=lambda receipt: True, backup=self.backup)
        args.update(overrides)
        return scripts.apply_change(plan, approval(plan), **args)

    def test_exact_configured_copy_patch_preserves_every_other_file_and_durable_backup_precedes_put(self):
        plan = self.prepare()
        self.assertFalse(plan['binding']['manifest_change'])
        def durable(snapshot):
            self.session.events.append(('backup',))
            with tempfile.TemporaryDirectory() as folder:
                # Real temporary test backup; production callback must fsync and
                # retain its actual outside-Git durable file until rollback expiry.
                file = Path(folder) / 'source-backup.json'
                file.write_text(json.dumps(snapshot))
                self.assertEqual(json.loads(file.read_text())['files'], FILES)
            return backup(snapshot)
        result = self.apply(plan, backup=durable)
        self.assertEqual(result['status'], 'source_verified')
        self.assertEqual(self.session.files[0]['source'], PATCH[0]['source'])
        self.assertEqual(self.session.files[1:], FILES[1:])
        operations = [event[0] for event in self.session.events]
        self.assertLess(operations.index('backup'), operations.index('put'))
        self.assertFalse(result['execution_requested'])
        self.assertFalse(result['deployment_requested'])
        self.assertFalse(result['trigger_configuration_changed'])
        self.assertFalse(result['trigger_effects_verified'])
        for event in self.session.events:
            if event[0] in ('get', 'put'):
                self.assertIn('/projects/' + COPY + '/content', event[1])
                self.assertFalse(event[2]['allow_redirects'])

    def test_arbitrary_or_same_copy_production_target_is_refused_before_network(self):
        for env, selector in (({}, 'copy'), (ENV, PROD),
                              ({'TAESAN_SCRIPT_PROJECT_ID': PROD, 'TAESAN_SCRIPT_TEST_PROJECT_ID': PROD}, 'copy')):
            with self.subTest(selector=selector), self.assertRaises(scripts.SourceAccessError):
                scripts.read_source(selector, env=env, session=self.session, actor='owner-fixture', role_lookup=self.role)
        self.assertEqual(self.session.events, [])

    def test_service_account_unknown_and_scope_missing_credentials_are_rejected(self):
        for credentials in (SimpleNamespace(service_account_email='fixture@project.iam.gserviceaccount.com'),
                            object(), Credentials(token='fixture-only', scopes=['https://www.googleapis.com/auth/spreadsheets'])):
            self.session.credentials = credentials
            with self.subTest(credentials=type(credentials)), self.assertRaises(scripts.SourceAccessError):
                self.prepare()
        self.assertEqual(self.session.events, [])

    def test_staff_and_revoked_role_before_write_cannot_edit(self):
        self.role.return_value = 'staff'
        with self.assertRaises(PermissionError):
            self.prepare()
        self.role.return_value = 'admin'
        plan = self.prepare()
        self.role.side_effect = ['admin', 'admin', None]
        with self.assertRaises(PermissionError):
            self.apply(plan)
        self.assertFalse(any(event[0] == 'put' for event in self.session.events))

    def test_changed_baseline_or_unknown_file_patch_does_not_overwrite_project(self):
        self.session.files[1]['source'] += '// another developer'
        with self.assertRaises(scripts.SourceAccessError):
            self.prepare()
        self.session.files = copy.deepcopy(FILES)
        with self.assertRaises(scripts.SourceAccessError):
            self.prepare(patch=[{'name': 'NewFile', 'type': 'SERVER_JS', 'source': 'new'}])
        plan = self.prepare()
        self.session.files[1]['source'] += '// another developer'
        with self.assertRaises(scripts.SourceAccessError):
            self.apply(plan)
        self.assertFalse(any(event[0] == 'put' for event in self.session.events))

    def test_backup_failure_non_durable_or_inside_git_receipt_blocks_before_put(self):
        plan = self.prepare()
        for callback in (Mock(side_effect=RuntimeError('private backup path')), lambda snapshot: None,
                         lambda snapshot: {**backup(snapshot), 'durable': False},
                         lambda snapshot: {**backup(snapshot), 'outside_git': False},
                         lambda snapshot: {**backup(snapshot), 'source_digest': '0' * 64}):
            with self.subTest(callback=type(callback)), self.assertRaises(scripts.SourceAccessError):
                self.apply(plan, backup=callback)
        self.assertFalse(any(event[0] == 'put' for event in self.session.events))

    def test_edit_or_approval_revocation_during_backup_is_rechecked(self):
        plan = self.prepare()
        def concurrent_edit(snapshot):
            self.session.files[1]['source'] += '// edit during backup'
            return backup(snapshot)
        with self.assertRaises(scripts.SourceAccessError):
            self.apply(plan, backup=concurrent_edit)
        self.session.files = copy.deepcopy(FILES)
        verifier = Mock(side_effect=[True, False])
        with self.assertRaises(scripts.SourceAccessError):
            self.apply(plan, verify_approval=verifier)
        self.assertFalse(any(event[0] == 'put' for event in self.session.events))

    def test_exact_approval_binding_and_actual_verifier_are_mandatory(self):
        plan = self.prepare()
        for key, value in (('project_id', PROD), ('baseline_digest', '0' * 64), ('patch_digest', '0' * 64),
                           ('after_digest', '0' * 64), ('approved_by', 'staff'), ('approved', 1)):
            receipt = approval(plan)
            receipt[key] = value
            with self.subTest(key=key), self.assertRaises(scripts.SourceAccessError):
                scripts.apply_change(plan, receipt, env=ENV, session=self.session, actor='owner-fixture',
                    role_lookup=self.role, verify_approval=lambda _: True, backup=self.backup)
        with self.assertRaises(scripts.SourceAccessError):
            self.apply(plan, verify_approval=lambda _: 1)
        self.backup.assert_not_called()

    def test_production_requires_actual_bound_passing_copy_test_for_full_candidate(self):
        self.session.target = PROD
        plan = self.prepare('production')
        with self.assertRaises(scripts.SourceAccessError):
            self.apply(plan)
        receipt = dict(status='passed', copy_project_id=COPY, patch_digest=plan['binding']['patch_digest'],
                       after_digest=plan['binding']['after_digest'], checks=['actual-copy-execution-fixture'], report_digest='a' * 64,
                       trigger_effects_reviewed=True)
        for key, value in (('status', 'failed'), ('copy_project_id', PROD),
                           ('patch_digest', '0' * 64), ('after_digest', '0' * 64), ('checks', []),
                           ('trigger_effects_reviewed', False)):
            invalid = {**receipt, key: value}
            with self.subTest(key=key), self.assertRaises(scripts.SourceAccessError):
                self.apply(plan, copy_receipt=invalid, verify_copy_receipt=lambda *_: True)
        with self.assertRaises(scripts.SourceAccessError):
            self.apply(plan, copy_receipt=receipt, verify_copy_receipt=lambda *_: False)
        self.assertEqual(self.apply(plan, copy_receipt=receipt, verify_copy_receipt=lambda *_: True)['status'], 'source_verified')

    def test_owner_explicit_manifest_change_flag_is_required_and_source_does_not_grant_consent(self):
        patch = [{'name': 'appsscript', 'type': 'JSON', 'source': '{"timeZone":"Asia/Seoul","oauthScopes":["new-scope-fixture"]}'}]
        plan = self.prepare(patch=patch)
        self.assertTrue(plan['binding']['manifest_change'])
        receipt = approval(plan)
        receipt['manifest_change'] = False
        with self.assertRaises(scripts.SourceAccessError):
            scripts.apply_change(plan, receipt, env=ENV, session=self.session, actor='owner-fixture',
                role_lookup=self.role, verify_approval=lambda _: True, backup=self.backup)
        result = self.apply(plan)
        self.assertEqual(result['scope'], 'source_only')
        self.assertIn('OAuth 동의', result['summary'])

    def test_put_timeout_or_readback_mismatch_is_uncertain_with_no_retry(self):
        for mode in ('timeout', 'mismatch'):
            self.session = Session()
            plan = self.prepare()
            if mode == 'timeout':
                self.session.put_failure = TimeoutError('private token / source / URL')
            else:
                self.session.mismatch_after_put = True
            result = self.apply(plan)
            self.assertEqual(result['status'], 'uncertain')
            self.assertEqual(sum(event[0] == 'put' for event in self.session.events), 1)
            self.assertNotIn('private token', json.dumps(result))
            self.assertFalse(result['execution_requested'])

    def test_provider_wrong_project_or_false_approval_never_leaks_source(self):
        self.session.target = PROD
        with self.assertRaises(scripts.SourceAccessError) as failure:
            self.prepare()
        self.assertEqual(failure.exception.code, 'source_target_unverified')
        self.assertNotIn('function', str(failure.exception))

    def test_full_digest_covers_unmodified_sources_but_not_transient_provider_metadata(self):
        changed = copy.deepcopy(FILES)
        changed[1]['source'] += '// changed'
        self.assertNotEqual(scripts.source_digest(changed), scripts.source_digest(FILES))
        metadata = copy.deepcopy(FILES)
        metadata[0]['updateTime'] = 'transient-provider-time'
        self.assertEqual(scripts.source_digest(metadata), scripts.source_digest(list(reversed(FILES))))

    def test_copy_receipt_revoked_during_backup_prevents_production_put(self):
        self.session.target = PROD
        plan = self.prepare('production')
        receipt = dict(status='passed', copy_project_id=COPY, patch_digest=plan['binding']['patch_digest'],
                       after_digest=plan['binding']['after_digest'], checks=['actual-copy-trigger-review'],
                       report_digest='a' * 64, trigger_effects_reviewed=True)
        with self.assertRaises(scripts.SourceAccessError):
            self.apply(plan, copy_receipt=receipt, verify_copy_receipt=Mock(side_effect=[True, False]))
        self.assertFalse(any(event[0] == 'put' for event in self.session.events))

    def test_role_revoked_after_real_put_and_reread_is_not_reported_as_verified(self):
        plan = self.prepare()
        def role(_):
            return None if self.session.wrote else 'admin'
        result = self.apply(plan, role_lookup=role)
        self.assertEqual(result['status'], 'uncertain')
        self.assertFalse(result['trigger_effects_verified'])
        self.assertNotIn('executed', result)
        self.assertIn('기존 트리거', result['summary'])

    def test_own_concurrent_project_writers_are_serialized_and_stale_second_plan_is_blocked(self):
        shared = {'files': copy.deepcopy(FILES)}
        class SharedSession(Session):
            @property
            def files(self):
                return shared['files']
            @files.setter
            def files(self, value):
                shared['files'] = value
        first, second = SharedSession(), SharedSession()
        self.session = first
        plan1 = self.prepare()
        self.session = second
        plan2 = self.prepare(patch=[{**PATCH[0], 'source': 'function run(){return 999;}'}])
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        outcomes = {}
        def held_backup(snapshot):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture wait timeout')
            return backup(snapshot)
        def first_call():
            try:
                outcomes['first'] = self.apply(plan1, session=first, backup=held_backup)
            except Exception as error:
                outcomes['first_error'] = error
        def second_call():
            second_started.set()
            try:
                outcomes['second'] = self.apply(plan2, session=second)
            except scripts.SourceAccessError as error:
                outcomes['second_error'] = error.code
        one, two = threading.Thread(target=first_call), threading.Thread(target=second_call)
        one.start()
        self.assertTrue(entered.wait(3))
        two.start()
        self.assertTrue(second_started.wait(3))
        release.set()
        one.join(3)
        two.join(3)
        self.assertFalse(one.is_alive())
        self.assertFalse(two.is_alive())
        self.assertEqual(outcomes['first']['status'], 'source_verified')
        self.assertEqual(outcomes['second_error'], 'baseline_changed')
        self.assertEqual(sum(event[0] == 'put' for event in first.events + second.events), 1)

    def test_configured_user_oauth_secret_file_loads_without_provisioning_or_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'user-oauth.json'
            payload = dict(type='authorized_user', client_id='fixture-client', client_secret='fixture-secret',
                           refresh_token='fixture-refresh', token='fixture-token', scopes=[scripts.PROJECT_SCOPE])
            file.write_text(json.dumps(payload))
            file.chmod(0o600)
            factory = Mock(side_effect=lambda credentials: SimpleNamespace(credentials=credentials))
            session = scripts.configured_session({'TAESAN_SCRIPT_GOOGLE_CREDENTIALS': str(file)}, session_factory=factory)
            self.assertIsInstance(session.credentials, Credentials)
            self.assertIn(scripts.PROJECT_SCOPE, session.credentials.scopes)
            factory.assert_called_once()

    def test_missing_service_account_or_unconsented_oauth_file_never_reuses_sheet_credentials(self):
        factory = Mock()
        with self.assertRaises(scripts.SourceAccessError):
            scripts.configured_session({'TAESAN_GOOGLE_CREDENTIALS': '/private/sheet-service-account'}, session_factory=factory)
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'credentials.json'
            for payload in ({'type': 'service_account', 'private_key': 'fixture-never-log'},
                            {'type': 'authorized_user', 'scopes': ['https://www.googleapis.com/auth/spreadsheets']},
                            {'type': 'authorized_user'}):
                file.write_text(json.dumps(payload))
                file.chmod(0o600)
                with self.subTest(kind=payload.get('type')), self.assertRaises(scripts.SourceAccessError) as error:
                    scripts.configured_session({'TAESAN_SCRIPT_GOOGLE_CREDENTIALS': str(file)}, session_factory=factory)
                self.assertNotIn('fixture-never-log', str(error.exception))
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
