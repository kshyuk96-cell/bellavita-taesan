import copy
import tempfile
import unittest
from pathlib import Path
from manager_bridge.store import Store, Conflict, digest
from manager_bridge.qa_jobs import QAJobs
from manager_bridge.tests import test_qa_admission


class QAJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.now = 100
        self.store = Store(Path(self.tmp)/'qa.sqlite', clock=lambda: self.now)
        self.owner = self.store.add_token('owner', 'owner', ['*'])
        self.store.register_project(self.owner, 'shop', 'fixture/shop')
        self.worker = self.store.add_token('worker', 'worker', ['shop'])
        self.other = self.store.add_token('other', 'worker', ['shop'])
        self.jobs = QAJobs(self.store)
        fixture = test_qa_admission.QAAdmissionTests()
        self.baseline = fixture.fixture()
        self.baseline_contract = fixture.expected_contract
        self.approval = self.jobs.approve(self.owner, project='shop', source_revision='a'*40,
            contract=fixture.expected_contract, coverage=['money'], adapter_release='local-reviewed-v1')
        self.job = self.jobs.receive(self.owner, self.approval, 'request1', 'task1')

    def report(self):
        r = copy.deepcopy(self.baseline)
        r['job_binding'] = dict(project='shop', task='task1', approval=self.approval,
                               adapter_release='local-reviewed-v1', source_revision='a'*40)
        r['report_digest'] = digest({k:v for k,v in r.items() if k != 'report_digest'})
        return r

    def test_persisted_results_and_admission(self):
        self.jobs.claim(self.worker, self.job['id'])
        result = self.jobs.finish(self.worker, self.job['id'], self.report())
        self.assertEqual('accepted', result['state'])
        self.assertFalse(result['decision']['operational_authorization'])
        self.assertEqual(result, QAJobs(self.store).get(self.owner, self.job['id']))

    def test_duplicate_and_replay_and_worker_ownership(self):
        self.assertEqual(self.job, self.jobs.receive(self.owner, self.approval, 'request1', 'task1'))
        self.assertEqual(self.job, self.jobs.receive(self.owner, self.approval, 'different-key', 'task1'))
        with self.assertRaises(Conflict):
            self.jobs.receive(self.owner, self.approval, 'request1', 'changed')
        self.jobs.claim(self.worker, self.job['id'])
        with self.assertRaises(Conflict): self.jobs.claim(self.worker, self.job['id'])
        with self.assertRaises(Conflict): self.jobs.finish(self.other, self.job['id'], self.report())
        second = self.jobs.receive(self.owner, self.approval, 'request2', 'task2')
        with self.assertRaises(Conflict): self.jobs.claim(self.worker, second['id'])

    def test_stale_source_and_cross_task_and_critical_fail_block(self):
        for mutation in ('source', 'binding', 'critical'):
            with self.subTest(mutation=mutation):
                job = self.jobs.receive(self.owner, self.approval, mutation, mutation)
                self.jobs.claim(self.worker, job['id'])
                r = self.report()
                r['job_binding']['task'] = mutation
                if mutation == 'source': r['source_revision'] = 'b'*40
                if mutation == 'binding': r['job_binding']['task'] = 'different'
                if mutation == 'critical': r['scenarios'][0]['status'] = 'unmeasured'
                r['report_digest'] = digest({k:v for k,v in r.items() if k != 'report_digest'})
                self.assertEqual('blocked', self.jobs.finish(self.worker, job['id'], r)['state'])

    def test_timeout_cancel_never_retry(self):
        self.jobs.claim(self.worker, self.job['id'])
        self.now += 301
        self.assertEqual('reconciliation_required', self.jobs.get(self.owner, self.job['id'])['state'])
        with self.assertRaises(Conflict): self.jobs.finish(self.worker, self.job['id'], self.report())
        with self.assertRaises(Conflict): self.jobs.claim(self.worker, self.job['id'])

    def test_approval_is_owner_only(self):
        with self.assertRaises(PermissionError):
            self.jobs.approve(self.worker, project='shop', source_revision='a'*40,
                contract= self.baseline_contract, coverage=['money'], adapter_release='v1')

    def test_cancel_and_cross_project(self):
        self.assertEqual('cancelled', self.jobs.cancel(self.owner, self.job['id'])['state'])
        with self.assertRaises(Conflict): self.jobs.claim(self.worker, self.job['id'])
        job = self.jobs.receive(self.owner, self.approval, 'second', 'task2')
        self.jobs.claim(self.worker, job['id'])
        self.assertEqual('reconciliation_required', self.jobs.cancel(self.owner, job['id'])['state'])
        self.store.register_project(self.owner, 'other', 'fixture/other')
        other = self.store.add_token('outside', 'manager', ['other'])
        from manager_bridge.store import NotFound
        with self.assertRaises(NotFound): self.jobs.get(other, job['id'])

    def test_http_intake_claim_result_recovery(self):
        import threading
        from manager_bridge.server import make_server
        from manager_bridge.control import request
        server = make_server(self.store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:'+str(server.server_address[1])
        try:
            result = request(base, self.owner, 'POST', '/v1/qa/jobs',
                dict(approval=self.approval, request_key='http', task='task-http'))
            path = '/v1/qa/jobs/'+result['job']['id']
            self.assertTrue(request(base, self.worker, 'POST', path+'/claim', {})['ok'])
            report = self.report()
            report['job_binding']['task'] = 'task-http'
            report['report_digest'] = digest({k:v for k,v in report.items() if k != 'report_digest'})
            result = request(base, self.worker, 'POST', path+'/result', {'report':report})
            self.assertEqual('accepted', result['job']['state'])
            self.assertEqual(result['job'], request(base, self.owner, 'GET', path)['job'])
            self.assertEqual(409, request(base, self.worker, 'POST', path+'/claim', {})['status'])
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def test_existing_manager_review_requires_bound_qa_for_exact_candidate(self):
        task = self.store.submit(self.owner, 'development', dict(project='shop',
            repository='fixture/shop', base_sha='b'*40, description='offline fixture',
            checks=['fixture'], kind='development'))
        self.jobs.require_for_task(self.owner, task['id'], self.approval)
        self.store.approve(self.owner, task['id'], task['version'], task['manifest_digest'])
        claimed = self.store.claim(self.worker, 'shop')
        self.store.report(self.worker, task['id'], claimed['lease_token'],
            dict(head_sha='a'*40, summary='offline fixture', checks=[dict(name='fixture',passed=True)]))
        def review():
            return self.store.review(self.owner, task['id'], task['version'], task['manifest_digest'], 'fixture:independent-check')
        with self.assertRaises(Conflict): review()
        job = self.jobs.receive(self.owner, self.approval, 'bound-task', task['id'])
        self.jobs.claim(self.worker, job['id'])
        r = self.report()
        r['job_binding']['task'] = task['id']
        r['report_digest'] = digest({k:v for k,v in r.items() if k != 'report_digest'})
        self.assertEqual('accepted', self.jobs.finish(self.worker, job['id'], r)['state'])
        with self.store._transaction() as c:
            import json
            changed = dict(head_sha='b'*40, summary='changed candidate', checks=[])
            c.execute('UPDATE bridge_tasks SET report=? WHERE id=?', (json.dumps(changed),task['id']))
        with self.assertRaises(Conflict): review()
        with self.store._transaction() as c:
            changed['head_sha'] = 'a'*40
            c.execute('UPDATE bridge_tasks SET report=? WHERE id=?', (json.dumps(changed),task['id']))
        self.assertEqual('reviewed', review()['state'])
