"""Atomic twenty-item manager preparation; owner authority remains explicit."""
from unittest.mock import patch
import unittest
from manager_bridge.tests import test_proposal_board as fixtures
from manager_bridge.store import Conflict, PermissionDenied, NotFound


class ControlBundleTests(unittest.TestCase):
    setUp=fixtures.ProposalTests.setUp
    spec=fixtures.ProposalTests.spec
    approve=fixtures.ProposalTests.approve
    def specs(self,count=20):
        return [{**self.spec('shop' if i%2 else 'erp'),
                 'description':'Synthetic item '+str(i),'allowed_paths':['fixture/item'+str(i)+'/'],
                 'priority':('P0','P1','P2','P3')[i%4]} for i in range(count)]

    def test_twenty_items_atomic_approval_and_reopen_idempotency(self):
        specs=self.specs();batch=self.board.propose_bundle(self.manager,'twenty',specs)
        self.assertEqual(len(batch['items']),20);self.assertEqual(batch['tasks'],[])
        self.assertEqual(self.board.propose_bundle(self.manager,'twenty',specs),batch)
        with self.assertRaises(PermissionDenied):self.approve(batch,self.manager)
        approved=self.approve(batch)
        self.assertEqual(len(approved['tasks']),20)
        from manager_bridge.proposal_board import ProposalBoard
        from manager_bridge.maintenance_registry import Registry
        from manager_bridge.store import Store
        reopened=Store(self.store.path,clock=lambda:1000)
        board=ProposalBoard(reopened,Registry(reopened))
        self.assertEqual(board.propose_bundle(self.manager,'twenty',specs),approved)
        self.assertEqual(board.approve_batch(self.owner,batch['id'],batch['digest']),approved)
        self.assertEqual(len(self.store.list_tasks(self.manager,'erp'))+len(self.store.list_tasks(self.manager,'shop')),20)

    def test_bundle_rejects_duplicate_oversize_changed_and_cross_scope(self):
        for value in ([],self.specs(21),[self.spec(),self.spec()]):
            with self.assertRaises(ValueError):self.board.propose_bundle(self.manager,'bad',value)
        with self.assertRaises(NotFound):self.board.propose_bundle(self.limited,'scope',self.specs())
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT count(*) FROM manager_proposals').fetchone()[0],0)
        self.board.propose_bundle(self.manager,'fixed',self.specs(2))
        changed=self.specs(2);changed[-1]['priority']='P0'
        with self.assertRaises(Conflict):self.board.propose_bundle(self.manager,'fixed',changed)
        with self.assertRaises(Conflict):self.board.propose_bundle(self.manager,'fixed',self.specs(1))

    def test_insertion_failure_rolls_back_all_preparation(self):
        original=self.board._insert_proposal;calls=[]
        def insert(*args):
            calls.append(1)
            if len(calls)==20:raise Conflict('synthetic final item failure')
            return original(*args)
        with patch.object(self.board,'_insert_proposal',side_effect=insert):
            with self.assertRaises(Conflict):self.board.propose_bundle(self.manager,'rollback',self.specs())
        with self.store._transaction() as c:
            for table in ('manager_proposals','manager_proposal_batches','bridge_tasks'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_last_task_approval_failure_rolls_back_twenty_tasks(self):
        batch=self.board.propose_bundle(self.manager,'owner-rollback',self.specs())
        original=self.store._event;calls=[]
        def event(c,task,actor,action,state,payload=None):
            if action=='approved':
                calls.append(1)
                if len(calls)==20:raise Conflict('synthetic last approval failure')
            return original(c,task,actor,action,state,payload)
        with patch.object(self.store,'_event',side_effect=event):
            with self.assertRaises(Conflict):self.approve(batch)
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM bridge_tasks').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM bridge_events').fetchone()[0],0)
        self.assertEqual(self.board.get_batch(self.manager,batch['id'])['state'],'pending_owner_approval')
