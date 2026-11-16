"""Private recovery and scoped expiry fixtures. No network or model calls."""
import json
import os
import sqlite3
from pathlib import Path
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.governance_durability import GovernanceDurability
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.store import Store,PermissionDenied,Conflict,token_hash
from manager_bridge.maintenance_registry import Registry
from manager_bridge.three_tier import ThreeTier


class DurabilityTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    task=fixtures.ThreeTierTests.task
    start=fixtures.ThreeTierTests.start

    def service(self,**kwargs):return GovernanceDurability(self.core,**kwargs)

    def test_wal_consistent_private_backup_restore_and_no_replay(self):
        controller=ContinuousManager(self.core,handlers={'start':lambda *a:(_ for _ in ()).throw(ConnectionError('private'))})
        task=self.task();controller.tick(self.manager,['project'],'before-backup')
        with self.store._transaction() as c:
            c.execute("UPDATE continuous_actions SET state='running'")
        service=self.service()
        wal=sqlite3.connect(self.store.path);self.addCleanup(wal.close)
        wal.execute('PRAGMA journal_mode=WAL');wal.execute("CREATE TABLE wal_fixture (value TEXT)")
        wal.execute("INSERT INTO wal_fixture VALUES ('committed')");wal.commit()
        wal.execute("INSERT INTO wal_fixture VALUES ('uncommitted')")
        bundle=self.root/'backup';receipt=service.backup(self.owner,bundle)
        self.assertEqual(receipt['state'],'private_backup_complete')
        self.assertEqual(bundle.stat().st_mode&0o777,0o700)
        for path in bundle.iterdir():self.assertEqual(path.stat().st_mode&0o777,0o600)
        for path in bundle.iterdir():self.assertNotIn(self.owner.encode(),path.read_bytes())
        restored=self.root/'recovered.sqlite';result=service.restore(self.owner,bundle,restored)
        self.assertEqual(result['state'],'restored_requires_verification');self.assertEqual(result['uncertain_actions'],1)
        with sqlite3.connect(restored) as c:
            self.assertEqual(c.execute('SELECT value FROM wal_fixture').fetchall(),[('committed',)])
            self.assertEqual(c.execute('SELECT state,reason FROM continuous_actions').fetchone(),('uncertain','restore_uncertain'))
            self.assertEqual(c.execute('SELECT expires FROM continuous_leader').fetchone()[0],0)
        wal.rollback()
        restored_store=Store(restored,clock=lambda:self.now);core=ThreeTier(restored_store,Registry(restored_store));calls=[]
        recovered=ContinuousManager(core,handlers={'start':lambda *a:calls.append(a)})
        recovered.tick(self.manager,['project'],'after-restore')
        self.assertEqual(calls,[])
        self.assertFalse(result['workers_stopped_verified']);self.assertFalse(result['automatic_replay'])

    def test_restored_queued_and_old_lease_execution_stay_quarantined_after_restart(self):
        queued=self.task();running=self.task();assignment=self.start(running)
        service=self.service();bundle=self.root/'snapshot';service.backup(self.owner,bundle)
        restored=self.root/'restored.sqlite';service.restore(self.owner,bundle,restored)
        for _ in range(2):
            # Core reconstruction without GovernanceDurability/configuration keeps quarantine.
            store=Store(restored,clock=lambda:self.now);core=ThreeTier(store,Registry(store));calls=[]
            controller=ContinuousManager(core,handlers={'start':lambda *args:calls.append(args),'dispatch_development':lambda *args:calls.append(args)})
            controller.tick(self.manager,['project'],'restored-instance')
            self.assertEqual(calls,[])
            self.assertEqual(store.get_task(self.owner,queued['id'])['state'],'queued')
            with self.assertRaisesRegex(Conflict,'restored state'):
                core.start(self.manager,queued['id'],'dev','restored-start')
            self.assertIsNone(store.claim(self.dev,'project'))
            with self.assertRaisesRegex(Conflict,'restored state'):
                store.report(self.dev,running['id'],assignment['lease_token'],{'head_sha':'b'*40,'summary':'Old worker remains unknown','checks':[{'name':'fixture-check','passed':True}]})
            self.assertEqual(store.get_task(self.owner,running['id'])['state'],'running')

    def test_profile_expiration_observer_runs_once_per_project_role(self):
        extra=self.store.add_token('developer-two','worker',['project'])
        self.core.register(self.owner,'project','dev-two',token_hash(extra),'developer',agent_id='dev-two-agent')
        calls=[]
        def observer(project,role):
            calls.append((project,role));return {'state':'observed','expires_at':self.now+7200}
        result=self.service(expiration_provider=observer).auth_watch(self.owner,['project'])
        self.assertEqual(sorted(calls),[('project','developer'),('project','reviewer')])
        self.assertEqual(len([item for item in result['items'] if item['role']=='developer']),2)

    def test_global_owner_only_and_refuse_overwrite_symlinks_public_paths(self):
        service=self.service();limited=self.store.add_token('limited-owner','owner',['project'])
        for token in (self.manager,self.dev,limited):
            with self.assertRaises(PermissionDenied):service.backup(token,self.root/'denied')
        bundle=self.root/'backup';service.backup(self.owner,bundle)
        with self.assertRaises(FileExistsError):service.backup(self.owner,bundle)
        destination=self.root/'existing';destination.write_text('keep')
        with self.assertRaises(ValueError):service.restore(self.owner,bundle,destination)
        self.assertEqual(destination.read_text(),'keep')
        symlink=self.root/'link';symlink.symlink_to(bundle,target_is_directory=True)
        with self.assertRaises(ValueError):service.restore(self.owner,symlink,self.root/'bad')
        public=self.root/'public';public.mkdir(mode=0o755);public.chmod(0o755)
        with self.assertRaises(ValueError):service.backup(self.owner,public/'backup')

    def test_tampered_metadata_and_unbounded_bundle_rejected(self):
        service=self.service();bundle=self.root/'backup';service.backup(self.owner,bundle)
        metadata=bundle/'metadata.json';value=json.loads(metadata.read_text());value['schema_digest']='f'*64;metadata.write_text(json.dumps(value))
        with self.assertRaises(Conflict):service.restore(self.owner,bundle,self.root/'bad')
        self.assertFalse((self.root/'bad').exists())
        (bundle/'extra').write_text('extra')
        with self.assertRaises(ValueError):service.restore(self.owner,bundle,self.root/'bad')

    def test_expiry_parent_and_profile_notice_dedupe(self):
        def observed(project,role):return {'state':'observed','expires_at':self.now+7200}
        service=self.service(expiration_provider=observed)
        with self.store._transaction() as c:
            c.execute('UPDATE bridge_tokens SET expires=? WHERE token_hash=?',(self.now+100,token_hash(self.dev)))
        first=service.auth_watch(self.owner,['project'],queue_notices=True)
        developer=next(i for i in first['items'] if i['role']=='developer')
        self.assertEqual(developer['state'],'refresh_required')
        service.auth_watch(self.owner,['project'],queue_notices=True)
        notices=service.list_notices(self.manager,['project'])['items']
        self.assertEqual(len(notices),1)
        self.assertNotIn('credential_hash',json.dumps(first));self.assertNotIn(self.dev,json.dumps(first))
        self.assertFalse(first['refresh_performed']);self.assertFalse(first['messages_sent'])

    def test_parent_expiry_and_revocation_are_not_current_credentials(self):
        parent=self.store.add_token('parent-worker','worker',['project'],expires_at=self.now+100)
        service=self.service(expiration_provider=lambda *_:{'state':'observed','expires_at':self.now+9000})
        with self.store._transaction() as c:
            c.execute('INSERT INTO bridge_web_sessions VALUES (?,?)',(token_hash(self.dev),token_hash(parent)))
        result=service.auth_watch(self.owner,['project'])
        developer=next(i for i in result['items'] if i['role']=='developer')
        self.assertEqual(developer['credential_expires_at'],self.now+100)
        self.assertEqual(developer['state'],'refresh_required')
        self.store.revoke_token(self.owner,token_hash(parent))
        result=service.auth_watch(self.owner,['project'],refresh_before_seconds=0)
        self.assertEqual(next(i for i in result['items'] if i['role']=='developer')['state'],'refresh_required')

    def test_restore_refuses_existing_sidecars_and_oversized_metadata(self):
        service=self.service();bundle=self.root/'backup';service.backup(self.owner,bundle)
        destination=self.root/'restore.sqlite';sidecar=Path(str(destination)+'-wal');sidecar.write_text('keep')
        with self.assertRaises(ValueError):service.restore(self.owner,bundle,destination)
        self.assertEqual(sidecar.read_text(),'keep')
        (bundle/'metadata.json').write_bytes(b' '*65537)
        with self.assertRaises(ValueError):service.restore(self.owner,bundle,self.root/'other.sqlite')

    def test_unknown_profile_expiry_is_unavailable_and_queue_requires_owner(self):
        service=self.service()
        result=service.auth_watch(self.manager,['project'])
        self.assertEqual([i['state'] for i in result['items'] if i['role']=='developer'],['unavailable'])
        with self.assertRaises(PermissionDenied):service.auth_watch(self.manager,['project'],queue_notices=True)
        limited=self.store.add_token('limited','manager',['project'])
        with self.assertRaises(Exception):service.auth_watch(limited,['other'])

    def test_observer_outside_transaction_revalidates_revocation(self):
        def observer(project,role):
            self.store.revoke_token(self.owner,token_hash(self.manager))
            return {'state':'observed','expires_at':self.now+9000}
        service=self.service(expiration_provider=observer)
        with self.assertRaises(PermissionDenied):service.auth_watch(self.manager,['project'])
        self.assertEqual(service.list_notices(self.owner,['project'])['items'],[])


if __name__=='__main__':unittest.main()
