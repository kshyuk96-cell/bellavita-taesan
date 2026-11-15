from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from manager_bridge.maintenance_data import BackupMetadata, BackupSpec, DataChecks, MigrationSpec
from manager_bridge.maintenance_registry import Registry
from manager_bridge.store import Conflict, NotFound, PermissionDenied, Store


class DataCheckTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now=1000.0
        self.store=Store(self.root/'manager.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        self.config={'company':'bellavita','name':'가상 ERP','source_kind':'github','source_ref':'fixture/erp',
                     'branch':'main','services':[],'maintenance_enabled':False}
        self.registry.register(self.owner,'erp',self.config)
        self.registry.register(self.owner,'other',{**self.config,'company':'hope','source_ref':'fixture/other'})
        self.manager=self.store.add_token('manager','manager',['erp'])
        self.other=self.store.add_token('other','manager',['other'])
        self.worker=self.store.add_token('worker','worker',['erp'])
        self.path=self.root/'fixture.sqlite3'
        with sqlite3.connect(self.path) as c:
            c.executescript('CREATE TABLE orders(id INTEGER PRIMARY KEY,quantity INTEGER NOT NULL);INSERT INTO orders VALUES(13370,2);')
        data=self.path.read_bytes()
        self.metadata=BackupMetadata('bellavita','erp','backup-1','source-v1',hashlib.sha256(data).hexdigest(),len(data),False,900,2000,'fixture:backup-1',readable=True)
        self.spec=BackupSpec('erp','backup-1',lambda *_:self.metadata,str(self.path),True)
        self.migration=MigrationSpec('erp','add-center',('ALTER TABLE orders ADD COLUMN center TEXT',),{'orders':('id','quantity','center')})
        self.checks=DataChecks(self.store,self.registry,backups={('erp','backup-1'):self.spec},migrations={('erp','add-center'):self.migration})

    def plan(self,migration=None):
        return self.checks.prepare(self.manager,'erp','backup-1',migration)

    def approved(self,migration=None):
        plan=self.plan(migration)
        self.checks.approve(self.owner,plan['id'],plan['digest'])
        return plan

    def run_plan(self,migration=None):
        plan=self.approved(migration)
        return self.checks.run(self.manager,plan['id'],'request-1')

    def test_restore_only_actual_fixture_integrity_and_rollback(self):
        before=self.path.read_bytes()
        result=self.run_plan()
        self.assertEqual(result['state'],'passed')
        self.assertTrue(result['rollback_verified'])
        self.assertTrue(result['data_preserved'])
        self.assertEqual(result['table_counts'],{'orders':1})
        self.assertFalse(result['production_restore_performed'])
        self.assertEqual(self.path.read_bytes(),before)
        self.assertNotIn('13370',json.dumps(result))

    def test_additive_migration_preserves_rows_and_source(self):
        before=self.path.read_bytes()
        result=self.run_plan('add-center')
        self.assertEqual(result['state'],'passed')
        self.assertTrue(result['schema_matches'])
        self.assertNotEqual(result['before_schema_digest'],result['after_schema_digest'])
        self.assertEqual(self.path.read_bytes(),before)

    def test_missing_observer_not_measured(self):
        result=DataChecks(self.store,self.registry).inspect(self.manager,'erp')
        self.assertEqual(result['state'],'not_configured')
        self.assertEqual(result['backups'],[])

    def test_encryption_checksum_and_restore_are_independent(self):
        result=self.checks.inspect(self.manager,'erp')['backups'][0]
        self.assertFalse(result['encrypted'])
        self.assertTrue(result['checksum_recorded'])
        self.assertFalse(result['restore_verified'])
        self.metadata=replace(self.metadata,encrypted=True,restore_checked_at=950,restore_reference='fixture:restore')
        result=self.checks.inspect(self.manager,'erp')['backups'][0]
        self.assertTrue(result['encrypted']);self.assertTrue(result['restore_verified'])

    def test_expired_backup_not_ready_for_rehearsal(self):
        self.now=2001
        self.assertEqual(self.checks.inspect(self.manager,'erp')['backups'][0]['state'],'expired')
        with self.assertRaises(Conflict):self.plan()

    def test_unreadable_backup_fact_does_not_imply_rehearsal_ready(self):
        self.metadata=replace(self.metadata,readable=False)
        self.assertFalse(self.checks.inspect(self.manager,'erp')['backups'][0]['readable'])
        with self.assertRaises(Conflict):self.plan()

    def test_restore_before_backup_creation_not_verified(self):
        with self.assertRaises(ValueError):replace(self.metadata,restore_checked_at=850,restore_reference='fixture:restore')

    def test_future_observation_not_current(self):
        self.metadata=replace(self.metadata,created_at=1100)
        self.assertEqual(self.checks.inspect(self.manager,'erp')['backups'][0]['state'],'unknown')

    def test_cross_company_callback_rejected(self):
        self.metadata=replace(self.metadata,company='hope')
        self.assertEqual(self.checks.inspect(self.manager,'erp')['backups'][0]['state'],'permission_denied')
        with self.assertRaises(Conflict):self.plan()

    def test_provider_exceptions_and_secrets_not_exposed(self):
        def failed(*_):raise RuntimeError('private '+self.manager)
        spec=replace(self.spec,observe=failed)
        checks=DataChecks(self.store,self.registry,backups={('erp','backup-1'):spec})
        result=checks.inspect(self.manager,'erp')
        self.assertEqual(result['backups'][0]['state'],'unknown')
        self.assertNotIn(self.manager,str(result));self.assertNotIn('private',str(result))

    def test_credential_reference_rejected(self):
        self.metadata=replace(self.metadata,reference=self.manager)
        self.assertEqual(self.checks.inspect(self.manager,'erp')['backups'][0]['state'],'unknown')

    def test_operation_backup_metadata_cannot_restore(self):
        spec=BackupSpec('erp','backup-1',lambda *_:self.metadata)
        checks=DataChecks(self.store,self.registry,backups={('erp','backup-1'):spec})
        self.assertEqual(checks.inspect(self.manager,'erp')['state'],'observed')
        with self.assertRaises(Conflict):checks.prepare(self.manager,'erp','backup-1')
        with self.assertRaises(ValueError):BackupSpec('erp','backup-1',lambda *_:self.metadata,str(self.path))

    def test_cross_project_and_worker_cannot_inspect(self):
        for token in (self.other,self.worker):
            with self.subTest(token_role='scoped'),self.assertRaises((NotFound,PermissionDenied)):
                self.checks.inspect(token,'erp')

    def test_owner_approval_required(self):
        plan=self.plan()
        with self.assertRaises(Conflict):self.checks.run(self.manager,plan['id'],'request-1')
        with self.assertRaises(PermissionDenied):self.checks.approve(self.manager,plan['id'],plan['digest'])
        with self.assertRaises(Conflict):self.checks.approve(self.owner,plan['id'],'0'*64)

    def test_approval_does_not_authorize_operation(self):
        plan=self.plan()
        result=self.checks.approve(self.owner,plan['id'],plan['digest'])
        self.assertFalse(result['production_restore_authorized'])

    def test_changed_backup_bytes_after_approval_blocked(self):
        plan=self.approved()
        with sqlite3.connect(self.path) as c:c.execute('UPDATE orders SET quantity=3')
        result=self.checks.run(self.manager,plan['id'],'request-1')
        self.assertEqual(result['state'],'blocked')
        with sqlite3.connect(self.path) as c:self.assertEqual(c.execute('SELECT quantity FROM orders').fetchone()[0],3)

    def test_changed_metadata_after_approval_blocked(self):
        plan=self.approved()
        self.metadata=replace(self.metadata,source_snapshot='source-v2')
        self.assertEqual(self.checks.run(self.manager,plan['id'],'request-1')['state'],'blocked')

    def test_changed_migration_after_approval_blocked(self):
        plan=self.approved('add-center')
        changed=replace(self.migration,statements=('ALTER TABLE orders ADD COLUMN changed TEXT',))
        self.checks.migrations[('erp','add-center')]=changed
        self.assertEqual(self.checks.run(self.manager,plan['id'],'request-1')['state'],'blocked')

    def test_changed_configuration_requires_new_approval(self):
        plan=self.approved()
        self.registry.register(self.owner,'erp',{**self.config,'name':'변경'},expected_revision=1)
        with self.assertRaises(Conflict):self.checks.run(self.manager,plan['id'],'request-1')

    def test_mutated_source_during_prepare_not_accepted(self):
        self.path.write_bytes(b'not sqlite')
        with self.assertRaises(Conflict):self.plan()

    def test_symlink_path_cannot_read(self):
        alias=self.root/'alias.sqlite3';alias.symlink_to(self.path)
        self.checks.backups[('erp','backup-1')]=replace(self.spec,fixture_path=str(alias))
        with self.assertRaises(Conflict):self.plan()

    def test_live_sqlite_sidecars_are_not_a_self_contained_backup(self):
        sidecar=Path(str(self.path)+'-wal');sidecar.write_bytes(b'fixture journal')
        with self.assertRaises(Conflict):self.plan()

    def test_traversal_path_cannot_be_task_input(self):
        with self.assertRaises(ValueError):self.checks.prepare(self.manager,'erp','../backup-1')
        with self.assertRaises(ValueError):BackupSpec('erp','backup-1',lambda *_:self.metadata,'relative/path',True)

    def test_successful_execution_replayed_after_restart_without_restore(self):
        plan=self.approved()
        first=self.checks.run(self.manager,plan['id'],'request-1')
        self.path.unlink()
        reopened=DataChecks(self.store,self.registry,backups={('erp','backup-1'):self.spec})
        self.assertEqual(reopened.run(self.manager,plan['id'],'request-1'),first)
        with self.assertRaises(Conflict):reopened.run(self.manager,plan['id'],'request-2')

    def test_interrupted_run_not_replayed(self):
        plan=self.approved()
        with self.store._transaction() as c:
            c.execute("UPDATE maintenance_data_plans SET state='running',request_key='request-1' WHERE id=?",(plan['id'],))
        self.assertEqual(self.checks.run(self.manager,plan['id'],'request-1')['state'],'uncertain')

    def test_execution_key_cannot_be_reused_for_other_plan(self):
        self.run_plan()
        second=self.approved()
        with self.assertRaises(Conflict):self.checks.run(self.manager,second['id'],'request-1')

    def test_data_loss_detected_even_expected_schema_matches(self):
        migration=MigrationSpec('erp','delete',('DELETE FROM orders',),{'orders':('id','quantity')})
        self.checks.migrations[('erp','delete')]=migration
        result=self.run_plan('delete')
        self.assertEqual(result['state'],'failed')
        self.assertFalse(result['data_preserved'])
        self.assertTrue(result['rollback_verified'])

    def test_schema_mismatch_detected(self):
        self.checks.migrations[('erp','add-center')]=replace(self.migration,expected_schema={'orders':('id','quantity','wrong')})
        result=self.run_plan('add-center')
        self.assertFalse(result['schema_matches']);self.assertEqual(result['state'],'failed')

    def test_drop_column_is_not_compatible(self):
        migration=MigrationSpec('erp','drop',('ALTER TABLE orders DROP COLUMN quantity',),{'orders':('id',)})
        self.checks.migrations[('erp','drop')]=migration
        result=self.run_plan('drop')
        self.assertEqual(result['state'],'failed');self.assertFalse(result['data_preserved'])

    def test_unsafe_sql_external_access_denied_in_disposable_copy(self):
        for statement in ("ATTACH DATABASE '/tmp/maintenance-forbidden.sqlite3' AS external",'PRAGMA writable_schema=ON','COMMIT'):
            with self.subTest(statement=statement):
                migration=MigrationSpec('erp','unsafe',(statement,),{'orders':('id','quantity')})
                self.checks.migrations[('erp','unsafe')]=migration
                plan=self.approved('unsafe')
                result=self.checks.run(self.manager,plan['id'],'req-'+plan['id'])
                self.assertEqual(result['state'],'failed')

    def test_foreign_key_violation_not_accepted(self):
        migration=MigrationSpec('erp','fk',('CREATE TABLE detail(order_id INTEGER REFERENCES orders(id))','INSERT INTO detail VALUES(999)'),{'orders':('id','quantity'),'detail':('order_id',)})
        self.checks.migrations[('erp','fk')]=migration
        self.assertEqual(self.run_plan('fk')['state'],'failed')

    def test_revoked_manager_cannot_replay_finished_result(self):
        plan=self.approved();self.checks.run(self.manager,plan['id'],'request-1')
        from manager_bridge.store import token_hash
        self.store.revoke_token(self.owner,token_hash(self.manager))
        with self.assertRaises(PermissionDenied):self.checks.run(self.manager,plan['id'],'request-1')

    def test_revocation_in_backup_observer_stops_before_rehearsal(self):
        plan=self.approved()
        from manager_bridge.store import token_hash
        from unittest.mock import patch
        def revoked(*_):
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return self.metadata
        self.checks.backups[('erp','backup-1')]=replace(self.spec,observe=revoked)
        with patch.object(self.checks,'_rehearse') as rehearse:
            with self.assertRaises(PermissionDenied):self.checks.run(self.manager,plan['id'],'request-1')
            rehearse.assert_not_called()

    def test_configuration_change_during_observation_rejects_rehearsal(self):
        plan=self.approved()
        from unittest.mock import patch
        def changed(*_):
            self.registry.register(self.owner,'erp',{**self.config,'name':'변경'},expected_revision=1)
            return self.metadata
        self.checks.backups[('erp','backup-1')]=replace(self.spec,observe=changed)
        with patch.object(self.checks,'_rehearse') as rehearse:
            with self.assertRaises(Conflict):self.checks.run(self.manager,plan['id'],'request-1')
            rehearse.assert_not_called()

    def test_revoked_first_observer_prevents_second_observer(self):
        from manager_bridge.store import token_hash
        calls=[]
        def revoke(*_):
            calls.append('first')
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return self.metadata
        def second(*_):
            calls.append('second')
            return replace(self.metadata,backup_id='backup-2')
        checks=DataChecks(self.store,self.registry,backups={
            ('erp','backup-1'):replace(self.spec,observe=revoke),
            ('erp','backup-2'):BackupSpec('erp','backup-2',second)})
        with self.assertRaises(PermissionDenied):checks.inspect(self.manager,'erp')
        self.assertEqual(calls,['first'])
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM maintenance_backup_reports').fetchone()[0],0)

    def test_configuration_change_by_first_observer_prevents_second(self):
        calls=[]
        def change(*_):
            calls.append('first')
            self.registry.register(self.owner,'erp',{**self.config,'name':'변경'},expected_revision=1)
            return self.metadata
        def second(*_):
            calls.append('second')
            return replace(self.metadata,backup_id='backup-2')
        checks=DataChecks(self.store,self.registry,backups={
            ('erp','backup-1'):replace(self.spec,observe=change),
            ('erp','backup-2'):BackupSpec('erp','backup-2',second)})
        with self.assertRaises(Conflict):checks.inspect(self.manager,'erp')
        self.assertEqual(calls,['first'])
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM maintenance_backup_reports').fetchone()[0],0)

    def test_report_persists_sanitized_facts_for_summary(self):
        self.assertEqual(self.checks.latest(self.manager,'erp')['state'],'not_checked')
        report=self.checks.inspect(self.manager,'erp')
        reopened=DataChecks(self.store,self.registry)
        latest=reopened.latest(self.manager,'erp')
        self.assertEqual(latest['backups'],report['backups'])
        self.assertFalse(latest['stale'])
        self.assertNotIn(str(self.path),json.dumps(latest))
        self.assertNotIn(self.manager,json.dumps(latest))
        with self.store._transaction() as c:
            row=c.execute('SELECT * FROM maintenance_backup_reports WHERE project=?',('erp',)).fetchone()
            self.assertEqual(row['config_fingerprint'],report['config_fingerprint'])
        self.now+=301
        self.assertEqual(reopened.latest(self.manager,'erp')['state'],'stale')

    def test_report_is_project_scoped_and_invalid_after_config_change(self):
        self.checks.inspect(self.manager,'erp')
        with self.assertRaises(NotFound):self.checks.latest(self.other,'erp')
        self.registry.register(self.owner,'erp',{**self.config,'name':'변경'},expected_revision=1)
        latest=self.checks.latest(self.manager,'erp')
        self.assertTrue(latest['configuration_changed'])
        self.assertEqual(latest['state'],'stale')

    def test_invalid_metadata_and_registration_fail_closed(self):
        for values in ({'size':True},{'sha256':'invalid'},{'encrypted':'true'},{'restore_checked_at':950}):
            with self.subTest(values=values),self.assertRaises(ValueError):replace(self.metadata,**values)
        with self.assertRaises(ValueError):DataChecks(self.store,self.registry,backups={('other','backup-1'):self.spec})

    def test_missing_migration_cannot_be_supplied_as_sql(self):
        with self.assertRaises(NotFound):self.plan('not-registered')
        with self.assertRaises(ValueError):self.plan('DROP TABLE orders')
