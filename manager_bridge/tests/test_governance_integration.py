"""Actual authenticated loopback integration; no customer writes or model calls."""
import json
from unittest.mock import patch
import unittest
from manager_bridge.tests import test_governance_service as service_fixtures
from manager_bridge.tests import test_three_tier as hierarchy_fixtures
from manager_bridge.control import request
from manager_bridge.source_watch import HeadProbe, SourceHead

ROOT='/v1/representative/runtime/governance/'


class GovernanceIntegrationTests(unittest.TestCase):
    setUp=hierarchy_fixtures.ThreeTierTests.setUp
    observe=hierarchy_fixtures.ThreeTierTests.observe
    verify=hierarchy_fixtures.ThreeTierTests.verify
    compose=service_fixtures.GovernanceServiceTests.compose
    start_http=service_fixtures.GovernanceServiceTests.start_http

    def test_catalog_registration_report_dashboard_and_decisions_via_http(self):
        service=self.compose();base=self.start_http(service)
        projects=list(service.server.governance_onboarding.catalog())
        listing=request(base,self.owner,'POST',ROOT+'catalog/list',{'projects':projects})
        self.assertEqual(len(listing['result']['projects']),4)
        self.assertFalse(listing['result']['registration_performed'])
        body={'projects':projects,'expected_revisions':dict.fromkeys(projects,0)}
        self.assertEqual(request(base,self.manager,'POST',ROOT+'catalog/register',body)['status'],403)
        registered=request(base,self.owner,'POST',ROOT+'catalog/register',body)
        self.assertTrue(registered['ok']);self.assertFalse(registered['result']['execution_ready'])
        manager=self.store.add_token('catalog-manager','manager',projects)
        replay=request(base,self.owner,'POST',ROOT+'catalog/register',body)
        self.assertEqual(registered['result'],replay['result'])
        report=request(base,manager,'POST',ROOT+'portfolio/report',{'projects':projects})
        self.assertTrue(report['ok']);result=report['result']
        self.assertEqual(len(result['projects']),4)
        self.assertTrue(all(len(p['progress'])==10 for p in result['projects']))
        self.assertFalse(result['always_on_verified']);self.assertFalse(result['operational_authorization'])
        self.assertTrue(all(p['routing']['state']=='blocked' for p in result['projects']))
        self.assertFalse(any(self.registry.get(self.owner,p)['config']['maintenance_enabled'] for p in projects))
        dashboard=request(base,manager,'POST',ROOT+'portfolio/dashboard',{'projects':projects})
        self.assertIn('중앙 거버넌스 현황',dashboard['result']['html'])
        self.assertNotIn('<script',dashboard['result']['html'])
        packet=request(base,manager,'POST',ROOT+'portfolio/decision-packet',{'projects':projects})
        self.assertEqual(len(packet['result']['owner_required']),4)
        self.assertFalse(packet['result']['technical_acceptance'])

    def test_strict_transport_and_cross_project_scope(self):
        service=self.compose();base=self.start_http(service)
        p=next(iter(service.server.governance_onboarding.catalog()))
        denied=request(base,self.manager,'POST',ROOT+'catalog/list',{'projects':[p]})
        self.assertEqual(denied['status'],403)
        body={'projects':['project'],'callback':'do-not-execute'}
        bad=request(base,self.manager,'POST',ROOT+'portfolio/report',body)
        self.assertEqual(bad['status'],400);self.assertNotIn('do-not-execute',json.dumps(bad))
        for action in ('report','dashboard','decision-packet'):
            self.assertEqual(request(base,self.dev,'POST',ROOT+'portfolio/'+action,{'projects':['project']})['status'],403)

    def test_current_process_health_is_measured_without_persistent_host_claim(self):
        service=self.compose();seen=[]
        self.assertEqual(service.health(self.owner,['project'])['state'],'unavailable')
        def source(project):
            return SourceHead(project,'github','fixture/project','main','a'*40,'fixture-head','fixture/head',self.now)
        service.server.source_watch.probes['project']=HeadProbe('github','fixture/project','main',source)
        def tick(*args,**kwargs):
            seen.append(service.health(self.owner,['project']))
            return {}
        with patch.object(self.controller,'tick',side_effect=tick):
            result=service.run(max_ticks=1)
        self.assertEqual(seen[0]['state'],'running');self.assertEqual(seen[0]['http_state'],'listening')
        self.assertEqual(service.health(self.owner,['project'])['state'],'stopping')
        self.assertFalse(result['always_on_verified'])

    def test_governance_disabled_does_not_offer_unregistered_portfolio(self):
        service=self.compose(False);base=self.start_http(service)
        report=request(base,self.manager,'POST',ROOT+'portfolio/report',{'projects':['project']})
        self.assertEqual(report['result']['state'],'not_configured')
        self.assertFalse(report['result']['operational_authorization'])

    def test_registered_inactive_project_keeps_service_available_without_dispatch(self):
        registered=self.registry.get(self.owner,'project')
        self.registry.register(self.owner,'project',{**registered['config'],'maintenance_enabled':False},expected_revision=registered['revision'])
        service=self.compose()
        with patch.object(self.controller,'tick',side_effect=AssertionError('inactive dispatch')):
            result=service.run(max_ticks=1)
        self.assertEqual(result['inactive_projects'],['project'])
        self.assertEqual(result['source_blocked_ticks'],0)
        self.assertEqual(result['state'],'bounded_run_complete')


if __name__=='__main__':unittest.main()
