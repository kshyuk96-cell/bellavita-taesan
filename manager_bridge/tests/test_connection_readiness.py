from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from manager_bridge.store import Store,Conflict,PermissionDenied,token_hash
from manager_bridge.maintenance_registry import Registry
from manager_bridge.connection_readiness import Connections,SourcePin,HostProfile,ConnectionFact,CAPABILITIES


class ConnectionReadiness(unittest.TestCase):
    def setUp(self):
        self.root=self.enterContext(tempfile.TemporaryDirectory());self.now=1000
        self.store=Store(Path(self.root)/'db.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*']);self.registry=Registry(self.store)
        self.config={'company':'a','name':'Fixture','source_kind':'github','source_ref':'fixture/a','branch':'main','services':[],'maintenance_enabled':False}
        self.registry.register(self.owner,'a',self.config)
        self.registry.register(self.owner,'b',{**self.config,'company':'b','source_ref':'fixture/b'})
        self.manager=self.store.add_token('manager','manager',['a'])
        self.worker=self.store.add_token('worker','worker',['a'])
        self.pin=SourcePin('a','github','fixture/a','main','a'*40,'snapshot-v1')
        self.facts={key:ConnectionFact('a',key,'a'*40,'verified','fixture/'+key,self.now,passed=5 if key=='fixture_tests' else 0) for key in CAPABILITIES}
        self.observer=lambda p,key:self.facts.get(key)
        self.connections=Connections(self.store,self.registry,{'a':HostProfile(self.pin,lambda p,key:self.observer(p,key))})

    def test_no_providers_not_connected_not_healthy(self):
        report=Connections(self.store,self.registry).inspect(self.manager,'a')
        self.assertFalse(report['secure_worker_ready']);self.assertFalse(report['development_source_tested'])
        self.assertTrue(all(x['state']=='not_configured' for x in report['facts']))

    def test_readiness_is_not_customer_approval_or_enabled_execution(self):
        report=self.connections.inspect(self.manager,'a')
        self.assertTrue(report['secure_worker_ready'])
        self.assertFalse(report['delegated_execution_enabled']);self.assertFalse(report['customer_launch_verified'])
        self.assertIsNone(report['live_model_score'])

    def test_workspace_tests_alone_not_secure_worker(self):
        self.facts={k:v for k,v in self.facts.items() if k in ('source_snapshot','fixture_tests','build')}
        report=self.connections.inspect(self.manager,'a')
        self.assertTrue(report['development_source_tested']);self.assertFalse(report['secure_worker_ready'])

    def test_skip_or_fail_cannot_be_fully_verified(self):
        for kwargs in ({'skipped':1},{'failed':1},{'passed':0}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):replace(self.facts['fixture_tests'],**kwargs)

    def test_partial_results_keep_skips_visible(self):
        self.facts['fixture_tests']=replace(self.facts['fixture_tests'],state='partial',passed=97,skipped=6)
        report=self.connections.inspect(self.manager,'a')
        result=next(x for x in report['facts'] if x['capability']=='fixture_tests')
        self.assertEqual((result['state'],result['passed'],result['skipped']),('partial',97,6))

    def test_wrong_commit_or_project_cannot_count(self):
        for change in ({'sha':'b'*40},{'project':'b'}):
            self.facts['source_snapshot']=replace(self.facts['source_snapshot'],**change)
            report=self.connections.inspect(self.manager,'a')
            self.assertEqual(report['facts'][0]['state'],'unknown')
            self.assertFalse(report['secure_worker_ready'])

    def test_source_ref_mismatch_rejected_before_observation(self):
        bad=Connections(self.store,self.registry,{'a':HostProfile(replace(self.pin,source_ref='fixture/b'),lambda *_:self.fail('called'))})
        with self.assertRaises(Conflict):bad.inspect(self.manager,'a')

    def test_future_and_old_evidence_are_stale(self):
        for observed in (self.now+1,1):
            self.facts['source_snapshot']=replace(self.facts['source_snapshot'],observed_at=observed)
            report=self.connections.inspect(self.manager,'a',max_age=300)
            self.assertEqual(report['facts'][0]['state'],'stale')

    def test_fact_age_not_refreshed_by_report_time(self):
        self.facts['source_snapshot']=replace(self.facts['source_snapshot'],observed_at=701)
        self.connections.inspect(self.manager,'a',max_age=300)
        self.now+=2
        report=self.connections.latest(self.manager,'a',max_age=300)
        self.assertTrue(report['stale']);self.assertFalse(report['secure_worker_ready'])

    def test_cross_project_and_worker_access_rejected(self):
        for token,project in ((self.manager,'b'),(self.worker,'a')):
            with self.subTest(project=project),self.assertRaises((PermissionDenied,LookupError)):
                self.connections.inspect(token,project)

    def test_revocation_stops_next_observer_and_persistence(self):
        calls=[]
        def observe(p,key):
            calls.append(key);self.store.revoke_token(self.owner,token_hash(self.manager));return self.facts[key]
        self.observer=observe
        with self.assertRaises(PermissionDenied):self.connections.inspect(self.manager,'a')
        self.assertEqual(len(calls),1)
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM manager_connection_reports').fetchone()[0],0)

    def test_config_change_stops_next_observer(self):
        calls=[]
        def observe(p,key):
            calls.append(key);self.registry.register(self.owner,'a',{**self.config,'name':'Changed'},expected_revision=1);return self.facts[key]
        self.observer=observe
        with self.assertRaises(Conflict):self.connections.inspect(self.manager,'a')
        self.assertEqual(len(calls),1)

    def test_error_is_unknown_and_redacted_not_empty(self):
        def failed(*_):raise RuntimeError('private token error')
        self.observer=failed
        report=self.connections.inspect(self.manager,'a')
        self.assertTrue(all(x['state']=='unknown' for x in report['facts']))
        self.assertNotIn('private token',str(report))

    def test_permission_error_is_distinct(self):
        def denied(*_):raise PermissionError('private')
        self.observer=denied
        self.assertTrue(all(x['state']=='permission_denied' for x in self.connections.inspect(self.manager,'a')['facts']))

    def test_unnecessary_owner_actions_are_not_repeated(self):
        self.registry.register(self.owner,'a',{**self.config,'maintenance_enabled':True},expected_revision=1)
        self.connections.inspect(self.manager,'a')
        packet=self.connections.owner_packet(self.manager,'one-packet',['a'])
        self.assertEqual(packet['packet']['owner_actions'],[])
        self.assertFalse(packet['packet']['activation_performed'])

    def test_packet_ignores_receipt_rotation_and_check_time(self):
        self.connections.inspect(self.manager,'a');first=self.connections.owner_packet(self.manager,'packet',['a'])
        self.now+=1
        self.facts={k:replace(v,observed_at=self.now,reference='fixture/new-'+k) for k,v in self.facts.items()}
        self.connections.inspect(self.manager,'a');second=self.connections.owner_packet(self.manager,'packet',['a'])
        self.assertEqual(first['fingerprint'],second['fingerprint'])

    def test_packet_changes_when_source_or_result_changes(self):
        self.connections.inspect(self.manager,'a');self.connections.owner_packet(self.manager,'packet',['a'])
        self.facts['receiver']=replace(self.facts['receiver'],state='unavailable')
        self.connections.inspect(self.manager,'a')
        with self.assertRaises(Conflict):self.connections.owner_packet(self.manager,'packet',['a'])

    def test_restart_preserves_scoped_evidence_and_staleness(self):
        self.connections.inspect(self.manager,'a');self.now+=4000
        restarted=Connections(self.store,self.registry)
        result=restarted.latest(self.manager,'a')
        self.assertTrue(result['stale']);self.assertFalse(result['secure_worker_ready'])

    def test_not_applicable_is_not_a_fake_security_proof(self):
        with self.assertRaises(ValueError):replace(self.facts['sandbox'],state='not_applicable')
        self.facts['build']=replace(self.facts['build'],state='not_applicable')
        self.assertEqual(self.connections.inspect(self.manager,'a')['facts'][2]['state'],'not_applicable')
