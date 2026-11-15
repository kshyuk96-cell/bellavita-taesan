from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from manager_bridge.maintenance import main
from manager_bridge.store import Store, PermissionDenied


class MaintenanceCLI(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory())
        self.data=Path(self.temp)/'private'

    def call(self,*args):
        output=io.StringIO()
        with redirect_stdout(output):code=main(['--data',str(self.data),*args])
        return code,json.loads(output.getvalue())

    def init(self):
        code,value=self.call('init')
        self.assertEqual(code,0)
        return value

    def test_four_sources_default_disabled_private_credentials(self):
        value=self.init()
        self.assertEqual(len(value['projects']),4)
        for file in ('owner.token','manager.token','maintenance.sqlite3'):
            self.assertEqual(stat.S_IMODE((self.data/file).stat().st_mode),0o600)
        code,rows=self.call('inventory','--token-file',str(self.data/'manager.token'))
        self.assertEqual(code,0)
        self.assertTrue(all(r['config']['maintenance_enabled'] is False for r in rows))
        self.assertEqual({r['config']['source_kind'] for r in rows},{'sites','github'})
        for file in ('owner.token','manager.token'):
            self.assertNotIn((self.data/file).read_text().strip(),json.dumps(value)+json.dumps(rows))

    def test_bootstrap_cannot_overwrite_existing_tokens(self):
        self.init();before=(self.data/'owner.token').read_bytes()
        self.assertEqual(self.call('init')[0],1)
        self.assertEqual((self.data/'owner.token').read_bytes(),before)

    def test_inspect_does_not_treat_disconnected_as_healthy(self):
        self.init()
        code,value=self.call('inspect','--project','bellavita-erp','--token-file',str(self.data/'manager.token'))
        self.assertEqual(code,0)
        self.assertEqual(value['state'],'attention')
        self.assertTrue(all(r['state']=='not_configured' for r in value['observations']))
        self.assertIs(value['operational_change'],False)

    def test_combined_report_is_available_without_execution_provider(self):
        self.init()
        code,value=self.call('report','--token-file',str(self.data/'manager.token'))
        self.assertEqual(code,0)
        self.assertIsNone(value['live_ai_score'])
        self.assertIs(value['live_ai_measured'],False)
        self.assertNotIn((self.data/'manager.token').read_text().strip(),json.dumps(value))

    def test_revoked_or_worker_token_cannot_read_whole_inventory(self):
        self.init()
        store=Store(self.data/'maintenance.sqlite3')
        worker=store.add_token('test-worker','worker',['bellavita-erp'])
        path=self.data/'worker.token';path.write_text(worker);path.chmod(0o600)
        self.assertEqual(self.call('inventory','--token-file',str(path))[0],1)
        from manager_bridge.store import token_hash
        owner=(self.data/'owner.token').read_text().strip()
        manager=(self.data/'manager.token').read_text().strip()
        store.revoke_token(owner,token_hash(manager))
        self.assertEqual(self.call('inventory','--token-file',str(self.data/'manager.token'))[0],1)

    def test_database_symlink_does_not_open_target(self):
        self.init()
        database=self.data/'maintenance.sqlite3'
        target=self.data/'preserved.sqlite3'
        database.rename(target);database.symlink_to(target)
        before=target.read_bytes()
        self.assertEqual(self.call('inventory','--token-file',str(self.data/'manager.token'))[0],1)
        self.assertEqual(target.read_bytes(),before)


class ExactMaintenanceClaim(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory())
        self.store=Store(Path(self.tmp)/'manager.sqlite3')
        self.owner=self.store.add_token('owner','owner',['*'])
        self.store.register_project(self.owner,'a','fixture/a')
        self.store.register_project(self.owner,'b','fixture/b')
        self.worker=self.store.add_token('worker','worker',['a'])

    def approved(self,project,key):
        task=self.store.submit(self.owner,key,{'project':project,'repository':'fixture/'+project,
            'base_sha':'a'*40,'kind':'test','description':'synthetic maintenance scope','checks':['fixture']})
        return self.store.approve(self.owner,task['id'],task['version'],task['manifest_digest'])

    def test_targeted_claim_never_takes_another_queued_task(self):
        first=self.approved('a','first');second=self.approved('a','second')
        result=self.store.claim(self.worker,'a',task_id=second['id'])
        self.assertEqual(result['id'],second['id'])
        self.assertEqual(self.store.get_task(self.owner,first['id'])['state'],'queued')

    def test_foreign_target_does_not_fallback_to_local_task(self):
        first=self.approved('a','first');foreign=self.approved('b','foreign')
        self.assertIsNone(self.store.claim(self.worker,'a',task_id=foreign['id']))
        self.assertEqual(self.store.get_task(self.owner,first['id'])['state'],'queued')

    def test_nonexistent_target_does_not_claim_available_task(self):
        first=self.approved('a','first')
        self.assertIsNone(self.store.claim(self.worker,'a',task_id='0'*32))
        self.assertEqual(self.store.get_task(self.owner,first['id'])['state'],'queued')
