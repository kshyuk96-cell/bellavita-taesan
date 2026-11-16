"""Owner-bound mandatory QA admission. Providers are server registrations only.

Synthetic observations remain synthetic even when admitted for technical review;
this gate never grants deployment or operational authorization.
"""
from dataclasses import dataclass
import json
import math
from .store import Conflict, canonical_json, digest
from .maintenance_registry import name
from .qa_admission import admit_qa_report


@dataclass(frozen=True)
class QAObservation:
    project: str
    task_id: str
    version: int
    manifest_digest: str
    config_fingerprint: str
    candidate_digest: str
    source_revision: str
    contract_version: str
    contract_digest: str
    provider_identity: str
    reviewer_subject: str
    scope: str
    observed_at: float
    revoked: bool
    report: dict


class GovernanceGate:
    def __init__(self, store, registry, *, providers=None):
        if registry.store is not store:
            raise ValueError('shared registered store required')
        self.store, self.registry = store, registry
        self.providers = dict(providers or {})
        for project, callback in self.providers.items():
            name(project)
            if not callable(callback):
                raise ValueError('registered QA provider callback required')
        with store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS governance_enrolled_projects (project TEXT PRIMARY KEY, enrolled_at REAL NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS governance_qa_policies (project TEXT PRIMARY KEY, policy TEXT NOT NULL, policy_digest TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS governance_qa_policy_history (project TEXT NOT NULL, contract_version TEXT NOT NULL, policy TEXT NOT NULL, policy_digest TEXT NOT NULL, PRIMARY KEY(project,contract_version))')
            for row in c.execute('SELECT * FROM governance_qa_policies').fetchall():
                old=json.loads(row['policy'])
                c.execute('INSERT OR IGNORE INTO governance_qa_policy_history VALUES (?,?,?,?)',(row['project'],old['contract_version'],row['policy'],row['policy_digest']))

    def enroll(self, token, projects):
        """Durably restrict selected project acceptance; never grants permissions."""
        if type(projects) not in (list, tuple) or not projects or len(projects)>100 or len(set(projects)) != len(projects):
            raise ValueError('explicit unique bounded governance projects required')
        for project in projects:
            name(project)
        with self.store._transaction() as c:
            for project in projects:
                self.registry._get(c,token,project,('owner','manager'))
            now=self.store._now()
            for project in projects:
                c.execute('INSERT OR IGNORE INTO governance_enrolled_projects VALUES (?,?)',(project,now))
        return {'projects':list(projects),'governance_required':True,'operational_authorization':False}

    def register_policy(self, token, project, *, contract_version, expected_contract,
                        required_coverage, provider_identity, scope='synthetic_only',
                        max_age_seconds=300, contract_ref=None, expected_policy_digest=None):
        """Register immutable versions; switch active policy with an owner CAS."""
        name(project)
        if type(contract_version) is not str or not contract_version.strip():
            raise ValueError('exact QA contract version required')
        if type(provider_identity) is not str or not provider_identity.strip():
            raise ValueError('registered provider identity required')
        contract_ref = contract_version if contract_ref is None else contract_ref
        if type(contract_ref) is not str or not contract_ref.strip():
            raise ValueError('exact QA contract reference required')
        if scope not in ('synthetic_only', 'actual_project'):
            raise ValueError('explicit QA evidence scope required')
        if type(max_age_seconds) not in (int, float) or not math.isfinite(max_age_seconds) or not 0 < max_age_seconds <= 3600:
            raise ValueError('bounded QA freshness required')
        # Reuse the admission contract validator; malformed_report is expected here.
        validation = admit_qa_report(None, expected_source_revision='a'*40,
                                    required_coverage=required_coverage, expected_contract=expected_contract)
        if validation['reasons'] != ['malformed_report']:
            raise ValueError('valid independently stored QA contract required')
        if type(required_coverage) not in (list, tuple, set, frozenset) or not required_coverage or any(type(x) is not str or not x for x in required_coverage):
            raise ValueError('required QA coverage required')
        if not set(required_coverage) <= {row['coverage_key'] for row in expected_contract['scenarios'].values()}:
            raise ValueError('contract does not define required QA coverage')
        with self.store._transaction() as c:
            config = self.registry._get(c, token, project, ('owner',))
            policy = dict(project=project, config_fingerprint=config['fingerprint'],
                          source_config=config['config'], contract_version=contract_version, contract_ref=contract_ref,
                          contract_digest=digest(expected_contract), expected_contract=expected_contract,
                          required_coverage=sorted(set(required_coverage)), provider_identity=provider_identity,
                          scope=scope, max_age_seconds=max_age_seconds)
            encoded = canonical_json(policy)
            old = c.execute('SELECT policy,policy_digest FROM governance_qa_policies WHERE project=?', (project,)).fetchone()
            if old is None and expected_policy_digest is not None:
                raise Conflict('no active QA policy matches expected policy digest')
            if old and old['policy'] != encoded:
                if expected_policy_digest != old['policy_digest']:
                    raise Conflict('registered QA policy replacement requires exact active policy digest')
                if contract_version == json.loads(old['policy'])['contract_version']:
                    raise Conflict('registered QA policy version is immutable')
            history=c.execute('SELECT policy FROM governance_qa_policy_history WHERE project=? AND contract_version=?',(project,contract_version)).fetchone()
            if history and history['policy'] != encoded:
                raise Conflict('registered QA policy version is immutable')
            if history and old and old['policy'] != encoded:
                raise Conflict('registered QA policy rollover requires a new contract version')
            c.execute('INSERT OR IGNORE INTO governance_qa_policy_history VALUES (?,?,?,?)',(project,contract_version,encoded,digest(policy)))
            c.execute('INSERT OR REPLACE INTO governance_qa_policies VALUES (?,?,?)', (project, encoded, digest(policy)))
            return dict(project=project, policy_digest=digest(policy), contract_digest=policy['contract_digest'], mandatory=True, scope=scope, operational_authorization=False)

    def _snapshot(self, c, candidate):
        """Reobserve every acceptance/freshness boundary; no policy preserves legacy behavior."""
        task = candidate['task']
        row = c.execute('SELECT * FROM governance_qa_policies WHERE project=?', (task['project'],)).fetchone()
        approval = task.get('approval') or {}
        if type(approval) is str:
            approval = json.loads(approval)
        proposal = 'proposal_batch_id' in approval
        if row is None:
            enrolled=c.execute('SELECT 1 FROM governance_enrolled_projects WHERE project=?',(task['project'],)).fetchone()
            if proposal or enrolled:
                raise Conflict('governed project requires registered mandatory QA policy')
            return None
        policy = json.loads(row['policy'])
        if proposal:
            linked = c.execute('SELECT snapshot,fingerprint,task FROM manager_proposals WHERE id=?', (approval.get('proposal_id'),)).fetchone()
            if not linked or linked['task'] != task['id'] or linked['fingerprint'] != approval.get('proposal_digest'):
                raise Conflict('proposal QA binding changed')
            pinned_policy=validate_proposal_policy(c, task['project'], json.loads(linked['snapshot'])['spec'])
            if approval.get('qa_policy_digest') != pinned_policy:
                raise Conflict('proposal approved QA policy version changed')
        historical=c.execute('SELECT policy,policy_digest FROM governance_qa_policy_history WHERE project=? AND contract_version=?',(task['project'],policy['contract_version'])).fetchone()
        if digest(policy) != row['policy_digest'] or not historical or historical['policy'] != row['policy'] or historical['policy_digest'] != row['policy_digest']:
            raise Conflict('registered QA policy integrity changed')
        current = c.execute('SELECT fingerprint,config FROM maintenance_projects WHERE project=?', (task['project'],)).fetchone()
        if current is None or current['fingerprint'] != policy['config_fingerprint'] or json.loads(current['config']) != policy['source_config'] or candidate['config_fingerprint'] != policy['config_fingerprint']:
            raise Conflict('QA policy source configuration changed')
        return policy, row['policy_digest']

    def require(self, token, candidate, reviewer_subject):
        with self.store._transaction() as c:
            self.registry._get(c, token, candidate['task']['project'], ('owner', 'manager', 'worker'))
            snapshot = self._snapshot(c, candidate)
        if snapshot is None:
            return None
        policy, policy_digest = snapshot
        task = candidate['task']
        callback = self.providers.get(task['project'])
        if callback is None:
            raise Conflict('mandatory registered QA provider unavailable')
        try:
            observation = callback(json.loads(canonical_json(candidate)), reviewer_subject)
        except Exception as exc:
            raise Conflict('mandatory QA observation unavailable') from exc
        if type(observation) is not QAObservation:
            raise Conflict('typed registered QA observation required')
        bindings = dict(project=task['project'], task_id=task['id'], version=task['version'],
                        manifest_digest=task['manifest_digest'], config_fingerprint=policy['config_fingerprint'],
                        candidate_digest=candidate['digest'], source_revision=candidate['source']['head_sha'],
                        contract_version=policy['contract_version'], contract_digest=policy['contract_digest'],
                        provider_identity=policy['provider_identity'], reviewer_subject=reviewer_subject, scope=policy['scope'])
        if any(type(getattr(observation, key)) is not type(value) or getattr(observation, key) != value for key, value in bindings.items()):
            raise Conflict('mandatory QA exact binding mismatch')
        now = self.store._now()
        if observation.revoked is not False or type(observation.observed_at) not in (int, float) or not math.isfinite(observation.observed_at) or not 0 < observation.observed_at <= now or now - observation.observed_at > policy['max_age_seconds']:
            raise Conflict('mandatory QA observation stale or revoked')
        if reviewer_subject == candidate['developer_subject']:
            raise Conflict('independent QA reviewer required')
        # The existing engine is synthetic. An actual-project declaration cannot
        # promote its reports to measured project evidence.
        if observation.scope != 'synthetic_only':
            raise Conflict('actual project QA engine not registered')
        result = admit_qa_report(observation.report,
                                 expected_source_revision=bindings['source_revision'],
                                 required_coverage=policy['required_coverage'],
                                 expected_contract=policy['expected_contract'])
        if not result['admitted']:
            raise Conflict('mandatory QA admission failed: ' + ','.join(result['reasons']))
        with self.store._transaction() as c:
            self.registry._get(c, token, task['project'], ('owner', 'manager', 'worker'))
            self.verify_snapshot(c, candidate, policy_digest, {'observed_at':observation.observed_at})
        return dict(result, policy_digest=policy_digest, contract_version=policy['contract_version'],
                    provider_identity=policy['provider_identity'], contract_digest=policy['contract_digest'],
                    report_digest=observation.report['report_digest'], observed_at=observation.observed_at,
                    candidate_digest=candidate['digest'], source_revision=observation.source_revision,
                    actual_project_verified=False)

    def verify_snapshot(self, c, candidate, policy_digest, qa=None):
        snapshot = self._snapshot(c, candidate)
        current = snapshot[1] if snapshot else None
        if current != policy_digest:
            raise Conflict('mandatory QA policy changed during observation')
        if snapshot is not None:
            observed_at = qa.get('observed_at') if type(qa) is dict else None
            now = self.store._now()
            if type(observed_at) not in (int,float) or not math.isfinite(observed_at) or not 0 < observed_at <= now or now-observed_at > snapshot[0]['max_age_seconds']:
                raise Conflict('mandatory QA observation expired before acceptance')


def validate_proposal_policy(c, project, spec):
    """Proposal approval and technical acceptance share the same trusted QA pin."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='governance_qa_policies'").fetchone():
        raise Conflict('proposal requires registered mandatory QA policy')
    row = c.execute('SELECT policy,policy_digest FROM governance_qa_policies WHERE project=?', (project,)).fetchone()
    if row is None:
        raise Conflict('proposal requires registered mandatory QA policy')
    policy = json.loads(row['policy'])
    config = c.execute('SELECT fingerprint FROM maintenance_projects WHERE project=?',(project,)).fetchone()
    if digest(policy) != row['policy_digest'] or not config or config['fingerprint'] != policy['config_fingerprint']:
        raise Conflict('proposal QA policy configuration changed')
    if spec.get('qa_contract_digest') != policy['contract_digest'] or spec.get('qa_contract_ref') != policy.get('contract_ref', policy['contract_version']):
        raise Conflict('proposal QA contract pin differs from registered policy')
    return row['policy_digest']
