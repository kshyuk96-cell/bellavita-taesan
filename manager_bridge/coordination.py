"""Trusted manager policy helpers; no model calls, shell-from-task, or deployments."""
from pathlib import PurePosixPath

from .store import Conflict, NotFound, PermissionDenied, canonical_json, digest, _text


_FAILURES = {
    'authentication': ('reconnect_authentication', False),
    'usage_limit': ('wait_for_quota_or_owner_budget_decision', False),
    'sandbox_unavailable': ('check_registered_alternative', True),
    'environment_unverified': ('repeat_fixed_environment_probe', True),
    'code_check_failed': ('developer_review_existing_artifacts', False),
    'transport_uncertain': ('reconcile_receipt_no_replay', False),
    'unknown': ('manager_inspection', False),
}


def failure_plan(code, *, started):
    """Only known structured codes are retained; never raw provider messages."""
    if type(started) is not bool:
        raise ValueError('실행 시작 여부를 실제 불리언으로 알려주세요.')
    category = code if isinstance(code, str) and code in _FAILURES else 'unknown'
    action, local = _FAILURES[category]
    if started:
        action, local = 'reconcile_receipt_no_replay', False
    return {'category': category, 'next_action': action, 'manager_can_check': local,
            'automatically_replayed': False, 'requires_stop_confirmation': started}


def validate_paths(paths):
    if not isinstance(paths, list) or not 1 <= len(paths) <= 100:
        raise ValueError('승인할 파일 또는 폴더를 1~100개 지정해 주세요.')
    for name in paths:
        if (not isinstance(name, str) or len(name) > 300 or not name or '\\' in name
                or any(ord(char) < 32 for char in name) or name.startswith('/')
                or any(part in ('', '.', '..', '.git') for part in (name[:-1] if name.endswith('/') else name).split('/'))
                or any(char in name for char in '*?[]')):
            raise ValueError('저장소 안의 정확한 상대 경로 또는 끝이 /인 폴더가 필요해요.')
    return sorted(set(paths))


def scope_violations(paths, allowed_paths):
    allowed = validate_paths(allowed_paths)
    bad = []
    for name in paths:
        # Git returns repository-relative paths; reject malformed observations.
        if (not isinstance(name, str) or PurePosixPath(name).is_absolute()
                or '..' in PurePosixPath(name).parts or '\\' in name):
            raise ValueError('변경 파일 경로를 확인하지 못했어요.')
        if not any(name == permit or (permit.endswith('/') and name.startswith(permit)) for permit in allowed):
            bad.append(name)
    return sorted(set(bad))


class Coordination:
    """Transactions share the bridge DB, token scopes, clock, and task bindings."""
    def __init__(self, store):
        self.store = store
        with store._transaction() as c:
            self.create_tables(c)

    @staticmethod
    def create_tables(c):
        c.executescript('''
            CREATE TABLE IF NOT EXISTS bridge_task_policy (
                task TEXT PRIMARY KEY,policy TEXT NOT NULL,policy_digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_worker_health (
                worker_hash TEXT PRIMARY KEY,ready INTEGER NOT NULL,reason TEXT NOT NULL,
                checked REAL NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_task_route (
                task TEXT PRIMARY KEY,worker_hash TEXT NOT NULL,routed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_failure_records (
                task TEXT PRIMARY KEY,plan TEXT NOT NULL,recorded REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_verified_candidates (
                task TEXT PRIMARY KEY,report_digest TEXT NOT NULL,head_sha TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,dirty INTEGER NOT NULL,verified REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_stop_confirmations (
                task TEXT PRIMARY KEY,evidence TEXT NOT NULL,actor TEXT NOT NULL,recorded REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_evaluations (
                task TEXT NOT NULL,case_id TEXT NOT NULL,dimension TEXT NOT NULL,source TEXT NOT NULL,
                passed INTEGER NOT NULL,critical INTEGER NOT NULL,evidence TEXT NOT NULL,recorded REAL NOT NULL,
                PRIMARY KEY(task,case_id,source));
        ''')
        from .sessions import Sessions
        from .workflow import Workflow
        Sessions.create_tables(c)
        Workflow.create_tables(c)
        from .operations import Operations
        Operations.create_tables(c)

    def set_policy(self, token, task_id, version, manifest_digest, *, allowed_paths, dependencies):
        allowed = validate_paths(allowed_paths)
        if not isinstance(dependencies, list) or len(dependencies) > 100 or len(set(dependencies)) != len(dependencies):
            raise ValueError('선행 작업을 중복 없이 100개 이내로 지정해 주세요.')
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner',))
            row = self.store._get(c, actor, task_id)
            if (row['state'] != 'pending_approval' or type(version) is not int or row['version'] != version
                    or row['manifest_digest'] != manifest_digest):
                raise Conflict('승인 전 정확한 요청에만 작업 정책을 연결할 수 있어요.')
            bindings = []
            for dependency in dependencies:
                parent = self.store._get(c, actor, dependency)
                if parent['project'] != row['project'] or dependency == task_id:
                    raise Conflict('동일 프로젝트의 다른 선행 작업이 필요해요.')
                # Edges may target only already-approved immutable tasks. Policies
                # cannot change after approval, so a dependency cycle cannot form.
                if not parent['approval'] or parent['state'] in ('cancelled', 'needs_verification'):
                    raise Conflict('승인된 유효 선행 작업을 지정해 주세요.')
                bindings.append({'id': dependency, 'version': parent['version'],
                                 'manifest_digest': parent['manifest_digest']})
            policy = {'version': version, 'manifest_digest': manifest_digest,
                      'allowed_paths': allowed, 'dependencies': bindings, 'routing_required': True}
            c.execute('INSERT OR REPLACE INTO bridge_task_policy VALUES (?,?,?)',
                      (task_id, canonical_json(policy), digest(policy)))
            self.store._event(c, task_id, actor['subject'], 'policy_bound', row['state'], policy)
            return policy

    def get_policy(self, token, task_id):
        import json
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._get(c, actor, task_id)
            row = c.execute('SELECT policy FROM bridge_task_policy WHERE task=?', (task_id,)).fetchone()
            return json.loads(row['policy']) if row else None

    def readiness(self, token, worker_hash, *, ready, reason, ttl=60):
        import json
        import re
        if (type(ready) is not bool or reason not in ('ready', 'sandbox_unavailable', 'environment_unverified',
                'authentication', 'usage_limit') or ready != (reason == 'ready')
                or type(ttl) is not int or not 1 <= ttl <= 300 or not isinstance(worker_hash, str)
                or not re.fullmatch(r'[a-f0-9]{64}', worker_hash)):
            raise ValueError('실제 환경 점검 결과와 1~300초 유효기간이 필요해요.')
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            worker = c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?', (worker_hash,)).fetchone()
            if not worker or worker['role'] != 'worker' or worker['revoked'] is not None:
                raise NotFound('등록된 작업자 연결을 찾지 못했어요.')
            if not set(json.loads(worker['projects'])) <= set(actor['projects']) and actor['projects'] != ['*']:
                raise PermissionDenied('관리자 범위 밖의 작업자 상태는 변경할 수 없어요.')
            now = self.store._now()
            if worker['expires'] is not None and worker['expires'] <= now:
                raise PermissionDenied('만료된 작업자예요.')
            c.execute('INSERT OR REPLACE INTO bridge_worker_health VALUES (?,?,?,?,?)',
                      (worker_hash, int(ready), reason, now, now + ttl))
            return {'ready': ready, 'reason': reason, 'expires_at': now + ttl}

    def route(self, token, task_id, *, worker_hash=None):
        import json
        if worker_hash is not None:
            import re
            if not isinstance(worker_hash,str) or not re.fullmatch('[a-f0-9]{64}',worker_hash):
                raise ValueError('exact registered worker identity required')
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            row = self.store._get(c, actor, task_id)
            self.store._expire(c, row['project'])
            row = self.store._get(c, actor, task_id)
            if row['state'] != 'queued':
                raise Conflict('아직 실행하지 않은 승인 대기 작업만 배정 대상을 바꿀 수 있어요.')
            from .operations import Operations
            if Operations.capacity_blocked(c,row['project']):
                return {'status':'capacity_wait','task_id':task_id,'execution_started':False}
            policy = c.execute('SELECT * FROM bridge_task_policy WHERE task=?', (task_id,)).fetchone()
            if not policy or json.loads(row['approval']).get('policy_digest') != policy['policy_digest']:
                raise Conflict('승인에 결합된 작업 정책이 필요해요.')
            if not self.dependencies_ready(c, json.loads(policy['policy'])):
                return {'status': 'waiting_dependencies', 'task_id': task_id}
            manifest, now = json.loads(row['manifest']), self.store._now()
            if manifest['repository'] in self.store._reserved_repositories(c):
                return {'status': 'repository_busy_or_stop_unconfirmed', 'task_id': task_id, 'execution_started': False}
            workers = c.execute('''SELECT t.* FROM bridge_tokens t JOIN bridge_worker_health h
                ON t.token_hash=h.worker_hash WHERE t.role='worker' AND t.revoked IS NULL
                AND (t.expires IS NULL OR t.expires>?) AND h.ready=1 AND h.expires>?
                ORDER BY h.checked DESC,t.subject,t.token_hash''', (now, now)).fetchall()
            for worker in workers:
                if worker_hash is not None and worker['token_hash']!=worker_hash:
                    continue
                if (row['project'] not in json.loads(worker['projects'])
                        or manifest['kind'] not in json.loads(worker['capabilities'])):
                    continue
                busy = c.execute("SELECT 1 FROM bridge_tasks t WHERE worker_token_hash=? AND "
                    "(state IN ('running','needs_verification') OR (state='cancelled' AND NOT EXISTS "
                    "(SELECT 1 FROM bridge_stop_confirmations s WHERE s.task=t.id)))", (worker['token_hash'],)).fetchone()
                if busy:
                    continue
                c.execute('INSERT OR REPLACE INTO bridge_task_route VALUES (?,?,?)',
                          (task_id, worker['token_hash'], now))
                self.store._event(c, task_id, actor['subject'], 'ready_worker_routed', 'queued')
                return {'status': 'routed', 'task_id': task_id, 'worker': worker['subject'],
                        'execution_started': False}
            c.execute('DELETE FROM bridge_task_route WHERE task=?', (task_id,))
            return {'status': 'no_ready_worker', 'task_id': task_id, 'execution_started': False}

    def probe_and_route(self, token, task_id, probes):
        """Parent runtime supplies fixed registered probe callables, not task text.

        This checks adapters and queues a route. It never claims, dispatches, or
        retries execution. A ready native adapter must have a real parent tool
        connection; this module cannot manufacture one or control Work windows.
        """
        task = self.store.get_task(token, task_id)
        actor = self.store.authenticate(token)
        if actor['role'] not in ('owner', 'manager'):
            raise PermissionDenied('관리자 배정 권한이 필요해요.')
        if task['state'] != 'queued' or not isinstance(probes, dict) or len(probes) > 20:
            raise Conflict('실행 전 작업과 등록된 환경 점검 연결이 필요해요.')
        # Validate all identities before calling any adapter.
        with self.store._transaction() as c:
            for worker_hash, probe in probes.items():
                import json
                worker = c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?', (worker_hash,)).fetchone()
                if (not worker or worker['role'] != 'worker' or worker['revoked'] is not None
                        or task['project'] not in json.loads(worker['projects']) or not callable(probe)
                        or (actor['projects'] != ['*'] and
                            not set(json.loads(worker['projects'])) <= set(actor['projects']))):
                    raise PermissionDenied('고정된 프로젝트 작업자 점검 연결이 필요해요.')
        for worker_hash, probe in probes.items():
            try:
                result = probe()
                ready = isinstance(result, dict) and result.get('ready') is True and result.get('reason') == 'ready'
                reason = result.get('reason') if isinstance(result, dict) else None
                if not ready and reason not in ('sandbox_unavailable', 'environment_unverified', 'authentication', 'usage_limit'):
                    reason = 'environment_unverified'
                self.readiness(token, worker_hash, ready=ready, reason='ready' if ready else reason)
            except Exception:
                self.readiness(token, worker_hash, ready=False, reason='environment_unverified')
        return self.route(token, task_id)

    @staticmethod
    def dependencies_ready(c, policy):
        import json
        for dependency in policy['dependencies']:
            row = c.execute('SELECT * FROM bridge_tasks WHERE id=?', (dependency['id'],)).fetchone()
            if (not row or row['state'] != 'reviewed' or row['version'] != dependency['version']
                    or row['manifest_digest'] != dependency['manifest_digest'] or not row['review']
                    or json.loads(row['review']).get('report_digest') != digest(json.loads(row['report']))):
                return False
            from .store import _hierarchy_acceptance
            if not _hierarchy_acceptance(c,row):return False
            report = json.loads(row['report'])
            review = json.loads(row['review'])
            for check in report['checks']:
                artifact_attested = (check['name'] == 'artifact_access_verified' and
                    review.get('evidence_reference', '').startswith('independent-artifact:'))
                if check['passed'] is not True and not artifact_attested:
                    return False
        return True

    def confirm_stopped(self, token, task_id, version, manifest_digest, evidence_reference):
        import re
        if (not isinstance(evidence_reference, str) or
                not re.fullmatch(r'[A-Za-z0-9_:/#.-]{1,500}', evidence_reference) or token in evidence_reference):
            raise ValueError('실제 프로세스/플랫폼 종료를 독립 확인한 참조가 필요해요.')
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            row = self.store._get(c, actor, task_id)
            if (row['state'] != 'cancelled' or row['worker_subject'] is None
                    or type(version) is not int or row['version'] != version
                    or row['manifest_digest'] != manifest_digest):
                raise Conflict('실행 이력이 있는 취소 작업의 정확한 버전을 대조해야 해요.')
            old = c.execute('SELECT evidence FROM bridge_stop_confirmations WHERE task=?', (task_id,)).fetchone()
            if old and old['evidence'] != evidence_reference:
                raise Conflict('이미 기록한 종료 확인을 덮어쓰지 않았어요.')
            if not old:
                c.execute('INSERT INTO bridge_stop_confirmations VALUES (?,?,?,?)',
                          (task_id, evidence_reference, actor['subject'], self.store._now()))
                self.store._event(c, task_id, actor['subject'], 'execution_stop_confirmed', 'cancelled')
            return {'task_id': task_id, 'state': 'cancelled', 'stop_confirmed': True, 'execution_replayed': False}

    def record_failure(self, token, task_id, code):
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            row = self.store._get(c, actor, task_id)
            plan = failure_plan(code, started=row['worker_subject'] is not None)
            c.execute('INSERT OR REPLACE INTO bridge_failure_records VALUES (?,?,?)',
                      (task_id, canonical_json(plan), self.store._now()))
            self.store._event(c, task_id, actor['subject'], 'failure_classified', row['state'], plan)
            return plan

    def record_evaluation(self, token, task_id, *, case_id, dimension, source, passed, critical, evidence):
        import re
        if (not isinstance(case_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', case_id)
                or dimension not in ('understanding', 'judgment', 'development')
                or source not in ('real', 'synthetic') or type(passed) is not bool or type(critical) is not bool):
            raise ValueError('평가 항목·영역·실측 구분·불리언 결과를 확인해 주세요.')
        _text(evidence, 500, '평가 근거 참조')
        if not re.fullmatch(r'[A-Za-z0-9_:/#.-]{1,500}', evidence):
            raise ValueError('질문 원문 대신 검증 기록의 참조만 남겨주세요.')
        if token in evidence:
            raise ValueError('평가 기록에 인증 정보를 포함하지 마세요.')
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            row = self.store._get(c, actor, task_id)
            if row['state'] != 'reviewed':
                raise Conflict('독립 검토 기록이 있는 작업의 근거만 평가에 연결해 주세요.')
            values = (task_id, case_id, dimension, source, int(passed), int(critical), evidence)
            old = c.execute('SELECT * FROM bridge_evaluations WHERE task=? AND case_id=? AND source=?',
                            (task_id, case_id, source)).fetchone()
            if old:
                if tuple(old[key] for key in ('task','case_id','dimension','source','passed','critical','evidence')) != values:
                    raise Conflict('기록한 평가를 덮어쓰지 않아요.')
                return {'recorded': True, 'duplicate': True}
            c.execute('INSERT INTO bridge_evaluations VALUES (?,?,?,?,?,?,?,?)', (*values, self.store._now()))
            return {'recorded': True, 'duplicate': False}

    def scorecard(self, token, project):
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner', 'manager'))
            self.store._project_access(c, actor, project)
            rows = c.execute('''SELECT e.* FROM bridge_evaluations e JOIN bridge_tasks t ON e.task=t.id
                WHERE t.project=? ORDER BY e.recorded,e.rowid''', (project,)).fetchall()
            result = {}
            for source in ('real', 'synthetic'):
                dimensions = {}
                latest = {}
                for observation in rows:
                    if observation['source'] == source:
                        latest[(observation['dimension'], observation['case_id'])] = observation
                for dimension in ('understanding', 'judgment', 'development'):
                    observed = [row for row in rows if row['source'] == source and row['dimension'] == dimension]
                    current = [row for row in latest.values() if row['dimension'] == dimension]
                    passed = sum(row['passed'] for row in current)
                    dimensions[dimension] = {'samples': len(observed), 'distinct_cases': len(current),
                        'passed': passed, 'sample_score': round(100*passed/len(current), 1) if current else None}
                critical = sum(not row['passed'] and row['critical'] for row in latest.values())
                qualified = (source == 'real' and critical == 0 and all(
                    value['distinct_cases'] >= 20 and value['sample_score'] >= 90 for value in dimensions.values()))
                result[source] = {'dimensions': dimensions, 'critical_failures': critical,
                                  'target_demonstrated_for_recorded_cases': qualified}
            failures = {}
            for row in rows:
                if not row['passed']:
                    key = (row['case_id'], row['dimension'], row['source'])
                    failures[key] = failures.get(key, 0) + 1
            result['regression_candidates'] = [{'case_id': key[0], 'dimension': key[1], 'source': key[2], 'failures': count}
                                               for key, count in sorted(failures.items()) if count >= 2]
            result['operational_baseline_changed'] = False
            return result


def main(argv=None):
    """Authenticated API client; credentials stay in a private file, never argv."""
    import argparse
    import json
    import re
    from pathlib import Path
    from urllib.parse import quote
    from .control import request, secret
    parser = argparse.ArgumentParser(description='태산이 작업 정책·배정·평가 연결')
    parser.add_argument('action', choices=('policy', 'route', 'failure', 'evaluation', 'readiness', 'scorecard', 'stopped'))
    parser.add_argument('--url', default='http://127.0.0.1:8791')
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--task')
    parser.add_argument('--project')
    parser.add_argument('--input-file', type=Path)
    args = parser.parse_args(argv)
    try:
        token = secret(args.token_file)
        payload = {}
        if args.input_file:
            if args.input_file.is_symlink() or args.input_file.stat().st_size > 20000:
                raise ValueError('설정 파일 범위를 확인해 주세요.')
            payload = json.loads(args.input_file.read_text())
            if not isinstance(payload, dict):
                raise ValueError('설정 객체가 필요해요.')
        if args.action == 'scorecard':
            if not args.project:
                raise ValueError('프로젝트가 필요해요.')
            result = request(args.url, token, 'GET', '/v1/scorecard?project=' + quote(args.project, safe=''))
        else:
            if args.action == 'readiness':
                path = '/v1/manager/readiness'
            else:
                if not isinstance(args.task, str) or not re.fullmatch(r'[a-f0-9]{32}', args.task):
                    raise ValueError('정확한 작업 식별값이 필요해요.')
                path = '/v1/tasks/' + args.task + '/' + args.action
            result = request(args.url, token, 'POST', path, payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('ok') else 1
    except Exception:
        print(json.dumps({'ok': False, 'error': 'coordination_request_unverified'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
