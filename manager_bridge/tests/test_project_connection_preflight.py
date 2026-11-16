import json
import unittest
from unittest.mock import patch
from manager_bridge.tests import test_control_onboarding as control_fixtures
from manager_bridge.project_connection_preflight import ProjectConnectionPreflight,MetadataObservation,ACTIONS
from manager_bridge.source_watch import SourceWatch
from manager_bridge.connection_readiness import CAPABILITIES
from manager_bridge.store import Conflict,NotFound,PermissionDenied,token_hash


class ConnectionPreflightTests(unittest.TestCase):
    def setUp(self):
        self.fx=control_fixtures.ControlOnboardingTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.store=self.fx.store;self.registry=self.fx.registry;self.owner=self.fx.owner
        missing=['bellavita-erp','funeral-association']
        self.fx.onboarding.register_catalog(self.owner,missing,{p:0 for p in missing})
        self.watch=SourceWatch(self.store,self.registry)
        self.preflight=ProjectConnectionPreflight(self.store,self.registry,self.fx.control)

    def seed_source(self,project='bellavita-shop',state='unchanged',observed_at=1000):
        config=self.registry.get(self.owner,project)
        with self.store._transaction() as c:
            c.execute('INSERT OR REPLACE INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',(project,config['fingerprint'],'a'*40,'v1','fixture/source',observed_at,1))
            c.execute('INSERT OR REPLACE INTO manager_source_observation_status VALUES (?,?,?,?)',(project,config['fingerprint'],state,observed_at))

    def metadata(self,project='bellavita-shop',state='observed',observed_at=1000):
        config=self.registry.get(self.owner,project)
        result=[MetadataObservation(project,config['fingerprint'],'source',config['config']['source_kind'],config['config']['source_ref'],state,'fixture/metadata',observed_at)]
        result.extend(MetadataObservation(project,config['fingerprint'],'service',s['kind'],s['id'],state,'fixture/service',observed_at) for s in config['config']['services'])
        return result

    def test_exact_four_customer_preflight_is_missing_and_blocked_without_probes(self):
        with patch.object(self.fx.connections,'inspect',side_effect=AssertionError('no callback allowed')),patch.object(self.watch,'inspect',side_effect=AssertionError('no source probe allowed')):
            result=self.preflight.inspect(self.owner)
        self.assertEqual({p['project'] for p in result['projects']},set(ACTIONS))
        self.assertTrue(all(p['execution_blocked'] for p in result['projects']))
        self.assertTrue(all(p['source']['classification']=='missing' for p in result['projects']))
        self.assertFalse(result['probe_performed']);self.assertFalse(result['approval_granted'])
        self.assertIsNone(result['model_quality_score'])
        self.assertNotEqual(result['projects'][0]['required_actions'],result['projects'][-1]['required_actions'])

    def test_fresh_source_and_metadata_are_not_live_connection_or_execution_authority(self):
        project='bellavita-shop';self.seed_source();self.fx.register_roles()
        config=self.registry.get(self.owner,project)
        with self.store._transaction() as c:
            facts=[{'capability':cap,'state':'verified','reference':'fixture/cached','observed_at':1000} for cap in CAPABILITIES]
            report={'project':project,'config_fingerprint':config['fingerprint'],'facts':facts}
            c.execute('INSERT INTO manager_connection_reports VALUES (?,?,?)',(project,json.dumps(report),1000))
        service=ProjectConnectionPreflight(self.store,self.registry,self.fx.control,metadata=self.metadata())
        item=service.inspect(self.owner,[project])['projects'][0]
        self.assertEqual(item['source']['classification'],'observed_current_source')
        self.assertTrue(all(m['classification']=='observed_metadata' for m in item['provider_metadata']))
        self.assertTrue(item['execution_blocked']);self.assertFalse(item['live_connection_verified'])
        self.assertIn('task_specific_owner_policy_qa_and_live_execution_fences_required',item['blocks'])

    def test_source_denied_stale_future_and_metadata_config_drift_are_explicit(self):
        project='bellavita-shop';self.seed_source(state='permission_denied')
        service=ProjectConnectionPreflight(self.store,self.registry,self.fx.control,metadata=self.metadata())
        self.assertEqual(service.inspect(self.owner,[project])['projects'][0]['source']['classification'],'permission_denied')
        for stamp in (1,1001,float('inf')):
            self.seed_source(observed_at=stamp)
            self.assertEqual(service.inspect(self.owner,[project])['projects'][0]['source']['classification'],'unverified')
        self.seed_source();cfg=self.registry.get(self.owner,project)
        self.registry.register(self.owner,project,cfg['config'],expected_revision=cfg['revision'])
        item=service.inspect(self.owner,[project])['projects'][0]
        self.assertEqual(item['source']['reason'],'source_configuration_changed')
        self.assertTrue(all(m['reason']=='metadata_configuration_changed' for m in item['provider_metadata']))

    def test_packet_idempotency_private_scope_and_source_change_invalidation(self):
        packet=self.preflight.owner_packet(self.owner,'owner-review')
        self.assertEqual(packet,self.preflight.owner_packet(self.owner,'owner-review'))
        self.assertEqual(self.preflight.get_packet(self.owner,packet['id'])['state'],'current')
        self.seed_source()
        self.assertEqual(self.preflight.get_packet(self.owner,packet['id'])['state'],'invalidated')
        with self.assertRaises(Conflict):self.preflight.owner_packet(self.owner,'owner-review')
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM project_connection_preflight_packets').fetchone()[0],1)

    def test_cross_project_scope_revocation_worker_and_fingerprint_cas_fail_closed(self):
        with self.assertRaises(NotFound):self.preflight.inspect(self.fx.limited)
        self.assertEqual(len(self.preflight.inspect(self.fx.limited,['bellavita-shop'])['projects']),1)
        with self.assertRaises(PermissionDenied):self.preflight.inspect(self.fx.dev,['bellavita-shop'])
        with self.assertRaises(Conflict):self.preflight.owner_packet(self.owner,'cas',['bellavita-shop'],expected_fingerprints={'bellavita-shop':'b'*64})
        packet=self.preflight.owner_packet(self.fx.limited,'private',['bellavita-shop'])
        with self.assertRaises(NotFound):self.preflight.get_packet(self.owner,packet['id'])
        self.store.revoke_token(self.owner,token_hash(self.fx.limited))
        with self.assertRaises(PermissionDenied):self.preflight.get_packet(self.fx.limited,packet['id'])

    def test_metadata_and_request_bearers_rejected_without_echo(self):
        observation=self.metadata()[0]
        values={**vars(observation),'reference':self.fx.reviewer}
        service=ProjectConnectionPreflight(self.store,self.registry,self.fx.control,metadata=[MetadataObservation(**values)])
        with self.assertRaises(ValueError):service.inspect(self.owner,['bellavita-shop'])
        with self.assertRaises(ValueError):self.preflight.owner_packet(self.owner,self.fx.dev)
        with self.assertRaises(ValueError):ProjectConnectionPreflight(self.store,self.registry,metadata=[vars(observation)])
        with self.assertRaises(ValueError):MetadataObservation(**{**vars(observation),'observed_at':float('nan')})

    def test_config_change_inside_snapshot_prevents_mixed_packet_and_rolls_back(self):
        original=self.preflight._source
        def drift(c,project,config,max_age):
            result=original(c,project,config,max_age)
            c.execute('UPDATE maintenance_projects SET revision=revision+1,fingerprint=? WHERE project=?',('b'*64,project))
            return result
        with patch.object(self.preflight,'_source',side_effect=drift):
            with self.assertRaises(Conflict):self.preflight.owner_packet(self.owner,'drift')
        self.assertTrue(all(p['revision']==1 for p in self.registry.list(self.owner)))
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM project_connection_preflight_packets').fetchone()[0],0)

    def test_exact_selection_and_metadata_targets_are_bounded(self):
        for selection in ([],['unknown'],list(ACTIONS)*2):
            with self.assertRaises(ValueError):self.preflight.inspect(self.owner,selection)
        with self.assertRaises(ValueError):ProjectConnectionPreflight(self.store,self.registry,metadata=self.metadata()*2)
        with self.assertRaises(ValueError):self.preflight.inspect(self.owner,max_age=True)

    def test_new_provider_source_version_explicitly_invalidates_old_cached_evidence(self):
        project='bellavita-shop';self.seed_source()
        cfg=self.registry.get(self.owner,project)
        report={'project':project,'config_fingerprint':cfg['fingerprint'],
                'source_pin':{'project':project,'source_kind':'github','source_ref':cfg['config']['source_ref'],'branch':cfg['config']['branch'],'sha':'a'*40,'revision':'old'},
                'facts':[{'capability':'receiver','state':'verified','reference':'fixture/old','observed_at':1000}]}
        with self.store._transaction() as c:c.execute('INSERT INTO manager_connection_reports VALUES (?,?,?)',(project,json.dumps(report),1000))
        metadata=self.metadata();values={**vars(metadata[0]),'source_sha':'b'*40,'source_revision':'new'}
        service=ProjectConnectionPreflight(self.store,self.registry,self.fx.control,metadata=[MetadataObservation(**values),metadata[1]])
        item=service.inspect(self.owner,[project])['projects'][0]
        self.assertEqual(item['source_alignment']['observed_metadata_source_pin']['sha'],'b'*40)
        self.assertFalse(item['source_alignment']['cached_connections_match_metadata'])
        self.assertFalse(item['source_alignment']['latest_source_qa_verified'])
        self.assertIn('cached_connection_evidence_not_validated_for_latest_source',item['blocks'])
        receiver=next(f for f in item['facts'] if f['capability']=='receiver')
        self.assertEqual(receiver['classification'],'unverified')
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT sha FROM manager_source_heads WHERE project=?',(project,)).fetchone()[0],'a'*40)

    def test_latest_source_permission_denial_is_visible_without_a_saved_head(self):
        project='hope-sunrise';config=self.registry.get(self.owner,project)
        with self.store._transaction() as c:
            c.execute('INSERT INTO manager_source_observation_status VALUES (?,?,?,?)',(project,config['fingerprint'],'permission_denied',1000))
        item=self.preflight.inspect(self.owner,[project])['projects'][0]
        self.assertEqual(item['source']['classification'],'permission_denied')
        self.assertIsNone(item['source']['source_pin'])

    def test_same_sites_sha_different_provider_version_needs_new_evidence(self):
        project='hope-sunrise';self.seed_source(project)
        values={**vars(self.metadata(project)[0]),'source_sha':'a'*40,'source_revision':'v2'}
        service=ProjectConnectionPreflight(self.store,self.registry,self.fx.control,metadata=[MetadataObservation(**values)])
        item=service.inspect(self.owner,[project])['projects'][0]
        self.assertFalse(item['source_alignment']['cached_source_matches_metadata'])
        self.assertIn('provider_source_revision_differs_from_cached_source',item['blocks'])
