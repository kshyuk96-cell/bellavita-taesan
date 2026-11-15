from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from manager_bridge.manager_host import HostRejected, PROJECTS, main, open_host, prepare_host
from manager_bridge.maintenance import main as bootstrap
from manager_bridge.maintenance_registry import Registry
from manager_bridge.store import Store, token_hash


class ManagerHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory())
        self.root=Path(self.tmp)
        self.source=self.root/'source';self.source.mkdir()
        (self.source/'README').write_text('synthetic review host source\n')
        env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1'}
        def git(*args):
            return subprocess.check_output(['/usr/bin/git','-c','core.hooksPath=/dev/null','-C',str(self.source),*args],env=env,stderr=subprocess.DEVNULL,text=True).strip()
        git('init');git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','Fixture')
        self.sha=git('rev-parse','HEAD')
        self.persistent=self.root/'persistent';self.persistent.mkdir(mode=0o700)
        self.data=self.persistent/'manager'
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap(['--data',str(self.data),'init']),0)
        self.owner=(self.data/'owner.token').read_text().strip()
        self.manager=(self.data/'manager.token').read_text().strip()
        self.kwargs=dict(expected_commit=self.sha,public_origin='https://manager.example.invalid',source_root=self.source,persistent_root=self.persistent)

    def prepare(self,**changes):
        args={**self.kwargs,**changes}
        return prepare_host(self.data,**args)

    def test_accepts_existing_private_bootstrap_without_changing_bytes(self):
        before={p.name:p.read_bytes() for p in self.data.iterdir() if p.is_file()}
        result=self.prepare()
        self.assertEqual(result['projects'],sorted(PROJECTS))
        self.assertTrue(result['bootstrap_verified'])
        self.assertFalse(result['execution_enabled'])
        self.assertFalse(result['operational_readiness'])
        self.assertFalse(result['restart_persistence_verified'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.data.iterdir() if p.is_file()})
        self.assertNotIn(self.owner,json.dumps(result));self.assertNotIn(self.manager,json.dumps(result))

    def test_missing_bootstrap_is_not_created(self):
        missing=self.persistent/'absent'
        with self.assertRaises(HostRejected):prepare_host(missing,**self.kwargs)
        self.assertFalse(missing.exists())

    def test_source_pin_mismatch_or_symbolic_head_rejected(self):
        for value in ('a'*40,'HEAD','main',''):
            if value==self.sha:continue
            with self.subTest(pin=value),self.assertRaises(HostRejected):self.prepare(expected_commit=value)

    def test_dirty_source_cannot_boot(self):
        (self.source/'README').write_text('unreviewed changes')
        with self.assertRaises(HostRejected):self.prepare()

    def test_source_symlink_cannot_boot(self):
        alias=self.root/'source-alias';alias.symlink_to(self.source,target_is_directory=True)
        with self.assertRaises(HostRejected):self.prepare(source_root=alias)

    def test_data_directory_symlink_rejected(self):
        alias=self.persistent/'alias';alias.symlink_to(self.data,target_is_directory=True)
        with self.assertRaises(HostRejected):prepare_host(alias,**self.kwargs)

    def test_database_symlink_rejected_before_open(self):
        file=self.data/'maintenance.sqlite3';target=self.data/'protected.sqlite3'
        file.rename(target);file.symlink_to(target)
        before=target.read_bytes()
        with self.assertRaises(HostRejected):self.prepare()
        self.assertEqual(before,target.read_bytes())

    def test_insecure_directory_rejected(self):
        self.data.chmod(0o755)
        with self.assertRaises(HostRejected):self.prepare()

    def test_public_bootstrap_file_rejected(self):
        for filename in ('owner.token','manager.token','maintenance.sqlite3'):
            with self.subTest(filename=filename):
                path=self.data/filename;path.chmod(0o644)
                with self.assertRaises(HostRejected):self.prepare()
                path.chmod(0o600)

    def test_token_symlink_rejected(self):
        file=self.data/'manager.token';target=self.data/'other.token'
        file.rename(target);file.symlink_to(target)
        with self.assertRaises(HostRejected):self.prepare()

    def test_revoked_manager_profile_rejected(self):
        Store(self.data/'maintenance.sqlite3').revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(HostRejected):self.prepare()

    def test_expired_manager_profile_rejected(self):
        with sqlite3.connect(self.data/'maintenance.sqlite3') as c:
            c.execute('UPDATE bridge_tokens SET expires=1 WHERE token_hash=?',(token_hash(self.manager),))
        with self.assertRaises(HostRejected):self.prepare()

    def test_malformed_manager_scope_cannot_masquerade_as_project_list(self):
        with sqlite3.connect(self.data/'maintenance.sqlite3') as c:
            c.execute('UPDATE bridge_tokens SET projects=? WHERE token_hash=?',(json.dumps({p:True for p in PROJECTS}),token_hash(self.manager)))
        with self.assertRaises(HostRejected):self.prepare()

    def test_wrong_manager_role_rejected(self):
        worker=Store(self.data/'maintenance.sqlite3').add_token('worker','worker',['bellavita-erp'])
        (self.data/'manager.token').write_text(worker+'\n')
        with self.assertRaises(HostRejected):self.prepare()

    def test_manager_cannot_have_unreviewed_extra_project_scope(self):
        store=Store(self.data/'maintenance.sqlite3')
        bootstrap_owner=store.add_token('fixture-bootstrap-owner','owner',['*'])
        store.register_project(bootstrap_owner,'extra','fixture/extra')
        token=store.add_token('wider','manager',list(PROJECTS)+['extra'])
        (self.data/'manager.token').write_text(token+'\n')
        with self.assertRaises(HostRejected):self.prepare()

    def test_incomplete_database_does_not_receive_auto_migrations(self):
        path=self.data/'maintenance.sqlite3';path.unlink()
        with sqlite3.connect(path) as c:c.execute('CREATE TABLE placeholder (n INTEGER)')
        path.chmod(0o600);before=path.read_bytes()
        with self.assertRaises(HostRejected):self.prepare()
        self.assertEqual(path.read_bytes(),before)

    def test_review_host_rejects_active_execution_flag(self):
        store=Store(self.data/'maintenance.sqlite3');registry=Registry(store)
        config=registry.get(self.owner,'bellavita-erp')
        registry.register(self.owner,'bellavita-erp',{**config['config'],'maintenance_enabled':True},expected_revision=config['revision'])
        with self.assertRaises(HostRejected):self.prepare()

    def test_repository_binding_mismatch_rejected(self):
        with sqlite3.connect(self.data/'maintenance.sqlite3') as c:
            c.execute("UPDATE bridge_projects SET repository='fixture/different' WHERE project='bellavita-shop'")
        with self.assertRaises(HostRejected):self.prepare()

    def test_invalid_origins_rejected(self):
        for origin in ('http://manager.example.invalid','https://user:password@example.invalid','https://example.invalid/path','https://example.invalid?key=value','https://example.invalid#fragment','https://example.invalid\n','https://example.invalid:99999'):
            with self.subTest(origin=origin),self.assertRaises(HostRejected):self.prepare(public_origin=origin)

    def test_data_outside_declared_persistent_root_rejected(self):
        other=self.root/'other';other.mkdir(mode=0o700)
        with self.assertRaises(HostRejected):self.prepare(persistent_root=other)

    def test_legacy_database_requires_explicit_selection_not_fallback_or_copy(self):
        (self.data/'maintenance.sqlite3').rename(self.data/'manager.sqlite3')
        with self.assertRaises(FileNotFoundError):self.prepare()
        self.assertFalse((self.data/'maintenance.sqlite3').exists())
        self.assertEqual(self.prepare(database='manager.sqlite3')['database'],'manager.sqlite3')

    def test_arbitrary_database_path_rejected(self):
        with self.assertRaises(HostRejected):self.prepare(database='../other.sqlite3')

    def arguments(self):
        return ['--data',str(self.data),'--persistent-root',str(self.persistent),'--source-root',str(self.source),'--expected-commit',self.sha,'--public-origin','https://manager.example.invalid']

    def test_default_cli_only_checks_without_listener(self):
        stream=io.StringIO()
        with redirect_stdout(stream),patch('manager_bridge.server.make_server') as create:
            code=main(self.arguments())
        self.assertEqual(code,0);create.assert_not_called()
        self.assertFalse(json.loads(stream.getvalue())['accounts_generated'])

    def test_bad_port_environment_is_sanitized(self):
        stream=io.StringIO()
        with redirect_stdout(stream),patch.dict(os.environ,{'PORT':'invalid-private-value'}):code=main(self.arguments())
        self.assertEqual(code,1)
        self.assertNotIn('invalid-private-value',stream.getvalue())

    def test_open_host_passes_explicit_db_to_existing_server_without_callbacks(self):
        fake=SimpleNamespace()
        with patch('manager_bridge.server.make_server',return_value=fake) as create:
            server=open_host(self.data,port=0,**self.kwargs)
        self.assertIs(server,fake)
        self.assertEqual(create.call_args.args[0].path,self.data/'maintenance.sqlite3')
        self.assertFalse(server.manager_host_plan['execution_enabled'])
        self.assertFalse(hasattr(server,'agent_connectors'))
