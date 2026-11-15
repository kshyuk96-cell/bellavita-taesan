from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from manager_bridge.source_watch import SourceWatch,SourceHead,HeadProbe
from manager_bridge.store import Store,Conflict,NotFound,PermissionDenied,token_hash
from manager_bridge.maintenance_registry import Registry


class SourceWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory());self.now=1000
        self.store=Store(Path(self.temp)/'private.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*']);self.registry=Registry(self.store)
        self.config={'company':'a','name':'A','source_kind':'github','source_ref':'fixture/a','branch':'main','services':[],'maintenance_enabled':False}
        self.registry.register(self.owner,'a',self.config)
        self.registry.register(self.owner,'b',{**self.config,'company':'b','source_ref':'fixture/b','name':'B'})
        self.manager=self.store.add_token('manager','manager',['a'])
        self.worker=self.store.add_token('worker','worker',['a'])
        self.head=SourceHead('a','github','fixture/a','main','a'*40,'v1','fixture/source',self.now)
        self.calls=[];self.callback=lambda p:self.head
        self.watch=SourceWatch(self.store,self.registry,{'a':HeadProbe('github','fixture/a','main',self.observe)})

    def observe(self,project):
        self.calls.append(project);return self.callback(project)

    def advance(self,sha,revision='v2'):
        self.now+=1;self.head=replace(self.head,sha=sha,revision=revision,observed_at=self.now)
        return self.watch.inspect(self.manager,'a')

    def test_baseline_change_and_repeated_head_deduplicate(self):
        self.assertEqual(self.watch.inspect(self.manager,'a')['state'],'baseline')
        change=self.advance('b'*40)
        self.assertEqual(change['state'],'changed');self.assertEqual(change['previous_sha'],'a'*40)
        self.assertFalse(change['approval_granted']);self.assertFalse(change['source_modified'])
        self.assertEqual(self.watch.inspect(self.manager,'a')['state'],'unchanged')
        events=self.watch.pending(self.manager,['a'])['events']
        self.assertEqual(len(events),1);self.assertEqual(events[0]['id'],change['event_id'])

    def test_returning_head_creates_new_transition_not_duplicate_old_event(self):
        self.watch.inspect(self.manager,'a');one=self.advance('b'*40)
        two=self.advance('a'*40,'v3');three=self.advance('b'*40,'v4')
        self.assertEqual(len({x['event_id'] for x in (one,two,three)}),3)
        self.assertEqual([x['sequence'] for x in self.watch.pending(self.manager,['a'])['events']],[1,2,3])

    def test_acknowledgement_idempotent_without_approval_or_execution(self):
        self.watch.inspect(self.manager,'a');key=self.advance('b'*40)['event_id']
        first=self.watch.acknowledge(self.manager,key);second=self.watch.acknowledge(self.manager,key)
        self.assertEqual(first,second);self.assertTrue(first['acknowledged']);self.assertFalse(first['approval_granted'])
        self.assertEqual(self.watch.pending(self.manager,['a'])['events'],[])
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM bridge_tasks').fetchone()[0],0)

    def test_absent_observer_not_empty_or_unchanged(self):
        report=SourceWatch(self.store,self.registry).inspect(self.manager,'a')
        self.assertEqual(report['state'],'not_configured');self.assertIsNone(report['head_sha'])

    def test_outage_and_permission_denial_preserve_prior_good_head(self):
        self.watch.inspect(self.manager,'a')
        for error,state in ((TimeoutError('private URL'), 'unavailable'),(PermissionDenied('private details'),'permission_denied')):
            self.callback=lambda _,e=error:(_ for _ in ()).throw(e)
            report=self.watch.inspect(self.manager,'a')
            self.assertEqual(report['state'],state);self.assertEqual(report['previous_sha'],'a'*40)
            self.assertNotIn('private',str(report));self.assertIsNone(report['head_sha'])
        self.callback=lambda _:self.head
        self.assertEqual(self.advance('b'*40)['previous_sha'],'a'*40)

    def test_bad_source_kind_project_branch_or_ref_unknown(self):
        for change in ({'project':'b'},{'source_kind':'sites'},{'source_ref':'fixture/b'},{'branch':'other'}):
            self.head=replace(SourceHead('a','github','fixture/a','main','a'*40,'v1','fixture/source',self.now),**change)
            self.assertEqual(self.watch.inspect(self.manager,'a')['state'],'unknown')
        self.assertEqual(self.watch.pending(self.manager,['a'])['events'],[])

    def test_old_future_and_delayed_response_cannot_update_baseline(self):
        self.watch.inspect(self.manager,'a')
        for stamp in (1,self.now+1):
            self.head=replace(self.head,sha='b'*40,observed_at=stamp)
            self.assertEqual(self.watch.inspect(self.manager,'a')['state'],'stale')
        self.head=replace(self.head,sha='b'*40,observed_at=self.now)
        self.now+=10;self.head=replace(self.head,observed_at=self.now);self.watch.inspect(self.manager,'a')
        self.head=replace(self.head,sha='c'*40,observed_at=self.now-1)
        self.assertEqual(self.watch.inspect(self.manager,'a')['state'],'stale')

    def test_unknown_shape_not_success(self):
        self.callback=lambda _:self.head.__dict__
        self.assertEqual(self.watch.inspect(self.manager,'a')['state'],'unknown')

    def test_cross_project_and_worker_admin_access_denied_before_callback(self):
        for token,project in ((self.manager,'b'),(self.worker,'a')):
            with self.assertRaises((NotFound,PermissionDenied)):self.watch.inspect(token,project)
        self.assertEqual(self.calls,[])
        with self.assertRaises((NotFound,PermissionDenied)):self.watch.pending(self.manager,['a','b'])

    def test_revoke_during_callback_does_not_persist(self):
        def callback(_):
            with self.store._transaction() as c:c.execute('UPDATE bridge_tokens SET revoked=? WHERE token_hash=?',(self.now,token_hash(self.manager)))
            return self.head
        self.callback=callback
        with self.assertRaises(PermissionDenied):self.watch.inspect(self.manager,'a')
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM manager_source_heads').fetchone()[0],0)

    def test_config_change_during_callback_does_not_persist(self):
        def callback(_):
            self.registry.register(self.owner,'a',{**self.config,'name':'Updated'},expected_revision=1)
            return self.head
        self.callback=callback
        with self.assertRaises(Conflict):self.watch.inspect(self.manager,'a')
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM manager_source_heads').fetchone()[0],0)

    def test_changed_registration_requires_new_baseline_old_events_not_current(self):
        self.watch.inspect(self.manager,'a');key=self.advance('b'*40)['event_id']
        self.registry.register(self.owner,'a',{**self.config,'name':'Updated'},expected_revision=1)
        self.assertEqual(self.watch.pending(self.manager,['a'])['events'],[])
        with self.assertRaises(Conflict):self.watch.acknowledge(self.manager,key)
        self.assertEqual(self.watch.inspect(self.manager,'a')['state'],'baseline')

    def test_misbound_probe_rejected_without_call(self):
        watch=SourceWatch(self.store,self.registry,{'a':HeadProbe('github','fixture/a','other',self.observe)})
        with self.assertRaises(Conflict):watch.inspect(self.manager,'a')
        self.assertEqual(self.calls,[])

    def test_known_registered_credential_reference_never_saved(self):
        self.head=replace(self.head,reference=self.worker)
        result=self.watch.inspect(self.manager,'a');self.assertEqual(result['state'],'unknown')
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM manager_source_heads').fetchone()[0],0)

    def test_concurrent_polls_one_transition(self):
        self.watch.inspect(self.manager,'a');self.now+=1;self.head=replace(self.head,sha='b'*40,observed_at=self.now)
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:self.watch.inspect(self.manager,'a'),range(4)))
        self.assertEqual(sum(r['state']=='changed' for r in results),1)
        self.assertEqual(len(self.watch.pending(self.manager,['a'])['events']),1)

    def test_pending_limit_reports_more_and_returns_oldest(self):
        self.watch.inspect(self.manager,'a');self.advance('b'*40);self.advance('c'*40)
        packet=self.watch.pending(self.manager,['a'],limit=1)
        self.assertTrue(packet['more']);self.assertEqual(len(packet['events']),1);self.assertEqual(packet['events'][0]['sequence'],1)

    def test_source_proof_fresh_exact_and_never_from_baseline_cache(self):
        proof=self.watch.source_observer('a')
        observed=proof('a');self.assertEqual(observed.sha,'a'*40)
        self.head=replace(self.head,sha='b'*40)
        self.assertEqual(proof('a').sha,'b'*40)
        with self.assertRaises(Conflict):proof('b')
        self.head=replace(self.head,observed_at=1)
        with self.assertRaises(Conflict):proof('a')

    def test_source_proof_cannot_forward_known_registered_credential(self):
        self.head=replace(self.head,reference=self.worker)
        with self.assertRaises(Conflict):self.watch.source_observer('a')('a')

    def test_source_proof_rechecks_configuration_after_provider(self):
        def callback(_):
            self.registry.register(self.owner,'a',{**self.config,'name':'Updated'},expected_revision=1)
            return self.head
        self.callback=callback
        with self.assertRaises(Conflict):self.watch.source_observer('a')('a')

    def test_restart_retains_pending_transition(self):
        self.watch.inspect(self.manager,'a');key=self.advance('b'*40)['event_id']
        again=SourceWatch(self.store,self.registry)
        self.assertEqual(again.pending(self.manager,['a'])['events'][0]['id'],key)

    def test_invalid_batch_or_age_rejected(self):
        for projects in ([],['a','a'],'a'):
            with self.assertRaises(ValueError):self.watch.pending(self.manager,projects)
        with self.assertRaises(ValueError):SourceWatch(self.store,self.registry,max_age=True)
        with self.assertRaises(ValueError):self.watch.pending(self.manager,['a'],limit=True)
