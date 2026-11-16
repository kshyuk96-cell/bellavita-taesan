import tempfile
import unittest
from pathlib import Path
from manager_bridge.store import Store, Conflict, NotFound, PermissionDenied, token_hash, digest
from manager_bridge.maintenance_registry import Registry
from manager_bridge.source_watch import SourceWatch
from manager_bridge.proposal_board import ProposalBoard, validate_execution


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory())
        self.store=Store(Path(self.temp)/'board.db',clock=lambda:1000)
        self.owner=self.store.add_token('owner','owner',['*'])
        self.registry=Registry(self.store)
        SourceWatch(self.store,self.registry)
        self.config={}
        from manager_bridge.governance_gate import GovernanceGate
        from manager_bridge.tests.test_qa_admission import QAAdmissionTests
        fixture=QAAdmissionTests();fixture.fixture();self.contract=fixture.expected_contract
        self.gate=GovernanceGate(self.store,self.registry)
        for project in ('erp','shop'):
            cfg={'company':'fixture','name':project,'source_kind':'github','source_ref':'fixture/'+project,'branch':'main','services':[],'maintenance_enabled':False}
            self.config[project]=cfg
            value=self.registry.register(self.owner,project,cfg)
            self.gate.register_policy(self.owner,project,contract_version='fixture/qa',expected_contract=self.contract,required_coverage=['money'],provider_identity='synthetic-fixture')
            with self.store._transaction() as c:
                c.execute('INSERT INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',(project,value['fingerprint'],'a'*40,'rev1','fixture/head',1000,1))
        self.manager=self.store.add_token('manager','manager',['erp','shop'])
        self.limited=self.store.add_token('limited','manager',['erp'])
        self.worker=self.store.add_token('worker','worker',['erp'])
        self.board=ProposalBoard(self.store,self.registry)

    def spec(self,project='erp'):
        return {'project':project,'repository':'fixture/'+project,'base_sha':'a'*40,'description':'Synthetic fixture only','checks':['synthetic_check'],'kind':'development','allowed_paths':['fixture/'],'dependencies':[],'qa_contract_ref':'fixture/qa','qa_contract_digest':digest(self.contract),'priority':'P2','risk_rationale':'Synthetic scoped fixture'}

    def batch(self,spec=None,key='one'):
        proposal=self.board.propose(self.manager,key,spec or self.spec())
        return self.board.create_batch(self.manager,key,[proposal['id']])

    def approve(self,batch,token=None):
        return self.board.approve_batch(token or self.owner,batch['id'],batch['digest'])

    def test_immutable_scope_idempotency_and_structured_owner_decision(self):
        batch=self.batch()
        self.assertFalse(batch['business_approval'])
        with self.assertRaises(PermissionDenied):self.approve(batch,self.manager)
        with self.assertRaises(PermissionDenied):self.approve(batch,self.worker)
        with self.assertRaises(Conflict):self.board.approve_batch(self.owner,batch['id'],'c'*64)
        result=self.approve(batch)
        self.assertEqual(result,self.approve(batch))
        self.assertFalse(result['technical_acceptance']);self.assertFalse(result['native_delivery_authorized'])
        task=result['tasks'][0]
        self.assertEqual(task['state'],'queued')
        self.assertEqual(len(self.store.list_tasks(self.manager,'erp')),1)
        changed=self.spec();changed['allowed_paths']=['other/']
        with self.assertRaises(Conflict):self.board.propose(self.manager,'one',changed)
        with self.store._transaction() as c:
            row=self.store._get(c,self.store._authenticate(c,self.owner),task['id'])
            validate_execution(c,self.store,row)

    def test_cross_project_access_and_dependency_are_rejected(self):
        proposal=self.board.propose(self.manager,'shop',self.spec('shop'))
        with self.assertRaises(NotFound):self.board.get_proposal(self.limited,proposal['id'])
        parent=self.approve(self.batch(self.spec('shop'),'parent'))['tasks'][0]
        spec=self.spec();spec['dependencies']=[parent['id']]
        batch=self.batch(spec,'dependent')
        with self.assertRaises(Conflict):self.approve(batch)
        self.assertEqual(self.store.list_tasks(self.manager,'erp'),[])

    def test_same_project_dependencies_bound_to_exact_approved_task(self):
        parent=self.approve(self.batch(key='parent'))['tasks'][0]
        spec=self.spec();spec['dependencies']=[parent['id']]
        child=self.approve(self.batch(spec,'child'))['tasks'][0]
        with self.store._transaction() as c:
            import json
            policy=json.loads(c.execute('SELECT policy FROM bridge_task_policy WHERE task=?',(child['id'],)).fetchone()[0])
        self.assertEqual(policy['dependencies'][0]['manifest_digest'],parent['manifest_digest'])

    def test_source_and_config_drift_require_new_proposal(self):
        batch=self.batch()
        with self.store._transaction() as c:c.execute("UPDATE manager_source_heads SET revision='rev2' WHERE project='erp'")
        with self.assertRaises(Conflict):self.approve(batch)
        with self.assertRaises(Conflict):self.board.propose(self.manager,'one',self.spec())
        self.assertEqual(self.store.list_tasks(self.manager,'erp'),[])

    def test_execution_fence_invalidates_after_config_change(self):
        task=self.approve(self.batch())['tasks'][0]
        self.registry.register(self.owner,'erp',self.config['erp'],expected_revision=1)
        with self.store._transaction() as c:
            row=self.store._get(c,self.store._authenticate(c,self.owner),task['id'])
            with self.assertRaises(Conflict):self.store._validate_execution(c,row)

    def test_partial_failure_rolls_back_tasks_policies_and_events(self):
        a=self.board.propose(self.manager,'a',self.spec())
        b=self.board.propose(self.manager,'b',self.spec('shop'))
        batch=self.board.create_batch(self.manager,'batch',[a['id'],b['id']])
        original=self.store._event
        approved=[]
        def fail(c,task,actor,action,state,payload=None):
            original(c,task,actor,action,state,payload)
            if action=='approved':approved.append(task)
            if len(approved)==2:raise RuntimeError('synthetic second-task failure')
        self.store._event=fail
        with self.assertRaises(RuntimeError):self.approve(batch)
        self.store._event=original
        self.assertEqual(self.store.list_tasks(self.manager,'erp'),[])
        self.assertEqual(self.store.list_tasks(self.manager,'shop'),[])
        with self.store._transaction() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM bridge_events').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM bridge_task_policy').fetchone()[0],0)
        self.assertEqual(len(self.approve(batch)['tasks']),2)

    def test_authority_revocation_during_approval_rolls_back(self):
        batch=self.batch();original=self.store._event
        def revoke(c,*args,**kwargs):
            original(c,*args,**kwargs)
            c.execute('UPDATE bridge_tokens SET revoked=1000 WHERE token_hash=?',(token_hash(self.owner),))
        self.store._event=revoke
        with self.assertRaises(PermissionDenied):self.approve(batch)
        self.store._event=original
        self.assertEqual(self.store.list_tasks(self.manager,'erp'),[])

    def test_business_material_and_unbounded_batches_never_approve(self):
        with self.assertRaises(ValueError):self.board.propose(self.manager,'one',self.spec(),source='business_material')
        with self.assertRaises(ValueError):self.board.create_batch(self.manager,'batch',['a'+str(i) for i in range(21)])
        with self.assertRaises(PermissionDenied):self.board.propose(self.worker,'one',self.spec())

    def test_source_drift_after_approval_blocks_execution_and_reports_new_decision(self):
        batch=self.batch();task=self.approve(batch)['tasks'][0]
        with self.store._transaction() as c:
            c.execute("UPDATE manager_source_heads SET revision='rev2' WHERE project='erp'")
            row=self.store._get(c,self.store._authenticate(c,self.owner),task['id'])
            with self.assertRaises(Conflict):self.store._validate_execution(c,row)
        report=self.board.get_batch(self.manager,batch['id'])
        self.assertEqual(report['state'],'invalidated')
        self.assertEqual(report['required_decision'],'new_proposal_snapshot')
        self.assertFalse(report['business_approval'])
        self.assertTrue(report['historical_business_approval'])

    def test_revoked_owner_blocks_previously_approved_execution(self):
        task=self.approve(self.batch())['tasks'][0]
        self.store.revoke_token(self.owner,token_hash(self.owner))
        with self.store._transaction() as c:
            row=self.store._get(c,self.store._authenticate(c,self.manager),task['id'])
            with self.assertRaises(Conflict):self.store._validate_execution(c,row)

    def test_stale_future_and_nonfinite_source_metadata_fail_closed(self):
        batch=self.batch()
        for observed in (1,1001,float('inf'),float('-inf'),None):
            with self.subTest(observed=observed):
                with self.store._transaction() as c:
                    c.execute("UPDATE manager_source_heads SET observed_at=? WHERE project='erp'",(observed,))
                with self.assertRaises(Conflict):self.board.propose(self.manager,'new',self.spec())
                with self.assertRaises(Conflict):self.board.create_batch(self.manager,'new-batch',[batch['items'][0]['id']])
                with self.assertRaises(Conflict):self.approve(batch)
                self.assertEqual(self.board.get_batch(self.manager,batch['id'])['state'],'invalidated')

    def test_source_freshness_execution_cannot_be_downgraded_by_flag(self):
        batch=self.batch();task=self.approve(batch)['tasks'][0]
        with self.store._transaction() as c:
            c.execute("UPDATE manager_source_heads SET observed_at=1 WHERE project='erp'")
            row=self.store._get(c,self.store._authenticate(c,self.owner),task['id'])
            for check in (False,True):
                with self.assertRaises(Conflict):self.store._validate_execution(c,row,check_source_age=check)

    def test_unchanged_source_timestamp_refresh_preserves_exact_approval(self):
        batch=self.batch()
        with self.store._transaction() as c:
            c.execute("UPDATE manager_source_heads SET observed_at=999 WHERE project='erp'")
        self.assertTrue(self.approve(batch)['business_approval'])

    def test_owner_approval_requires_registered_exact_qa_contract(self):
        for missing in (False,True):
            batch=self.batch(key='missing' if missing else 'mismatch')
            with self.store._transaction() as c:
                if missing:c.execute("DELETE FROM governance_qa_policies WHERE project='erp'")
                else:
                    import json
                    row=c.execute("SELECT policy FROM governance_qa_policies WHERE project='erp'").fetchone()
                    policy=json.loads(row['policy']);policy['contract_ref']='other/contract'
                    c.execute("UPDATE governance_qa_policies SET policy=?,policy_digest=? WHERE project='erp'",(json.dumps(policy),digest(policy)))
            with self.assertRaises(Conflict):self.approve(batch)
            self.assertEqual(self.store.list_tasks(self.manager,'erp'),[])

    def test_durable_negative_source_status_blocks_fresh_baseline(self):
        batch=self.batch();task=self.approve(batch)['tasks'][0]
        with self.store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS manager_source_observation_status (project TEXT PRIMARY KEY,config_fingerprint TEXT,state TEXT,checked_at REAL)')
            fingerprint=self.registry._get(c,self.owner,'erp')['fingerprint']
            c.execute('INSERT OR REPLACE INTO manager_source_observation_status VALUES (?,?,?,?)',('erp',fingerprint,'unavailable',1000))
            row=self.store._get(c,self.store._authenticate(c,self.owner),task['id'])
            with self.assertRaisesRegex(Conflict,'unavailable'):self.store._validate_execution(c,row)
        with self.assertRaisesRegex(Conflict,'unavailable'):self.approve(batch)
        with self.assertRaisesRegex(Conflict,'unavailable'):self.board.propose(self.manager,'new',self.spec())
        with self.store._transaction() as c:
            c.execute("UPDATE manager_source_observation_status SET state='unchanged' WHERE project='erp'")
        self.assertTrue(self.approve(batch)['business_approval'])

    def test_qa_policy_rollover_invalidates_previous_owner_approval(self):
        batch=self.batch();task=self.approve(batch)['tasks'][0]
        with self.store._transaction() as c:
            old=c.execute("SELECT policy_digest FROM governance_qa_policies WHERE project='erp'").fetchone()[0]
        self.gate.register_policy(self.owner,'erp',contract_version='next',contract_ref='fixture/qa',expected_contract=self.contract,required_coverage=['money'],provider_identity='new-synthetic-provider',expected_policy_digest=old)
        with self.store._transaction() as c:
            row=self.store._get(c,self.store._authenticate(c,self.owner),task['id'])
            with self.assertRaisesRegex(Conflict,'QA policy changed'):self.store._validate_execution(c,row)


    def test_durable_enrollment_blocks_legacy_development_execution_and_restart(self):
        manifest={k:self.spec()[k] for k in ('project','repository','base_sha','description','checks','kind')}
        legacy=self.store.submit(self.owner,'legacy-direct',manifest)
        self.store.approve(self.owner,legacy['id'],legacy['version'],legacy['manifest_digest'])
        with self.store._transaction() as c:
            row=self.store._get(c,self.store._authenticate(c,self.owner),legacy['id'])
            self.store._validate_execution(c,row)
            c.execute('CREATE TABLE IF NOT EXISTS governance_enrolled_projects (project TEXT PRIMARY KEY,enrolled_at REAL NOT NULL)')
            c.execute('INSERT OR IGNORE INTO governance_enrolled_projects VALUES (?,?)',('erp',1000))
            with self.assertRaisesRegex(Conflict,'ProposalBoard'):self.store._validate_execution(c,row)
        restarted=Store(self.store.path,clock=lambda:1000)
        with restarted._transaction() as c:
            row=restarted._get(c,restarted._authenticate(c,self.owner),legacy['id'])
            with self.assertRaisesRegex(Conflict,'ProposalBoard'):restarted._validate_execution(c,row)
        self.assertEqual(self.store.get_task(self.manager,legacy['id'])['state'],'queued')
        self.assertIsNone(self.store.claim(self.worker,'erp',task_id=legacy['id']))
        with self.store._transaction() as c:
            import json
            row=self.store._get(c,self.store._authenticate(c,self.owner),legacy['id'])
            forged=json.loads(row['approval']);forged['proposal_batch_id']='forged';forged['proposal_id']='forged';forged['proposal_digest']='a'*64
            c.execute('UPDATE bridge_tasks SET approval=? WHERE id=?',(json.dumps(forged),legacy['id']))
            row=self.store._get(c,self.store._authenticate(c,self.owner),legacy['id'])
            with self.assertRaisesRegex(Conflict,'proposal approval binding'):self.store._validate_execution(c,row)
        self.assertNotEqual(self.store.get_task(self.manager,legacy['id'])['state'],'running')

    def test_enrolled_project_board_origin_requires_exact_task_link(self):
        with self.store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS governance_enrolled_projects (project TEXT PRIMARY KEY,enrolled_at REAL NOT NULL)')
            c.execute('INSERT OR IGNORE INTO governance_enrolled_projects VALUES (?,?)',('erp',1000))
        batch=self.batch();task=self.approve(batch)['tasks'][0]
        with self.store._transaction() as c:
            row=self.store._get(c,self.store._authenticate(c,self.owner),task['id'])
            self.store._validate_execution(c,row)
            c.execute('UPDATE manager_proposals SET task=NULL WHERE id=?',(batch['items'][0]['id'],))
            with self.assertRaisesRegex(Conflict,'task link'):self.store._validate_execution(c,row)


class DerivedProposalTests(unittest.TestCase):
    def setUp(self):
        from manager_bridge.tests.test_governance_gate import GovernanceGateTests
        self.fx=GovernanceGateTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.fx.register()
        self.parent,self.accepted=self.fx.accept()
        self.host=self.fx.fx
        self.store=self.host.store;self.owner=self.host.owner;self.manager=self.host.manager
        self.board=ProposalBoard(self.store,self.host.registry)
        SourceWatch(self.store,self.host.registry)
        config=self.host.registry.get(self.owner,'project')
        with self.store._transaction() as c:
            c.execute('INSERT INTO manager_source_heads VALUES (?,?,?,?,?,?,?)',('project',config['fingerprint'],'a'*40,'remote-v1','fixture/remote',self.host.now,1))
        self.spec={'project':'project','repository':'fixture/project','base_sha':'b'*40,'description':'Synthetic accepted-parent continuation','checks':['fixture-check'],'kind':'development','allowed_paths':['src/'],'dependencies':[self.parent['id']],'qa_contract_ref':'1','qa_contract_digest':digest(self.fx.contract),'priority':'P2','risk_rationale':'Synthetic bounded continuation'}

    def propose(self):return self.board.propose(self.manager,'derived',self.spec)

    def test_exact_accepted_parent_allows_derived_base_and_preserves_remote_fence(self):
        proposal=self.propose();snapshot=proposal['snapshot']
        self.assertEqual(snapshot['source']['sha'],'a'*40)
        self.assertEqual(snapshot['parent_authority']['head_sha'],'b'*40)
        batch=self.board.create_batch(self.manager,'child',[proposal['id']])
        result=self.board.approve_batch(self.owner,batch['id'],batch['digest'])
        child=result['tasks'][0]
        self.assertEqual(child['manifest']['base_sha'],'b'*40)
        assigned=self.host.core.approve_next(self.manager,self.parent['id'],child['id'],self.accepted['candidate_digest'],'derived-next')
        self.assertEqual(assigned['task_id'],child['id'])

    def test_wrong_head_unaccepted_parent_and_nonunique_dependencies_rejected(self):
        self.spec['base_sha']='d'*40
        with self.assertRaises(Conflict):self.propose()
        self.spec['base_sha']='b'*40;self.spec['dependencies']=[]
        with self.assertRaises(Conflict):self.propose()
        self.spec['dependencies']=[self.parent['id']]
        with self.store._transaction() as c:c.execute("UPDATE hierarchy_tasks SET state='review_passed' WHERE task_id=?",(self.parent['id'],))
        with self.assertRaises(Conflict):self.propose()

    def test_revoked_parent_member_and_changed_parent_evidence_block_approval(self):
        proposal=self.propose();batch=self.board.create_batch(self.manager,'child',[proposal['id']])
        self.store.revoke_token(self.owner,token_hash(self.host.reviewer))
        with self.assertRaises(Conflict):self.board.approve_batch(self.owner,batch['id'],batch['digest'])

    def test_parent_evidence_changed_after_child_approval_blocks_execution(self):
        proposal=self.propose();batch=self.board.create_batch(self.manager,'child',[proposal['id']])
        task=self.board.approve_batch(self.owner,batch['id'],batch['digest'])['tasks'][0]
        with self.store._transaction() as c:
            import json
            row=c.execute('SELECT evidence FROM hierarchy_tasks WHERE task_id=?',(self.parent['id'],)).fetchone()
            evidence=json.loads(row['evidence']);evidence['reference']='fixture/changed'
            c.execute('UPDATE hierarchy_tasks SET evidence=? WHERE task_id=?',(json.dumps(evidence),self.parent['id']))
            row=self.store._get(c,self.store._authenticate(c,self.owner),task['id'])
            with self.assertRaises(Conflict):self.store._validate_execution(c,row)
