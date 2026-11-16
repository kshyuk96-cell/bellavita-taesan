"""Predeclared project mechanical QA over durable independent execution evidence.

This adapter never runs candidate code. FixedReviewRunner owns execution and
isolation. Reports remain synthetic_only: mechanical checks are not customer UI,
business outcome, model capability or operational authorization evidence.
"""
from dataclasses import dataclass
import json
import re
from .store import Conflict, canonical_json, digest
from .maintenance_registry import name
from .governance_gate import QAObservation
from .three_tier import CheckOutcome, ReviewEvidence, CodeAssessment
from taesan_core.universal_qa import Adapter, Risk, Scenario, build_contract, run_scenarios


ADAPTER = 'persisted-mechanical-check'
ADAPTER_VERSION = '1'


def _scenarios(payload):
    return tuple(Scenario(
        'mechanical-'+str(index), ADAPTER, 'mechanical', 'mechanical-'+str(index),
        Risk('P1', 'Approved mechanical check must succeed',
             'A missing or changed command can invalidate review', 'Independent review and rework'),
        dict(project=payload['project'], config_fingerprint=payload['config_fingerprint'],
             source_base_revision=payload['approved_base_sha'], check=label, argv_digest=command_digest),
        0, 'Owner-registered command specification requires process exit zero',
        'One exact approved command for a candidate derived from the approved source base')
        for index, (label, command_digest) in enumerate(sorted(payload['checks'].items())))


@dataclass(frozen=True)
class ProjectQAContract:
    """Immutable canonical values, detached from caller-owned mutable objects."""
    payload_json: str
    expected_contract_json: str

    @property
    def payload(self):
        return json.loads(self.payload_json)

    @property
    def expected_contract(self):
        return json.loads(self.expected_contract_json)

    @property
    def required_coverage(self):
        return tuple(s.coverage_key for s in _scenarios(self.payload))

    @property
    def project(self): return self.payload['project']
    @property
    def contract_version(self): return self.payload['contract_version']
    @property
    def provider_identity(self): return self.payload['provider_identity']
    @property
    def contract_ref(self): return self.payload['contract_ref']

    def policy_arguments(self):
        return dict(contract_version=self.contract_version, contract_ref=self.contract_ref,
                    provider_identity=self.provider_identity, expected_contract=self.expected_contract,
                    required_coverage=list(self.required_coverage), scope='synthetic_only')


def freeze_project_qa_contract(*, project, config_fingerprint, checks,
                               contract_version, provider_identity, contract_ref=None,
                               approved_base_sha=None, source_revision=None):
    """Call from trusted host configuration BEFORE any review starts.

    approved_base_sha is the known remote source anchor before development.
    A later derived task base requires the exact durable Board parent authority
    rooted at this same remote anchor; arbitrary descendant declarations fail.
    The legacy source_revision keyword is an alias for that base, never a future
    candidate HEAD. The actual candidate HEAD is separately bound by the report.
    Every approved check label and exact command is included. Additional business
    oracles must use separate explicitly reviewed adapters; this mechanical
    provider does not infer business expectations from successful commands.
    """
    name(project)
    if type(config_fingerprint) is not str or not re.fullmatch('[0-9a-f]{64}',config_fingerprint):
        raise ValueError('exact project configuration fingerprint required')
    if approved_base_sha is not None and source_revision is not None:
        raise ValueError('supply one approved source base pin')
    approved_base_sha = approved_base_sha if approved_base_sha is not None else source_revision
    if type(approved_base_sha) is not str or not re.fullmatch('[0-9a-f]{40}',approved_base_sha):
        raise ValueError('exact approved source base revision required')
    if type(checks) is not dict or not 1 <= len(checks) <= 100:
        raise ValueError('nonempty bounded trusted command mapping required')
    commands={}
    for label, argv in checks.items():
        if type(label) is not str or not label or len(label)>200 or type(argv) not in (list,tuple) or not argv or any(type(arg) is not str or not arg or '\x00' in arg for arg in argv):
            raise ValueError('exact named trusted command required')
        commands[label]=digest(list(argv))
    for value in (contract_version,provider_identity,contract_ref if contract_ref is not None else contract_version):
        if type(value) is not str or not value.strip():
            raise ValueError('contract version, reference and provider identity required')
    payload=dict(project=project,config_fingerprint=config_fingerprint,approved_base_sha=approved_base_sha,
                 checks=commands,contract_version=contract_version,provider_identity=provider_identity,
                 contract_ref=contract_ref if contract_ref is not None else contract_version)
    adapters=[Adapter(ADAPTER,ADAPTER_VERSION,lambda _: None)]
    expected=build_contract(_scenarios(payload),adapters)
    return ProjectQAContract(canonical_json(payload),canonical_json(expected))


class PersistedReviewQAProvider:
    """Registered callback over completed server-owned independent review records."""
    def __init__(self,store,*,contracts):
        self.store=store
        self.contracts=dict(contracts)
        for project,contract in self.contracts.items():
            if type(contract) is not ProjectQAContract or contract.project!=project:
                raise ValueError('one frozen QA contract per registered project required')
            if build_contract(_scenarios(contract.payload),[Adapter(ADAPTER,ADAPTER_VERSION,lambda _:None)]) != contract.expected_contract:
                raise ValueError('frozen project QA contract integrity mismatch')
        with store._transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS project_mechanical_qa_contracts (project TEXT, contract_version TEXT, payload TEXT NOT NULL, expected_contract TEXT NOT NULL, registered_at REAL NOT NULL, event_watermark INTEGER NOT NULL, PRIMARY KEY(project,contract_version))')
            for project,contract in self.contracts.items():
                payload=contract.payload
                old=c.execute('SELECT * FROM project_mechanical_qa_contracts WHERE project=? AND contract_version=?',(project,contract.contract_version)).fetchone()
                if old:
                    if old['payload']!=contract.payload_json or old['expected_contract']!=contract.expected_contract_json:
                        raise Conflict('registered mechanical QA contract version is immutable')
                    continue
                has_events=c.execute("SELECT 1 FROM sqlite_master WHERE name='hierarchy_events'").fetchone()
                watermark=c.execute('SELECT COALESCE(MAX(id),0) FROM hierarchy_events').fetchone()[0] if has_events else 0
                c.execute('INSERT INTO project_mechanical_qa_contracts VALUES (?,?,?,?,?,?)',(project,contract.contract_version,contract.payload_json,contract.expected_contract_json,store._now(),watermark))

    def __call__(self,candidate,reviewer_subject):
        try:
            return self._observe(candidate,reviewer_subject)
        except Conflict:
            raise
        except (KeyError,TypeError,ValueError) as exc:
            raise Conflict('malformed persisted mechanical QA evidence') from exc

    def _observe(self,candidate,reviewer_subject):
        task=candidate['task']; project=task['project']
        contract=self.contracts.get(project)
        if contract is None:
            raise Conflict('project mechanical QA contract not registered')
        payload=contract.payload; source=candidate['source']
        labels=task['manifest']['checks']
        if (type(labels) is not list or len(labels)!=len(set(labels)) or set(labels)!=set(payload['checks']) or
                candidate['config_fingerprint']!=payload['config_fingerprint'] or
                source['project']!=project or source['task_id']!=task['id'] or
                source['base_sha']!=task['manifest']['base_sha'] or
                source['checks_digest']!=digest(payload['checks']) or
                candidate['developer_subject']==reviewer_subject):
            raise Conflict('candidate differs from predeclared mechanical QA contract')
        with self.store._transaction() as c:
            frozen=c.execute('SELECT * FROM project_mechanical_qa_contracts WHERE project=? AND contract_version=?',(project,contract.contract_version)).fetchone()
            if not frozen or frozen['payload']!=contract.payload_json or frozen['expected_contract']!=contract.expected_contract_json:
                raise Conflict('pre-execution mechanical QA registration changed')
            row=c.execute('SELECT * FROM hierarchy_tasks WHERE task_id=?',(task['id'],)).fetchone()
            current=c.execute('SELECT fingerprint FROM maintenance_projects WHERE project=?',(project,)).fetchone()
            upstream=c.execute('SELECT * FROM bridge_tasks WHERE id=?',(task['id'],)).fetchone()
            if (not row or row['state'] not in ('review_passed','accepted') or not row['evidence'] or
                    not current or current['fingerprint']!=payload['config_fingerprint'] or
                    not upstream or upstream['project']!=project or upstream['version']!=task['version'] or upstream['manifest_digest']!=task['manifest_digest'] or
                    row['project']!=project or row['version']!=task['version'] or
                    row['manifest_digest']!=task['manifest_digest'] or row['config_fingerprint']!=payload['config_fingerprint']):
                raise Conflict('completed exact independent review required for mechanical QA')
            if task['manifest']['base_sha']!=payload['approved_base_sha']:
                # A derived base is authority only through the exact owner-approved
                # Board snapshot and its accepted parent's immutable proof.
                approval=json.loads(upstream['approval'] or '{}')
                if 'proposal_batch_id' not in approval:
                    raise Conflict('derived mechanical QA base requires approved Board parent authority')
                self.store._validate_execution(c,upstream)
                proposal=c.execute('SELECT snapshot FROM manager_proposals WHERE id=?',(approval.get('proposal_id'),)).fetchone()
                snapshot=json.loads(proposal['snapshot']) if proposal else {}
                parent=snapshot.get('parent_authority')
                if (snapshot.get('source',{}).get('sha')!=payload['approved_base_sha'] or
                        type(parent) is not dict or parent.get('head_sha')!=task['manifest']['base_sha']):
                    raise Conflict('derived mechanical QA base differs from frozen source anchor')
            saved=json.loads(row['candidate'])
            if saved['digest']!=candidate['digest'] or saved['identity']!=candidate['identity'] or saved['source']!=source or saved['task']['manifest']!=task['manifest']:
                raise Conflict('persisted review candidate differs from mechanical QA request')
            if not c.execute("SELECT 1 FROM hierarchy_review_runs WHERE task_id=? AND candidate_digest=? AND state='completed'",(task['id'],candidate['digest'])).fetchone():
                raise Conflict('completed durable independent execution required')
            member=c.execute('SELECT * FROM hierarchy_members WHERE project=? AND member_id=?',(project,row['reviewer_id'])).fetchone()
            if not member or member['role']!='reviewer' or member['subject']!=reviewer_subject or member['config_fingerprint']!=payload['config_fingerprint']:
                raise Conflict('independent persisted reviewer binding mismatch')
            token=c.execute('SELECT * FROM bridge_tokens WHERE token_hash=?',(member['credential_hash'],)).fetchone()
            now=self.store._now()
            if not token or token['subject']!=reviewer_subject or token['role']!='worker' or token['revoked'] is not None or (token['expires'] is not None and token['expires']<=now) or not self.store._scope({'role':token['role'],'projects':json.loads(token['projects'])},project):
                raise Conflict('independent reviewer authority no longer current')
            claimed=c.execute("SELECT MAX(id) FROM hierarchy_events WHERE task_id=? AND kind='review_claimed'",(task['id'],)).fetchone()[0]
            if claimed is None or claimed<=frozen['event_watermark']:
                raise Conflict('mechanical QA contract must be frozen before independent execution starts')
            raw=json.loads(row['evidence'])
        checks=tuple(CheckOutcome(**item) for item in raw['checks'])
        assessment=CodeAssessment(**raw['code_assessment']) if raw.get('code_assessment') else None
        evidence=ReviewEvidence(**{**raw,'checks':checks,'code_assessment':assessment})
        if (evidence.candidate_digest!=candidate['digest'] or evidence.reviewer_subject!=reviewer_subject or
                evidence.artifact_digest!=source['artifact_digest'] or evidence.runtime_digest!=source['runtime_digest'] or
                evidence.isolation_verified is not True or not evidence.reference or
                not frozen['registered_at'] <= evidence.observed_at <= now or now-evidence.observed_at > 300):
            raise Conflict('mechanical QA independent evidence binding mismatch')
        if task['manifest']['kind']=='development' and (assessment is None or assessment.candidate_digest!=candidate['digest'] or assessment.reviewer_subject!=reviewer_subject or assessment.recommendation!='pass'):
            raise Conflict('independent code assessment required before mechanical QA')
        by_name={check.name:check for check in checks}
        if set(by_name)!=set(payload['checks']) or any(check.exit_code!=0 or check.argv_digest!=payload['checks'][check.name] for check in checks):
            raise Conflict('all predeclared mechanical checks must actually pass unchanged')
        def observed(inputs):
            check=by_name[inputs['check']]
            return {'actual':check.exit_code,'evidence':dict(reference=evidence.reference,
                        review_evidence_digest=digest(raw),candidate_digest=candidate['digest'],
                        reviewer_subject=reviewer_subject,argv_digest=check.argv_digest,
                        elapsed_seconds=check.elapsed_seconds)}
        report=run_scenarios(_scenarios(payload),[Adapter(ADAPTER,ADAPTER_VERSION,observed)],
                             source_revision=source['head_sha'],seed=task['id'],required_coverage=contract.required_coverage)
        if report['contract_digest']!=digest(contract.expected_contract):
            raise Conflict('mechanical QA contract changed')
        return QAObservation(project,task['id'],task['version'],task['manifest_digest'],
                 payload['config_fingerprint'],candidate['digest'],source['head_sha'],
                 contract.contract_version,digest(contract.expected_contract),contract.provider_identity,
                 reviewer_subject,'synthetic_only',evidence.observed_at,False,report)
