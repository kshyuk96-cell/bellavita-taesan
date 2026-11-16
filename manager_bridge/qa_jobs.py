"""Durable QA inbox; host-provisioned contracts, no code execution or automatic retries."""
import json
import re
import secrets
from .store import Conflict, NotFound, canonical_json, digest, _reject_credentials
from .qa_admission import admit_qa_report


class QAJobs:
    def __init__(self, store):
        self.store = store
        with store._transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS qa_reconciliation (
              job TEXT PRIMARY KEY, actor TEXT, evidence TEXT, reason TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS qa_approvals (
              id TEXT PRIMARY KEY, project TEXT, manifest TEXT);
            CREATE TABLE IF NOT EXISTS qa_task_requirements (
              task TEXT PRIMARY KEY, approval TEXT, manifest_digest TEXT);
            CREATE TABLE IF NOT EXISTS qa_jobs (
              id TEXT PRIMARY KEY, approval TEXT, request_key TEXT, task TEXT,
              state TEXT, worker TEXT, deadline REAL, report TEXT, decision TEXT,
              UNIQUE(approval,request_key), UNIQUE(approval,task));
            ''')

    def approve(self, token, *, project, source_revision, contract, coverage,
                adapter_release, max_seconds=300, execution_profile_digest=None):
        """Owner-only provisioning; deliberately absent from worker HTTP routes.

        adapter_release identifies a reviewed, isolated execution image/path.
        This is metadata, never a command supplied by a worker.
        """
        if (not re.fullmatch('[a-f0-9]{40}', source_revision) or
                type(max_seconds) is not int or not 1 <= max_seconds <= 3600 or
                type(adapter_release) is not str or not adapter_release.strip() or
                type(coverage) is not list or not coverage or
                type(contract) is not dict or not contract.get('scenarios') or
                len(contract['scenarios']) > 10000 or
                not set(coverage) <= {r['coverage_key'] for r in contract['scenarios'].values()}):
            raise ValueError('bounded reviewed contract required')
        check = admit_qa_report({}, expected_source_revision=source_revision,
            required_coverage=coverage, expected_contract=contract)
        if any(reason in check['reasons'] for reason in
               ('malformed_trusted_scenario_contract', 'trusted_scenario_contract_required')):
            raise ValueError('invalid trusted contract')
        manifest = dict(project=project, source_revision=source_revision, contract=contract,
                        coverage=coverage, adapter_release=adapter_release,
                        max_seconds=max_seconds, environment='isolated_nonproduction')
        if execution_profile_digest is not None:
            if type(execution_profile_digest) is not str or not re.fullmatch('[a-f0-9]{64}', execution_profile_digest):
                raise ValueError('execution profile digest required')
            manifest['execution_profile_digest'] = execution_profile_digest
        _reject_credentials(manifest, token)
        key = digest(manifest)
        with self.store._transaction() as c:
            actor = self.store._authenticate(c, token)
            self.store._role(actor, ('owner',))
            self.store._project_access(c, actor, project)
            c.execute('INSERT OR IGNORE INTO qa_approvals VALUES (?,?,?)',
                      (key, project, canonical_json(manifest)))
        return key

    def require_for_task(self, token, task_id, approval):
        """Owner opts a pending development task into mandatory QA before approval."""
        with self.store._transaction() as c:
            actor, manifest = self._approval(c, token, approval, ('owner',))
            task = self.store._get(c, actor, task_id)
            if task['project'] != manifest['project'] or task['state'] != 'pending_approval':
                raise Conflict('bind QA before approving exact project task')
            old = c.execute('SELECT * FROM qa_task_requirements WHERE task=?', (task_id,)).fetchone()
            if old and (old['approval'] != approval or old['manifest_digest'] != task['manifest_digest']):
                raise Conflict('QA requirement is immutable')
            c.execute('INSERT OR IGNORE INTO qa_task_requirements VALUES (?,?,?)',
                      (task_id, approval, task['manifest_digest']))
            return {'task':task_id, 'approval':approval}

    def _approval(self, c, token, key, roles):
        actor = self.store._authenticate(c, token)
        self.store._role(actor, roles)
        row = c.execute('SELECT * FROM qa_approvals WHERE id=?', (key,)).fetchone()
        if row is None:
            raise NotFound('QA approval missing')
        self.store._project_access(c, actor, row['project'])
        return actor, json.loads(row['manifest'])

    def receive(self, token, approval, request_key, task):
        if any(type(v) is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,100}', v)
               for v in (request_key, task)):
            raise ValueError('bounded request and task identifiers required')
        with self.store._transaction() as c:
            self._approval(c, token, approval, ('owner', 'manager'))
            old = c.execute('SELECT * FROM qa_jobs WHERE approval=? AND request_key=?',
                            (approval, request_key)).fetchone()
            if old:
                if old['task'] != task:
                    raise Conflict('idempotency conflict')
                return self._public(old)
            same_task = c.execute('SELECT * FROM qa_jobs WHERE approval=? AND task=?', (approval, task)).fetchone()
            if same_task:
                return self._public(same_task)
            if c.execute('SELECT count(*) FROM qa_jobs WHERE approval=?', (approval,)).fetchone()[0] >= 10000:
                raise Conflict('approval job limit reached')
            key = secrets.token_hex(16)
            c.execute('INSERT INTO qa_jobs VALUES (?,?,?,?,?,NULL,NULL,NULL,NULL)',
                      (key, approval, request_key, task, 'queued'))
            return self._public(c.execute('SELECT * FROM qa_jobs WHERE id=?', (key,)).fetchone())

    def _job(self, c, token, key, roles):
        row = c.execute('SELECT * FROM qa_jobs WHERE id=?', (key,)).fetchone()
        if row is None:
            raise NotFound('QA job missing')
        actor, manifest = self._approval(c, token, row['approval'], roles)
        if row['state'] == 'running' and row['deadline'] <= self.store._now():
            c.execute("UPDATE qa_jobs SET state='reconciliation_required' WHERE id=?", (key,))
            row = c.execute('SELECT * FROM qa_jobs WHERE id=?', (key,)).fetchone()
        return row, actor, manifest

    @staticmethod
    def _public(row):
        return {k: (json.loads(row[k]) if row[k] is not None and k in ('report', 'decision') else row[k])
                for k in ('id', 'approval', 'task', 'state', 'deadline', 'report', 'decision')}

    def get(self, token, key):
        with self.store._transaction() as c:
            row, _, _ = self._job(c, token, key, ('owner', 'manager', 'worker'))
            return self._public(row)

    def inspect(self, token, key):
        with self.store._transaction() as c:
            row, _, manifest = self._job(c, token, key, ('owner', 'manager', 'worker'))
            reconciliation = c.execute('SELECT actor,evidence,reason,created FROM qa_reconciliation WHERE job=?',(key,)).fetchone()
            return {'job':self._public(row), 'manifest':manifest,
                    'reconciliation':dict(reconciliation) if reconciliation else None}

    def uncertain(self, token, key, reason):
        if reason not in ('execution_interrupted','result_delivery_uncertain','worker_restarted'):
            raise ValueError('fixed uncertainty reason required')
        self.inspect(token, key)  # Persist expiry even if a subsequent action conflicts.
        with self.store._transaction() as c:
            row, actor, _ = self._job(c, token, key, ('worker',))
            if row['worker'] != actor['token_hash'] or row['state'] not in ('running','reconciliation_required'):
                raise Conflict('no owned uncertain execution')
            c.execute("UPDATE qa_jobs SET state='reconciliation_required',decision=? WHERE id=?",
                (canonical_json({'admitted':False,'reasons':[reason],'operational_authorization':False}),key))
            return self._public(c.execute('SELECT * FROM qa_jobs WHERE id=?',(key,)).fetchone())

    def reconcile(self, token, key, *, evidence_reference, process_stopped, effects_absent):
        if (type(evidence_reference) is not str or not 1 <= len(evidence_reference) <= 500 or
                process_stopped is not True or effects_absent is not True):
            raise ValueError('explicit independent stop and effect reconciliation required')
        _reject_credentials(evidence_reference, token)
        self.inspect(token, key)
        with self.store._transaction() as c:
            row, actor, _ = self._job(c, token, key, ('owner',))
            if row['state'] != 'reconciliation_required':
                raise Conflict('uncertain job required')
            c.execute('INSERT INTO qa_reconciliation VALUES (?,?,?,?,?)',
                (key,actor['subject'],evidence_reference,'stopped_effects_absent',self.store._now()))
            c.execute("UPDATE qa_jobs SET state='reconciled' WHERE id=?",(key,))
            return self._public(c.execute('SELECT * FROM qa_jobs WHERE id=?',(key,)).fetchone())

    def claim(self, token, key):
        self.inspect(token, key)
        with self.store._transaction() as c:
            row, actor, manifest = self._job(c, token, key, ('worker',))
            if row['state'] != 'queued':
                raise Conflict('job cannot be replayed')
            # One active run per project, across approvals and request keys.
            running = c.execute("SELECT j.id FROM qa_jobs j JOIN qa_approvals a ON a.id=j.approval WHERE a.project=? AND j.state IN ('running','reconciliation_required')", (manifest['project'],)).fetchone()
            if running:
                raise Conflict('project execution requires completion or reconciliation')
            c.execute("UPDATE qa_jobs SET state='running',worker=?,deadline=? WHERE id=?",
                      (actor['token_hash'], self.store._now()+manifest['max_seconds'], key))
            return dict(job=self._public(c.execute('SELECT * FROM qa_jobs WHERE id=?', (key,)).fetchone()),
                        manifest=manifest)

    def finish(self, token, key, report):
        self.inspect(token, key)
        _reject_credentials(report, token)
        if len(canonical_json(report).encode()) > 32*1024*1024:
            raise ValueError('report too large')
        with self.store._transaction() as c:
            row, actor, m = self._job(c, token, key, ('worker',))
            if row['state'] != 'running' or row['worker'] != actor['token_hash']:
                raise Conflict('no live worker claim')
            decision = admit_qa_report(report, expected_source_revision=m['source_revision'],
                                      required_coverage=m['coverage'], expected_contract=m['contract'])
            binding = dict(project=m['project'], task=row['task'], approval=row['approval'],
                           adapter_release=m['adapter_release'], source_revision=m['source_revision'])
            if report.get('job_binding') != binding:
                decision['admitted'] = False
                decision['reasons'].append('job_binding_mismatch')
            if not execution_receipt_matches(report, m):
                decision['admitted'] = False
                decision['reasons'].append('execution_receipt_mismatch')
            c.execute('UPDATE qa_jobs SET state=?,report=?,decision=? WHERE id=?',
                      ('accepted' if decision['admitted'] else 'blocked', canonical_json(report),
                       canonical_json(decision), key))
            return self._public(c.execute('SELECT * FROM qa_jobs WHERE id=?', (key,)).fetchone())

    def cancel(self, token, key):
        self.inspect(token, key)
        with self.store._transaction() as c:
            row, _, _ = self._job(c, token, key, ('owner', 'manager'))
            if row['state'] not in ('queued', 'running'):
                raise Conflict('terminal or uncertain job')
            state = 'cancelled' if row['state'] == 'queued' else 'reconciliation_required'
            c.execute('UPDATE qa_jobs SET state=? WHERE id=?', (state, key))
            return self._public(c.execute('SELECT * FROM qa_jobs WHERE id=?', (key,)).fetchone())


def require_task_qa(c, task, candidate_revision):
    """Called inside existing manager/three-tier acceptance transactions."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='qa_task_requirements'").fetchone():
        return  # Existing projects have no implicit QA approval or new policy.
    requirement = c.execute('SELECT * FROM qa_task_requirements WHERE task=?', (task['id'],)).fetchone()
    if requirement is None:
        return
    approval = c.execute('SELECT manifest FROM qa_approvals WHERE id=?', (requirement['approval'],)).fetchone()
    if approval is None:
        raise Conflict('required QA approval missing')
    manifest = json.loads(approval['manifest'])
    if (task['manifest_digest'] != requirement['manifest_digest'] or
            candidate_revision != manifest['source_revision']):
        raise Conflict('QA candidate or task changed')
    runs = c.execute("SELECT report,decision FROM qa_jobs WHERE approval=? AND task=? AND state='accepted'",
                     (requirement['approval'], task['id'])).fetchall()
    for run in runs:
        report = json.loads(run['report'])
        decision = admit_qa_report(report, expected_source_revision=candidate_revision,
            required_coverage=manifest['coverage'], expected_contract=manifest['contract'])
        expected_binding = dict(project=manifest['project'], task=task['id'],
            approval=requirement['approval'], adapter_release=manifest['adapter_release'],
            source_revision=candidate_revision)
        if decision['admitted'] and report.get('job_binding') == expected_binding and execution_receipt_matches(report, manifest):
            return
    raise Conflict('mandatory QA has no current passing evidence')


def execution_receipt_matches(report, manifest):
    if not manifest.get('execution_profile_digest'):
        return True  # Compatibility: new isolated workers require a profile pin.
    receipt = report.get('execution_receipt', {})
    observation = {k:v for k,v in report.items() if k not in
                   ('execution_receipt','job_binding','report_digest')}
    observation['report_digest'] = digest(observation)
    return (type(receipt) is dict and receipt.get('schema') == 'qa-execution-receipt/v1' and
            receipt.get('profile_digest') == manifest['execution_profile_digest'] and
            receipt.get('source_revision') == manifest['source_revision'] and
            receipt.get('isolation_verified') is True and receipt.get('process_exited') is True and
            receipt.get('network') == 'none' and
            receipt.get('report_observation_digest') == digest(observation))
