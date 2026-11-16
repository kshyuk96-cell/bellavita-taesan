"""Grouped local reporting and actual byte-derived artifact metadata fixtures."""
import hashlib
import json
from unittest.mock import patch
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.operations_reporting import OperationsReporting,ArtifactObservation
from manager_bridge.control_digest import ControlDigest
from manager_bridge.governance_portfolio import Portfolio
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.store import Conflict,NotFound,PermissionDenied


class OperationsReportingTests(unittest.TestCase):
    setUp=fixtures.ThreeTierTests.setUp
    observe=fixtures.ThreeTierTests.observe
    verify=fixtures.ThreeTierTests.verify
    task=fixtures.ThreeTierTests.task

    def service(self,**kwargs):
        return OperationsReporting(self.core,ControlDigest(Portfolio(self.core)),**kwargs)

    def test_artifact_actual_bytes_hash_and_size_only_are_retained(self):
        raw=b'PRIVATE_LOG_PAYLOAD';calls=[]
        def observer(project,key):calls.append((project,key));return ArtifactObservation(project,'qa_report',key,raw,self.now)
        service=self.service(artifact_sources={('project','qa_report'):observer})
        result=service.register_artifact(self.manager,'project','qa_report','observation-one')
        self.assertEqual(result['sha256'],hashlib.sha256(raw).hexdigest());self.assertEqual(result['size_bytes'],len(raw))
        self.assertEqual(result['retrieval'],'metadata_only');self.assertFalse(result['acceptance_authorized'])
        self.assertEqual(service.get_artifact(self.manager,result['reference']),result)
        with self.store._transaction() as c:
            rows=[dict(row) for row in c.execute('SELECT * FROM operations_artifacts')]
        self.assertNotIn('PRIVATE_LOG_PAYLOAD',json.dumps(rows));self.assertNotIn('data',result)
        self.assertEqual(calls,[('project','observation-one')])
        self.now+=86401
        self.assertEqual(service.get_artifact(self.manager,result['reference'])['state'],'expired')
        self.assertFalse(service.get_artifact(self.manager,result['reference'])['physical_deletion_verified'])

    def test_artifact_unconfigured_scope_mismatch_and_immutable_observation(self):
        with self.assertRaises(NotFound):self.service().register_artifact(self.manager,'project','diagnostic','one')
        raw=[b'first']
        service=self.service(artifact_sources={('project','diagnostic'):lambda project,key:ArtifactObservation(project,'diagnostic',key,raw[0],self.now)})
        result=service.register_artifact(self.manager,'project','diagnostic','one')
        limited=self.store.add_token('limited','manager',['other'])
        with self.assertRaises((NotFound,PermissionDenied)):service.get_artifact(limited,result['reference'])
        raw[0]=b'changed'
        with self.assertRaises(Conflict):service.register_artifact(self.manager,'project','diagnostic','one')
        with self.assertRaises(ValueError):service.register_artifact(self.manager,'project','diagnostic','/private/auth.json')
        with self.assertRaises(TypeError):service.register_artifact(self.manager,'project','diagnostic','two',sha256='a'*64)

    def test_artifact_callback_reauth_and_config_binding(self):
        def revoked(project,key):
            with self.store._transaction() as c:c.execute('UPDATE bridge_tokens SET revoked=? WHERE subject=?',(self.now,'taesan'))
            return ArtifactObservation(project,'diagnostic',key,b'fixture',self.now)
        service=self.service(artifact_sources={('project','diagnostic'):revoked})
        with self.assertRaises(PermissionDenied):service.register_artifact(self.manager,'project','diagnostic','one')
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM operations_artifacts').fetchone()[0],0)

    def test_one_grouped_snapshot_pagination_and_request_deduplication(self):
        service=self.service(controller=ContinuousManager(self.core))
        with patch.object(service.control_digest,'report',wraps=service.control_digest.report) as collect:
            first=service.report(self.manager,['project'],'daily-one',limit=1)
            second=service.report(self.manager,['project'],'daily-one',after=first['group_window']['next_cursor'],limit=1)
            repeated=service.report(self.manager,['project'],'daily-one',limit=1)
        self.assertEqual(collect.call_count,1)
        self.assertEqual(first['report_id'],second['report_id']);self.assertEqual(first,repeated)
        self.assertTrue(first['group_window']['partial']);self.assertFalse(first['summary_complete'])
        self.assertNotEqual(first['normal'],second['normal'])
        self.assertFalse(first['messages_sent']);self.assertEqual(first['per_task_messages_sent'],0)
        self.assertNotIn('description',json.dumps(first))

    def test_urgent_uncertain_actions_are_grouped_without_task_messages(self):
        controller=ContinuousManager(self.core,handlers={'start':lambda *args:(_ for _ in ()).throw(ConnectionError())})
        self.task();controller.tick(self.manager,['project'],'fixture-manager')
        service=self.service(controller=controller)
        report=service.report(self.manager,['project'],'uncertain')
        self.assertEqual(report['urgent'],[{'project':'project','category':'uncertain_execution','urgency':'urgent','count':1}])
        self.assertFalse(report['acceptance_authorized'])

    def test_cursor_scope_actor_config_and_expiration_are_bound(self):
        service=self.service();first=service.report(self.manager,['project'],'page',limit=1);cursor=first['group_window']['next_cursor']
        with self.assertRaises(NotFound):service.report(self.owner,['project'],'page',after=cursor,limit=1)
        with self.assertRaises(Conflict):service.report(self.manager,['other'],'page',after=cursor,limit=1)
        self.now+=301
        with self.assertRaises(NotFound):service.report(self.manager,['project'],'page',after=cursor,limit=1)
        cached=service.report(self.manager,['project'],'page')
        self.assertEqual(cached['snapshot_state'],'stale')
        self.registry.register(self.owner,'project',self.config,expected_revision=1)
        with self.assertRaises(Conflict):service.report(self.manager,['project'],'page')

    def test_report_callback_revocation_cannot_store_snapshot(self):
        service=self.service();original=service.control_digest.report
        def revoked(*args,**kwargs):
            result=original(*args,**kwargs)
            with self.store._transaction() as c:c.execute('UPDATE bridge_tokens SET revoked=? WHERE subject=?',(self.now,'taesan'))
            return result
        with patch.object(service.control_digest,'report',side_effect=revoked):
            with self.assertRaises(PermissionDenied):service.report(self.manager,['project'],'denied')
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM operations_grouped_reports').fetchone()[0],0)


if __name__=='__main__':unittest.main()
