"""Old decision discovery without mixed-scope disclosure or drifting cursors."""
import unittest
from manager_bridge.tests import test_proposal_board as fixtures
from manager_bridge.store import Conflict,NotFound


class BatchPaginationTests(unittest.TestCase):
    setUp=fixtures.ProposalTests.setUp
    spec=fixtures.ProposalTests.spec
    batch=fixtures.ProposalTests.batch

    def test_walk_reaches_all_old_batches_once_and_excludes_new_rows(self):
        original=[self.batch(key='old-'+str(i))['id'] for i in range(25)]
        first=self.board.page_batches(self.manager,['erp'],limit=7)
        self.batch(key='new-after-walk')
        ids=[b['id'] for b in first['items']];cursor=first['next_cursor']
        while cursor:
            page=self.board.page_batches(self.manager,['erp'],cursor=cursor,limit=7)
            ids.extend(b['id'] for b in page['items']);cursor=page['next_cursor']
        self.assertEqual(ids,original);self.assertEqual(len(ids),len(set(ids)))
        self.assertTrue(first['new_batches_require_new_walk']);self.assertFalse(first['business_approval'])

    def test_mixed_batch_hidden_and_scope_actor_config_and_expiry_bound(self):
        a=self.board.propose(self.manager,'erp',self.spec())
        b=self.board.propose(self.manager,'shop',self.spec('shop'))
        self.board.create_batch(self.manager,'mixed',[a['id'],b['id']])
        for i in range(3):self.batch(key='solo-'+str(i))
        broader=self.board.page_batches(self.manager,['erp'],limit=1)
        self.assertTrue(broader['visibility_partial']);self.assertNotIn('shop',str(broader))
        page=self.board.page_batches(self.limited,['erp'],limit=1)
        self.assertTrue(page['visibility_partial']);self.assertEqual(len(page['items']),1)
        self.assertNotIn('shop',str(page))
        cursor=page['next_cursor']
        with self.assertRaises(NotFound):self.board.page_batches(self.manager,['erp'],cursor=cursor)
        with self.assertRaises(NotFound):self.board.page_batches(self.limited,['erp','shop'],cursor=cursor)
        self.registry.register(self.owner,'erp',self.config['erp'],expected_revision=1)
        with self.assertRaises(Conflict):self.board.page_batches(self.limited,['erp'],cursor=cursor)

    def test_cursor_expired_worker_and_unbounded_requests_rejected(self):
        self.batch(key='one');self.batch(key='two')
        page=self.board.page_batches(self.manager,['erp'],limit=1)
        with self.store._transaction() as c:c.execute('UPDATE manager_batch_cursors SET expires=999')
        with self.assertRaises(Conflict):self.board.page_batches(self.manager,['erp'],cursor=page['next_cursor'])
        from manager_bridge.store import PermissionDenied
        with self.assertRaises(PermissionDenied):self.board.page_batches(self.worker,['erp'])
        for projects,limit in (([],20),(['erp','erp'],20),(['erp'],True),(['erp'],51)):
            with self.assertRaises(ValueError):self.board.page_batches(self.manager,projects,limit=limit)
