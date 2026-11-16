"""Portable, trusted-adapter contract QA. Pure fixtures are never operational proof."""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import re
from .core import CoreError, ident

SCHEMA = 'universal-qa/v1'
STATUSES = {'pass', 'fail', 'blocked', 'unmeasured'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class EvidenceUnavailable(CoreError):
    """A trusted adapter has no execution observation; never a passing verdict."""


@dataclass(frozen=True)
class Risk:
    priority: str
    impact: str
    likelihood: str
    recovery: str

    def __post_init__(self):
        if self.priority not in {'P0', 'P1', 'P2', 'P3'} or not all(
                type(v) is str and v.strip() for v in (self.impact, self.likelihood, self.recovery)):
            raise CoreError('risk requires impact, likelihood and recovery rationale')


@dataclass(frozen=True)
class Scenario:
    id: str
    adapter: str
    group: str
    coverage_key: str
    risk: Risk
    inputs: dict
    expected: object
    oracle: str
    state_rationale: str
    formula: dict | None = None

    def __post_init__(self):
        for value in (self.id, self.adapter, self.group, self.coverage_key):
            ident(value)
        if not isinstance(self.risk, Risk) or type(self.inputs) is not dict or not self.oracle or not self.state_rationale:
            raise CoreError('invalid scenario contract')
        fingerprint(self.inputs)
        fingerprint(self.expected)
        if self.formula is not None:
            needed = {'id', 'expression', 'variables', 'units', 'rounding', 'expected'}
            if type(self.formula) is not dict or not needed <= self.formula.keys():
                raise CoreError('incomplete formula contract')


@dataclass(frozen=True)
class Adapter:
    name: str
    version: str
    execute: object

    def __post_init__(self):
        ident(self.name)
        ident(self.version)
        if not callable(self.execute):
            raise CoreError('trusted callable adapter required')


CONTRACT_SCHEMA = 'universal-qa-contract/v1'
CONTRACT_FIELDS = ('id', 'adapter', 'group', 'coverage_key', 'priority', 'risk',
                   'inputs', 'expected', 'oracle', 'state_rationale', 'state_key')
FORMULA_FIELDS = ('id', 'expression', 'variables', 'units', 'rounding', 'expected')


def scenario_contract_row(row):
    """Project an observation onto immutable manager-owned contract fields."""
    value = {key: row[key] for key in CONTRACT_FIELDS}
    value['formula'] = ({key: row['formula'][key] for key in FORMULA_FIELDS}
                        if row.get('formula') is not None else None)
    return value


def build_contract(scenarios, registrations):
    """Manager builds/reviews this BEFORE accepting any worker report.

    Persist it in a trusted manager-owned location. Reconstructing it from the
    submitted report defeats priority, oracle, formula and coverage protection.
    """
    rows = {}
    for s in scenarios:
        if s.id in rows:
            raise CoreError('duplicate contract scenario')
        row = {'id':s.id, 'adapter':s.adapter, 'group':s.group, 'coverage_key':s.coverage_key,
               'priority':s.risk.priority, 'risk':vars(s.risk), 'inputs':s.inputs,
               'expected':s.expected, 'oracle':s.oracle, 'state_rationale':s.state_rationale,
               'state_key':fingerprint([s.adapter,s.inputs]), 'formula':s.formula}
        rows[s.id] = scenario_contract_row(row)
    # Detach trusted state from mutable scenario/report objects. A worker must
    # never be able to modify a manager manifest through a shared dictionary.
    return json.loads(json.dumps({'schema_version':CONTRACT_SCHEMA,
            'adapter_versions':{a.name:a.version for a in registrations}, 'scenarios':rows},
            ensure_ascii=False, allow_nan=False))


def run_scenarios(scenarios, adapters, *, source_revision, seed, required_coverage):
    """Execute registered Python callables only; no eval/exec/import of submitted code.

    An adapter returns {'actual': JSON, 'evidence': JSON}. Exceptions are blocked,
    absent adapters unmeasured. Equality is the explicit contract assertion; expected
    values MUST come from a separately reviewed specification/oracle, not the SUT.
    """
    if not re.fullmatch('[0-9a-f]{40}', source_revision):
        raise CoreError('exact source revision required')
    scenarios = deepcopy(list(scenarios))
    if not scenarios or len({s.id for s in scenarios}) != len(scenarios):
        raise CoreError('nonempty unique scenarios required')
    registry = {a.name: a for a in adapters}
    if len(registry) != len(adapters):
        raise CoreError('duplicate adapter')
    contract_digest = fingerprint(build_contract(scenarios, adapters))
    rows, observed, failures = [], set(), set()
    executed = asserted = 0
    for s in scenarios:
        row = {'id': s.id, 'adapter': s.adapter, 'group': s.group,
               'coverage_key': s.coverage_key, 'priority': s.risk.priority,
               'risk': vars(s.risk), 'inputs': s.inputs, 'expected': s.expected,
               'oracle': s.oracle, 'state_rationale': s.state_rationale,
               'actual': None, 'evidence': None, 'executed': False, 'asserted': False,
               'status': 'unmeasured', 'state_key': fingerprint([s.adapter, s.inputs])}
        adapter = registry.get(s.adapter)
        if adapter is not None:
            executed += 1
            row['executed'] = True
            try:
                result = adapter.execute(json.loads(json.dumps(s.inputs, allow_nan=False)))
                if type(result) is not dict or 'actual' not in result or not result.get('evidence'):
                    raise CoreError('adapter evidence missing')
                fingerprint(result)
                row.update(actual=result['actual'], evidence=result['evidence'], asserted=True)
                asserted += 1
                row['status'] = 'pass' if fingerprint(result['actual']) == fingerprint(s.expected) else 'fail'
                observed.add(s.coverage_key)
                if row['status'] == 'fail':
                    row['failure_key'] = fingerprint([s.adapter, s.coverage_key, s.expected, result['actual']])
                    failures.add(row['failure_key'])
            except EvidenceUnavailable:
                executed -= 1
                row.update(status='unmeasured', executed=False, evidence={'reason':'execution_evidence_unavailable'})
            except Exception as exc:
                row.update(status='blocked', evidence={'error_type': type(exc).__name__})
        if s.formula is not None:
            row['formula'] = dict(s.formula, actual=row['actual'], status=row['status'])
        rows.append(row)
    report = {'schema_version': SCHEMA, 'environment': 'synthetic',
              'scope': 'pure synthetic fixtures; no real users, UI, production or AI ability measurement',
              'source_revision': source_revision, 'seed': seed,
              'adapter_versions': {a.name: a.version for a in adapters},
              'contract_digest': contract_digest,
              'coverage': {'required': sorted(set(required_coverage)), 'observed': sorted(observed)},
              'counts': {'generated': len(rows), 'executed': executed, 'asserted': asserted,
                         'unique_failures': len(failures),
                         'unique_states': len({r['state_key'] for r in rows}),
                         **{status: sum(r['status'] == status for r in rows) for status in sorted(STATUSES)}},
              'scenarios': rows}
    report['report_digest'] = fingerprint(report)
    return report
