from pathlib import Path
import tempfile
import unittest
from manager_bridge.store import Store, Conflict, PermissionDenied, NotFound, token_hash
from manager_bridge.maintenance_registry import Registry
from manager_bridge.delegation import Delegation


class DelegationBatchTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.now=1000
        self.store=Store(self.root/'state.sqlite3',clock=lambda:self.now)
        self.owner=self.store.add_token('owner','owner',['*']);self.registry=Registry(self.store)
        for project in ('erp','shop','hope','association'):
            self.registry.register(self.owner,project,{'company':'bellavita' if project in ('erp','shop') else project,
                'name':project,'source_kind':'github','source_ref':'fixture/'+project,'branch':'main',
                'services':[],'maintenance_enabled':False})
        self.manager=self.store.add_token('manager','manager',['erp','shop','hope','association'])
        self.erp_owner=self.store.add_token('erp-owner','owner',['erp'])
        self.delegation=Delegation(self.store,self.registry)

    def proposal(self,project,owner=None):
        return self.delegation.propose(owner or self.owner,project,token_hash(self.manager),allowed_paths=['tests/'],
            allowed_checks=['authorization_regression','approval_regression'],kinds=['test'],max_tasks=2,expires_at=1500)

    @staticmethod
    def item(row):return {'id':row['id'],'fingerprint':row['fingerprint']}

    def test_four_project_owner_decision_remains_development_only_and_disabled(self):
        proposals=[self.proposal(p) for p in ('erp','shop','hope','association')]
        result=self.delegation.activate_batch(self.owner,[self.item(x) for x in proposals])
        self.assertEqual(len(result['delegations']),4);self.assertTrue(result['atomic'])
        self.assertFalse(result['project_execution_activated']);self.assertFalse(result['operational_approval'])
        inventory=self.delegation.inventory(self.manager,['erp','shop','hope','association'])
        self.assertTrue(all(x['effective_state']=='blocked' and x['remaining_tasks']==2 for x in inventory['delegations']))
        self.assertTrue(all(not x['config']['maintenance_enabled'] for x in self.registry.list(self.owner)))

    def test_invalid_second_approval_rolls_back_first(self):
        first=self.proposal('erp');second=self.proposal('hope')
        with self.assertRaises(Conflict):self.delegation.activate_batch(self.owner,[self.item(first),{'id':second['id'],'fingerprint':'0'*64}])
        self.assertEqual(self.delegation.get(self.owner,first['id'])['state'],'pending_approval')

    def test_cross_owner_scope_rolls_back_entire_batch(self):
        first=self.proposal('erp',self.erp_owner);second=self.proposal('hope')
        with self.assertRaises(NotFound):self.delegation.activate_batch(self.erp_owner,[self.item(first),self.item(second)])
        self.assertEqual(self.delegation.get(self.owner,first['id'])['state'],'pending_approval')

    def test_manager_cannot_self_approve_batch(self):
        proposal=self.proposal('erp')
        with self.assertRaises(PermissionDenied):self.delegation.activate_batch(self.manager,[self.item(proposal)])

    def test_expired_second_proposal_rolls_back_active_transition(self):
        first=self.proposal('erp');second=self.proposal('shop');self.now=1500
        with self.assertRaises(Conflict):self.delegation.activate_batch(self.owner,[self.item(first),self.item(second)])
        self.assertEqual(self.delegation.get(self.owner,first['id'])['state'],'pending_approval')

    def test_repeated_exact_batch_is_idempotent_but_revoked_cannot_reactivate(self):
        row=self.proposal('erp');items=[self.item(row)]
        self.delegation.activate_batch(self.owner,items);self.delegation.activate_batch(self.owner,items)
        self.delegation.revoke(self.owner,row['id'])
        with self.assertRaises(Conflict):self.delegation.activate_batch(self.owner,items)

    def test_duplicate_or_extra_fields_are_not_approval(self):
        row=self.proposal('erp');item=self.item(row)
        for items in ([],[item,item],[{**item,'approval':'ㅇㅇ'}]):
            with self.subTest(items=items),self.assertRaises(ValueError):self.delegation.activate_batch(self.owner,items)

    def test_inventory_does_not_leak_other_projects(self):
        self.proposal('hope')
        with self.assertRaises(NotFound):self.delegation.inventory(self.erp_owner,['erp','hope'])
        self.assertEqual(self.delegation.inventory(self.erp_owner,['erp'])['delegations'],[])
