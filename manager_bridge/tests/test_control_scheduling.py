"""Approved scheduling hints preserve core authority and bounded fair scanning."""
import unittest
from manager_bridge.tests import test_three_tier as fixtures
from manager_bridge.tests import test_qa_admission as qa_fixtures
from manager_bridge.continuous_manager import ContinuousManager
from manager_bridge.proposal_board import ProposalBoard
from manager_bridge.source_watch import SourceWatch
from manager_bridge.store import Store,token_hash,digest
from manager_bridge.three_tier import ThreeTier
from manager_bridge.maintenance_registry import Registry


class ControlSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.ThreeTierTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.scheduler=ContinuousManager(self.fx.core)

    def plans(self,limit=200):
        with self.fx.store._transaction() as c:
            actor,configs=self.scheduler._auth(c,self.fx.manager,['project'])
            return self.scheduler._plans(c,actor,configs,limit)

    def board_setup(self):
        SourceWatch(self.fx.store,self.fx.registry)
        qa=qa_fixtures.QAAdmissionTests();qa.fixture();self.contract=qa.expected_contract
        self.fx.core.governance_gate.register_policy(self.fx.owner,'project',contract_version='fixture',expected_contract=self.contract,required_coverage=['money'],provider_identity='fixture')
        config=self.fx.registry.get(self.fx.owner,'project')
        with self.fx.store._transaction() as c:
            c.execute('INSERT INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',('project',config['fingerprint'],'a'*40,'fixture','fixture/head',self.fx.now,0))
        self.board=ProposalBoard(self.fx.store,self.fx.registry)

    def proposal(self,key,priority,approve=True):
        spec={'project':'project','repository':'fixture/project','base_sha':'a'*40,'description':'Approved scheduling fixture','checks':['fixture-check'],'kind':'development','allowed_paths':['src/'],'dependencies':[],'qa_contract_ref':'fixture','qa_contract_digest':digest(self.contract),'priority':priority,'risk_rationale':'Synthetic priority test'}
        proposed=self.board.propose(self.fx.manager,key,spec)
        batch=self.board.create_batch(self.fx.manager,key,[proposed['id']])
        if approve:return self.board.approve_batch(self.fx.owner,batch['id'],batch['digest'])['tasks'][0]
        return proposed

    def test_exact_approved_priority_orders_only_authorized_work(self):
        self.board_setup();low=self.proposal('low','P3');high=self.proposal('high','P0');self.proposal('unapproved','P0',False);self.fx.task(approved=False)
        self.assertEqual([high['id'],low['id']],[p.task_id for p in self.plans()])
        with self.fx.store._transaction() as c:
            row=c.execute('SELECT * FROM bridge_tasks WHERE id=?',(low['id'],)).fetchone()
            self.assertEqual(3,self.scheduler._approved_priority(c,row))
            # A row whose manifest binding changed cannot acquire a priority hint.
            changed=dict(row);changed['manifest_digest']='f'*64
            self.assertEqual(2,self.scheduler._approved_priority(c,changed))
        self.assertEqual(0,self.fx.host_calls)

    def test_collect_review_precedes_new_start_even_when_start_older(self):
        first=self.fx.task();second,candidate=self.fx.reported()
        plans=self.plans()
        self.assertEqual(('review',second['id']),(plans[0].phase,plans[0].task_id))
        self.assertEqual(('start',first['id']),(plans[1].phase,plans[1].task_id))

    def test_recovery_plan_precedes_review_backlog(self):
        pending=self.fx.task()
        def fail(action,heartbeat):raise OSError('lost acknowledgement')
        broken=ContinuousManager(self.fx.core,handlers={'start':fail})
        broken.tick(self.fx.manager,['project'],'failed-start')
        reviewed,_=self.fx.reported()
        plans=self.plans()
        self.assertEqual(('reconcile',pending['id']),(plans[0].phase,plans[0].task_id))
        self.assertEqual(('review',reviewed['id']),(plans[1].phase,plans[1].task_id))

    def test_fair_cursor_advances_beyond_200_approved_tasks(self):
        # Explicit fixture rows share an owner-approved manifest; this test never
        # executes them. Each has its own exact task policy and durable identity.
        initial=self.fx.task()
        with self.fx.store._transaction() as c:
            source=dict(c.execute('SELECT * FROM bridge_tasks WHERE id=?',(initial['id'],)).fetchone())
            columns=list(source);sql='INSERT INTO bridge_tasks ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')'
            policy=c.execute('SELECT policy,policy_digest FROM bridge_task_policy WHERE task=?',(initial['id'],)).fetchone()
            for index in range(204):
                row={**source,'id':format(index+1,'032x'),'request_key':'fair-'+str(index),'created':self.fx.now+index+1}
                c.execute(sql,[row[k] for k in columns]);c.execute('INSERT INTO bridge_task_policy VALUES (?,?,?)',(row['id'],policy['policy'],policy['policy_digest']))
        first=self.plans();second=self.plans()
        self.assertEqual((200,5),(len(first),len(second)))
        self.assertEqual(205,len({p.task_id for p in first+second}))
        self.assertFalse({p.task_id for p in first}&{p.task_id for p in second})

    def test_dependency_waiting_and_repository_lock_capacity_unchanged(self):
        parent=self.fx.task();child=self.fx.task(dependencies=[parent['id']],base='b'*40)
        self.assertNotIn(child['id'],[p.task_id for p in self.plans()])
        self.fx.start(parent)
        unrelated=self.fx.task()
        self.fx.core.start(self.fx.manager,unrelated['id'],'dev','second')
        result=self.fx.coord.route(self.fx.owner,unrelated['id'],worker_hash=token_hash(self.fx.dev))
        self.assertEqual('repository_busy_or_stop_unconfirmed',result['status'])
        self.assertIsNone(self.fx.core.claim_development(self.fx.dev,unrelated['id']))
        self.fx.core.set_limits(self.fx.owner,'project',max_active=1,max_review=1,max_reviewers=1)
        self.assertIsNone(self.fx.core.claim_development(self.fx.dev,unrelated['id']))
        self.assertEqual(0,self.fx.host_calls)

    def test_reopened_store_preserves_uncertainty_without_automatic_replay(self):
        self.fx.task();calls=[]
        def fail(action,heartbeat):calls.append(action.task_id);raise OSError('lost acknowledgement')
        first=ContinuousManager(self.fx.core,handlers={'start':fail})
        first.tick(self.fx.manager,['project'],'first')
        self.fx.now+=31
        reopened=Store(self.fx.store.path,clock=lambda:self.fx.now);core=ThreeTier(reopened,Registry(reopened))
        restarted=ContinuousManager(core,handlers={'start':fail})
        result=restarted.tick(self.fx.manager,['project'],'replacement')
        self.assertEqual(1,len(calls))
        self.assertFalse(any(a['phase']=='start' and a['execution_started'] for a in result['actions']))
        with reopened._transaction() as c:
            self.assertEqual('uncertain',c.execute("SELECT state FROM continuous_actions WHERE phase='start'").fetchone()[0])
