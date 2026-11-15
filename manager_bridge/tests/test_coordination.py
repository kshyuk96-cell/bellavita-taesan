import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from manager_bridge.coordination import Coordination, failure_plan, scope_violations
from manager_bridge.store import Store, Conflict, PermissionDenied, digest, token_hash
from manager_bridge.worker import validate_task, WorkerError


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = 1000
        self.store = Store(self.root/'manager.sqlite3', clock=lambda: self.now)
        self.owner = self.store.add_token('owner', 'owner', ['*'])
        self.store.register_project(self.owner, 'a', 'fixture/a')
        self.store.register_project(self.owner, 'b', 'fixture/b')
        self.manager = self.store.add_token('manager', 'manager', ['a'])
        self.worker = self.store.add_token('cli', 'worker', ['a'])
        self.fallback = self.store.add_token('native', 'worker', ['a'])
        self.other = self.store.add_token('other', 'worker', ['b'])
        self.coordination = Coordination(self.store)
        self.report = {'head_sha': 'a'*40, 'summary': 'fixture result only',
                       'checks': [{'name': 'fixture', 'passed': True}]}

    def submit(self, key, repository='fixture/a'):
        return self.store.submit(self.manager, key, {'project': 'a', 'repository': repository,
            'base_sha': 'a'*40, 'description': 'fixture only', 'checks': ['fixture'], 'kind': 'test'})

    def managed(self, key='one', dependencies=None):
        task = self.submit(key)
        policy = self.coordination.set_policy(self.owner, task['id'], task['version'], task['manifest_digest'],
            allowed_paths=['src/', 'README.md'], dependencies=dependencies or [])
        return self.store.approve(self.owner, task['id'], task['version'], task['manifest_digest'],
                                  policy_digest=digest(policy))

    def ready(self, worker, ready=True):
        return self.coordination.readiness(self.manager, token_hash(worker), ready=ready,
                                           reason='ready' if ready else 'sandbox_unavailable', ttl=30)

    def reviewed(self, key='one'):
        task = self.managed(key)
        self.ready(self.worker)
        self.coordination.route(self.manager, task['id'])
        claim = self.store.claim(self.worker, 'a')
        self.store.report(self.worker, task['id'], claim['lease_token'], self.report)
        return self.store.review(self.manager, task['id'], task['version'], task['manifest_digest'], 'fixture-independent:1')

    def test_scope_policy_requires_owner_and_approval_of_exact_policy_digest(self):
        task = self.submit('one')
        with self.assertRaises(PermissionDenied):
            self.coordination.set_policy(self.manager, task['id'], 1, task['manifest_digest'],
                                         allowed_paths=['src/'], dependencies=[])
        policy = self.coordination.set_policy(self.owner, task['id'], 1, task['manifest_digest'],
                                              allowed_paths=['src/'], dependencies=[])
        with self.assertRaises(Conflict):
            self.store.approve(self.owner, task['id'], 1, task['manifest_digest'])
        self.store.approve(self.owner, task['id'], 1, task['manifest_digest'], policy_digest=digest(policy))
        with self.assertRaises(Conflict):
            self.coordination.set_policy(self.owner, task['id'], 1, task['manifest_digest'],
                                         allowed_paths=['README.md'], dependencies=[])

    def test_fresh_ready_worker_only_can_claim_and_receives_approved_policy(self):
        task = self.managed()
        self.ready(self.worker, False)
        self.ready(self.fallback)
        route = self.coordination.route(self.manager, task['id'])
        self.assertEqual(route['worker'], 'native')
        self.assertFalse(route['execution_started'])
        self.assertIsNone(self.store.claim(self.worker, 'a'))
        claim = self.store.claim(self.fallback, 'a')
        self.assertEqual(claim['execution_policy']['allowed_paths'], ['README.md', 'src/'])
        self.assertEqual(validate_task(claim, 'a')['base_sha'], 'a'*40)
        broken = copy.deepcopy(claim)
        broken['execution_policy']['allowed_paths'] = ['other/']
        with self.assertRaises(WorkerError):
            validate_task(broken, 'a')

    def test_queued_failover_is_safe_but_running_task_cannot_be_rerouted(self):
        task = self.managed()
        self.ready(self.worker)
        self.coordination.route(self.manager, task['id'])
        self.ready(self.worker, False)
        self.ready(self.fallback)
        self.coordination.route(self.manager, task['id'])
        self.assertIsNone(self.store.claim(self.worker, 'a'))
        self.assertIsNotNone(self.store.claim(self.fallback, 'a'))
        with self.assertRaises(Conflict):
            self.coordination.route(self.manager, task['id'])

    def test_cancelled_execution_reserves_worker_and_repository_until_stop_confirmed(self):
        first = self.managed('first')
        self.ready(self.worker)
        self.coordination.route(self.manager, first['id'])
        self.store.claim(self.worker, 'a')
        self.store.cancel(self.manager, first['id'])
        second = self.managed('second')
        self.assertEqual(self.coordination.route(self.manager, second['id'])['status'], 'repository_busy_or_stop_unconfirmed')
        self.assertIsNone(self.store.claim(self.worker, 'a'))
        overview = self.store.overview(self.manager, 'a')
        self.assertTrue(any(item['task_id'] == first['id'] and item['action'] == 'confirm_worker_stopped_no_replay'
                            for item in overview['actions']))
        with self.assertRaises(PermissionDenied):
            self.coordination.confirm_stopped(self.worker, first['id'], 1, first['manifest_digest'], 'fixture:stopped')
        with self.assertRaises(Conflict):
            self.coordination.confirm_stopped(self.manager, first['id'], 2, first['manifest_digest'], 'fixture:stopped')
        self.coordination.confirm_stopped(self.manager, first['id'], 1, first['manifest_digest'], 'fixture:independent-process-exit')
        self.assertEqual(self.coordination.route(self.manager, second['id'])['status'], 'routed')
        self.assertEqual(self.store.claim(self.worker, 'a')['id'], second['id'])

    def test_stale_or_revoked_health_is_not_executable(self):
        task = self.managed()
        self.ready(self.worker)
        self.coordination.route(self.manager, task['id'])
        self.now += 31
        self.assertIsNone(self.store.claim(self.worker, 'a'))
        self.assertEqual(self.coordination.route(self.manager, task['id'])['status'], 'no_ready_worker')
        self.ready(self.worker)
        self.store.revoke_token(self.owner, token_hash(self.worker))
        self.assertEqual(self.coordination.route(self.manager, task['id'])['status'], 'no_ready_worker')

    def test_parallel_claims_after_routing_assign_exactly_one_worker(self):
        task = self.managed()
        self.ready(self.worker)
        self.coordination.route(self.manager, task['id'])
        barrier = threading.Barrier(2)
        def claim(_):
            barrier.wait()
            return self.store.claim(self.worker, 'a')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_parent_probes_route_around_blocked_adapter_without_execution(self):
        task = self.managed()
        invoked = []
        def cli_probe():
            invoked.append('cli')
            return {'ready': False, 'reason': 'sandbox_unavailable'}
        def native_probe():
            invoked.append('native')
            return {'ready': True, 'reason': 'ready'}
        result = self.coordination.probe_and_route(self.manager, task['id'], {
            token_hash(self.worker): cli_probe, token_hash(self.fallback): native_probe})
        self.assertEqual(invoked, ['cli', 'native'])
        self.assertEqual(result['worker'], 'native')
        self.assertFalse(result['execution_started'])
        self.assertEqual(self.store.get_task(self.manager, task['id'])['state'], 'queued')

    def test_manager_cannot_record_health_for_another_business(self):
        with self.assertRaises(PermissionDenied):
            self.coordination.readiness(self.manager, token_hash(self.other), ready=True, reason='ready')
        with self.assertRaises(PermissionDenied):
            self.coordination.readiness(self.worker, token_hash(self.worker), ready=True, reason='ready')

    def test_dependency_waits_for_independent_review_and_survives_restart(self):
        parent = self.managed('parent')
        child = self.managed('child', [parent['id']])
        self.ready(self.worker)
        self.assertEqual(self.coordination.route(self.manager, child['id'])['status'], 'waiting_dependencies')
        self.coordination.route(self.manager, parent['id'])
        claim = self.store.claim(self.worker, 'a')
        self.store.report(self.worker, parent['id'], claim['lease_token'], self.report)
        restarted = Coordination(Store(self.root/'manager.sqlite3', clock=lambda: self.now))
        self.assertEqual(restarted.route(self.manager, child['id'])['status'], 'waiting_dependencies')
        self.store.review(self.manager, parent['id'], 1, parent['manifest_digest'], 'fixture:parent-checked')
        self.assertEqual(restarted.route(self.manager, child['id'])['status'], 'routed')
        self.assertEqual(self.store.claim(self.worker, 'a')['id'], child['id'])

    def test_failed_parent_report_cannot_unlock_dependency_via_manual_review(self):
        parent = self.managed('parent')
        child = self.managed('child', [parent['id']])
        self.ready(self.worker)
        self.coordination.route(self.manager, parent['id'])
        claim = self.store.claim(self.worker, 'a')
        report = self.report | {'checks': [{'name': 'actual-command', 'passed': False}]}
        self.store.report(self.worker, parent['id'], claim['lease_token'], report)
        self.store.review(self.manager, parent['id'], 1, parent['manifest_digest'], 'fixture:reviewed-failure')
        self.assertEqual(self.coordination.route(self.manager, child['id'])['status'], 'waiting_dependencies')
        self.assertIsNone(self.store.claim(self.worker, 'a'))

    def test_unapproved_or_self_dependency_cannot_create_a_cycle(self):
        parent = self.submit('parent')
        child = self.submit('child')
        for deps in ([parent['id']], [child['id']]):
            with self.assertRaises(Conflict):
                self.coordination.set_policy(self.owner, child['id'], 1, child['manifest_digest'],
                                             allowed_paths=['src/'], dependencies=deps)

    def test_exact_file_and_directory_boundary_detection(self):
        self.assertEqual(scope_violations(['README.md', 'src/a.py', 'src_extra/a.py', '.github/workflow.yml'],
                                         ['README.md', 'src/']), ['.github/workflow.yml', 'src_extra/a.py'])
        for permit in ('../', '/tmp/', '.git/', '*', 'src/../', 'src//'):
            with self.assertRaises(ValueError):
                scope_violations(['src/a.py'], [permit])

    def test_failure_plans_never_replay_started_or_uncertain_work(self):
        for category in ('authentication', 'usage_limit', 'sandbox_unavailable', 'code_check_failed', 'transport_uncertain'):
            plan = failure_plan(category, started=True)
            self.assertEqual(plan['next_action'], 'reconcile_receipt_no_replay')
            self.assertFalse(plan['automatically_replayed'])
        task = self.managed()
        plan = self.coordination.record_failure(self.manager, task['id'], 'sandbox_unavailable')
        self.assertEqual(plan['next_action'], 'check_registered_alternative')
        self.assertEqual(self.store.get_task(self.manager, task['id'])['state'], 'queued')
        private = 'secret raw provider message'
        self.coordination.record_failure(self.manager, task['id'], private)
        self.assertNotIn(private.encode(), (self.root/'manager.sqlite3').read_bytes())

    def test_scorecard_keeps_real_and_synthetic_separate_and_does_not_invent_scores(self):
        empty = self.coordination.scorecard(self.manager, 'a')
        self.assertIsNone(empty['real']['dimensions']['judgment']['sample_score'])
        self.assertFalse(empty['real']['target_demonstrated_for_recorded_cases'])
        task = self.reviewed()
        for source in ('real', 'synthetic'):
            self.coordination.record_evaluation(self.manager, task['id'], case_id='fixture-case',
                dimension='understanding', source=source, passed=source == 'synthetic', critical=True, evidence='fixture-log:1')
        score = self.coordination.scorecard(self.manager, 'a')
        self.assertEqual(score['real']['dimensions']['understanding']['sample_score'], 0)
        self.assertEqual(score['synthetic']['dimensions']['understanding']['sample_score'], 100)
        self.assertFalse(score['real']['target_demonstrated_for_recorded_cases'])
        self.assertFalse(score['operational_baseline_changed'])

    def test_repeated_failure_becomes_regression_candidate_with_evidence_refs_only(self):
        for key in ('one', 'two'):
            task = self.reviewed(key)
            arguments = dict(case_id='same-case', dimension='judgment', source='synthetic',
                             passed=False, critical=False, evidence='fixture-log:'+key)
            self.coordination.record_evaluation(self.manager, task['id'], **arguments)
            self.assertTrue(self.coordination.record_evaluation(self.manager, task['id'], **arguments)['duplicate'])
            with self.assertRaises(Conflict):
                self.coordination.record_evaluation(self.manager, task['id'], **(arguments | {'passed': True}))
        score = self.coordination.scorecard(self.manager, 'a')
        self.assertEqual(score['regression_candidates'][0]['failures'], 2)
        self.assertNotIn('fixture-log', json.dumps(score))

    def test_repeating_successful_case_does_not_inflate_distinct_case_score(self):
        first = self.reviewed('one')
        second = self.reviewed('two')
        for task in (first, second):
            self.coordination.record_evaluation(self.manager, task['id'], case_id='easy',
                dimension='judgment', source='synthetic', passed=True, critical=False, evidence='fixture:easy')
        self.coordination.record_evaluation(self.manager, second['id'], case_id='hard',
            dimension='judgment', source='synthetic', passed=False, critical=False, evidence='fixture:hard')
        result = self.coordination.scorecard(self.manager, 'a')['synthetic']['dimensions']['judgment']
        self.assertEqual(result['samples'], 3)
        self.assertEqual(result['distinct_cases'], 2)
        self.assertEqual(result['sample_score'], 50)

    def test_target_requires_all_dimensions_and_zero_critical_failures(self):
        task = self.reviewed('qualified')
        for dimension in ('understanding', 'judgment', 'development'):
            for index in range(20):
                self.coordination.record_evaluation(self.manager, task['id'], case_id=dimension+'-'+str(index),
                    dimension=dimension, source='real', passed=True, critical=False, evidence='fixture:qualification')
        score = self.coordination.scorecard(self.manager, 'a')
        self.assertTrue(score['real']['target_demonstrated_for_recorded_cases'])
        self.assertFalse(score['synthetic']['target_demonstrated_for_recorded_cases'])
        self.coordination.record_evaluation(self.manager, task['id'], case_id='critical-case',
            dimension='judgment', source='real', passed=False, critical=True, evidence='fixture:critical')
        score = self.coordination.scorecard(self.manager, 'a')
        self.assertGreater(score['real']['dimensions']['judgment']['sample_score'], 90)
        self.assertFalse(score['real']['target_demonstrated_for_recorded_cases'])

    def test_unreviewed_worker_cannot_award_real_scores(self):
        task = self.managed()
        args = dict(case_id='case', dimension='development', source='real', passed=True,
                    critical=False, evidence='fixture-log:1')
        with self.assertRaises(Conflict):
            self.coordination.record_evaluation(self.manager, task['id'], **args)
        with self.assertRaises(PermissionDenied):
            self.coordination.record_evaluation(self.worker, task['id'], **args)
        with self.assertRaises(ValueError):
            self.coordination.record_evaluation(self.manager, task['id'], **(args | {'evidence': '원문 질문 저장 금지'}))


if __name__ == '__main__':
    unittest.main()
