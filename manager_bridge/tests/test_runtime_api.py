import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from manager_bridge.store import Store, PermissionDenied, NotFound
from manager_bridge.maintenance_registry import Registry
from manager_bridge.representative_api import dispatch
from manager_bridge.project_adapters import ProjectProvider, ProviderResult, SOURCE_COMMITS
from manager_bridge.source_watch import HeadProbe, SourceHead
from manager_bridge.server import make_server
from manager_bridge.control import request


class Handler:
    def __init__(self,server):self.server=server;self.result=None
    def respond(self,status,result):self.status,self.result=status,result


class RuntimeAPI(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.now=1000
        self.store=Store(self.root/'manager.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*']);self.registry=Registry(self.store)
        self.configs=json.loads(Path('manager_bridge/maintenance_projects.json').read_text())
        for project,config in self.configs.items():self.registry.register(self.owner,project,config)
        self.manager=self.store.add_token('manager','manager',list(self.configs))
        self.erp_manager=self.store.add_token('erp-manager','manager',['bellavita-erp'])
        self.worker=self.store.add_token('worker','worker',['bellavita-erp'])
        self.server=SimpleNamespace(store=self.store)

    def call(self,route,args=None,*,method='POST',token=None):
        handler=Handler(self.server)
        self.assertTrue(dispatch(handler,method,'/v1/representative/runtime/'+route,token or self.manager,args))
        self.assertEqual(handler.status,200)
        return handler.result['result']

    def test_four_project_business_connections_default_to_not_configured(self):
        actions={'bellavita-erp':'orders.list','bellavita-shop':'orders.list','hope-sunrise':'products.list','funeral-association':'access.status'}
        for project,action in actions.items():
            result=self.call('business/read',{'project':project,'action':action})
            self.assertEqual(result['state'],'not_configured');self.assertIsNone(result['count'])
            self.assertFalse(result['operational_change'])

    def test_host_registered_provider_can_return_true_empty_without_activation(self):
        project='bellavita-erp';config=self.configs[project]
        self.server.project_business_providers={project:ProjectProvider(project,config['company'],config['source_ref'],config['branch'],SOURCE_COMMITS[project],
            lambda _:ProviderResult('ok','fixture/orders',self.now,{'orders':[]},SOURCE_COMMITS[project]))}
        result=self.call('business/read',{'project':project,'action':'orders.list'})
        self.assertEqual(result['state'],'empty');self.assertEqual(result['count'],0)
        self.assertFalse(self.registry.get(self.owner,project)['config']['maintenance_enabled'])

    def test_project_scope_checked_before_callback(self):
        with self.assertRaises(NotFound):self.call('business/read',{'project':'hope-sunrise','action':'products.list'},token=self.erp_manager)
        with self.assertRaises(PermissionDenied):self.call('business/read',{'project':'bellavita-erp','action':'orders.list'},token=self.worker)

    def test_request_cannot_supply_transport_model_or_score(self):
        requests=[('business/read',{'project':'bellavita-erp','action':'orders.list','providers':{}}),
            ('source/inspect',{'project':'bellavita-erp','observe':'code'}),
            ('failures/evaluate',{'project':'bellavita-erp','score':100}),
            ('receiver/check',{'project':'bellavita-erp','agent_id':'worker','execute':'code'})]
        for route,args in requests:
            with self.subTest(route=route),self.assertRaises(ValueError):self.call(route,args)

    def test_source_change_digest_is_available_through_single_manager_api(self):
        project='bellavita-erp';config=self.configs[project];head=['a'*40]
        self.server.project_source_probes={project:HeadProbe(config['source_kind'],config['source_ref'],config['branch'],
            lambda p:SourceHead(p,config['source_kind'],config['source_ref'],config['branch'],head[0],'fixture-v1','fixture/source',self.now))}
        self.assertEqual(self.call('source/inspect',{'project':project})['state'],'baseline')
        head[0]='b'*40
        changed=self.call('source/inspect',{'project':project});self.assertEqual(changed['state'],'changed')
        pending=self.call('source/pending',{'projects':[project]});self.assertEqual(len(pending['events']),1)
        self.assertFalse(self.call('source/ack',{'event_id':changed['event_id']})['approval_granted'])
        self.assertEqual(self.call('source/pending',{'projects':[project]})['events'],[])

    def test_failure_collection_and_model_measurement_are_not_fabricated(self):
        result=self.call('failures/collect',{'project':'bellavita-erp'})
        self.assertEqual(result['state'],'not_configured')
        report=self.call('failures/evaluate',{'project':'bellavita-erp'})
        self.assertEqual(report['fixture_reliability']['state'],'not_measured')
        self.assertEqual(report['model_lab']['status'],'not_measured')
        self.assertFalse(report['operational']['score_verified'])
        self.assertEqual(self.call('failures?project=bellavita-erp',method='GET'),[])

    def test_worker_receiver_missing_is_explicit_and_manager_cannot_execute(self):
        args={'project':'bellavita-erp','agent_id':'worker'}
        self.assertEqual(self.call('receiver/check',args,token=self.worker)['state'],'not_configured')
        with self.assertRaises(PermissionDenied):self.call('receiver/check',args)
        with self.assertRaises(NotFound):self.call('receiver/recovery',{**args,'project':'hope-sunrise'},token=self.worker)

    def test_strict_queries_and_unknown_run_route(self):
        with self.assertRaises(ValueError):self.call('source/inspect?project=bellavita-erp',{'project':'bellavita-erp'})
        with self.assertRaises(ValueError):self.call('failures?project=bellavita-erp&project=hope-sunrise',method='GET')
        self.assertFalse(dispatch(Handler(self.server),'POST','/v1/representative/runtime/receiver/run',self.worker,{}))

    def test_actual_loopback_transport_retains_auth_and_rejects_client_executor(self):
        server=make_server(self.store,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        try:
            data={'project':'bellavita-erp','action':'orders.list'}
            result=request(base,self.manager,'POST','/v1/representative/runtime/business/read',data)
            self.assertEqual(result['result']['state'],'not_configured')
            denied=request(base,self.worker,'POST','/v1/representative/runtime/business/read',data)
            self.assertEqual(denied['status'],403)
            bad=request(base,self.manager,'POST','/v1/representative/runtime/business/read',{**data,'execute':'ignored code'})
            self.assertEqual(bad['status'],400)
            self.assertNotIn('ignored code',json.dumps(bad))
        finally:server.shutdown();thread.join(timeout=3);server.server_close()
