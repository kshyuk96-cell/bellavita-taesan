import tempfile
import unittest
from pathlib import Path
from manager_bridge.store import Store,PermissionDenied
from manager_bridge.connections import Connections,Probe


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.now=1000
        self.store=Store(root/'state.db',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*']);self.store.register_project(self.owner,'a','fixture/a')
        self.manager=self.store.add_token('manager','manager',['a']);self.worker=self.store.add_token('worker','worker',['a'])

    def test_missing_permission_and_ready_are_distinct_and_expire(self):
        probes={'sheet_copy':Probe(lambda _:dict(state='permission_denied',evidence_reference='fixture:scope')),
                'native_development':Probe(lambda _:dict(state='ready',evidence_reference='fixture:native'))}
        checklist=Connections(self.store,probes)
        result=checklist.inspect(self.manager,'a');states={item['name']:item['state'] for item in result['connections']}
        self.assertEqual(states['sheet_copy'],'permission_denied');self.assertEqual(states['erp'],'not_configured')
        self.assertEqual(states['native_development'],'ready');self.assertFalse(result['configuration_changed'])
        self.now+=301;self.assertTrue(checklist.latest(self.manager,'a')['stale'])
        with self.assertRaises(PermissionDenied):checklist.inspect(self.worker,'a')

    def test_provider_errors_or_private_payloads_are_not_retained(self):
        def failed(_):raise RuntimeError('private raw token '+self.manager)
        checklist=Connections(self.store,{'erp':Probe(failed),
            'kakao_dev':Probe(lambda _:dict(state='ready',evidence_reference=self.manager))})
        result=checklist.inspect(self.manager,'a')
        self.assertEqual(result['connections'][0]['state'],'unknown')
        self.assertNotIn(self.manager,str(result));self.assertNotIn('private raw',str(result))
        with self.assertRaises(ValueError):Probe(failed,read_only=False)


if __name__=='__main__':unittest.main()
